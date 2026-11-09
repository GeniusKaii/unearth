"""Dependency free EXIF / TIFF parser.

Used for JPEG APP1, PNG ``eXIf``, HEIF ``Exif`` and as the core of the TIFF
based RAW carver (CR2 / NEF / ARW / DNG).  Only the tags that help a human
decide "is this a photo of my life?" are interpreted: capture time, camera,
orientation, GPS, pixel size.  It never raises on truncated or hostile data.
"""

from __future__ import annotations

import datetime as dt
import struct

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

T_MAKE = 0x010F
T_MODEL = 0x0110
T_ORIENTATION = 0x0112
T_DATETIME = 0x0132
T_SOFTWARE = 0x0131
T_IMAGE_WIDTH = 0x0100
T_IMAGE_LENGTH = 0x0101
T_STRIP_OFFSETS = 0x0111
T_STRIP_COUNTS = 0x0117
T_SUB_IFDS = 0x014A
T_EXIF_IFD = 0x8769
T_GPS_IFD = 0x8825
T_EXIF_WIDTH = 0xA002
T_EXIF_HEIGHT = 0xA003
T_DATETIME_ORIGINAL = 0x9003
T_DATETIME_DIGITIZED = 0x9004
T_OFFSET_ORIGINAL = 0x9011
T_ISO = 0x8827
T_FNUMBER = 0x829D
T_EXPOSURE = 0x829A
T_FOCAL = 0x920A
T_DNG_VERSION = 0xC612
T_TILE_OFFSETS = 0x0144
T_TILE_COUNTS = 0x0145

SUB_IFD_TAGS = (T_EXIF_IFD, T_GPS_IFD, T_SUB_IFDS)


class Tiff:
    def __init__(self, data: bytes):
        if len(data) < 8:
            raise ValueError("too short for TIFF")
        self.d = data
        if data[:2] == b"II":
            self.e = "<"
        elif data[:2] == b"MM":
            self.e = ">"
        else:
            raise ValueError("bad TIFF byte order")
        if self.u16(2) != 42:
            raise ValueError("bad TIFF magic")
        self.first = self.u32(4)

    def u16(self, o: int) -> int:
        return struct.unpack_from(self.e + "H", self.d, o)[0]

    def u32(self, o: int) -> int:
        return struct.unpack_from(self.e + "I", self.d, o)[0]

    def ifd(self, offset: int):
        """Return ([(tag, type, count, entry_offset)], next_ifd_offset)."""
        if offset <= 0 or offset + 2 > len(self.d):
            raise ValueError("IFD out of range")
        count = self.u16(offset)
        if count > 1024:
            raise ValueError("absurd IFD entry count")
        entries = []
        base = offset + 2
        for i in range(count):
            eo = base + i * 12
            if eo + 12 > len(self.d):
                break
            entries.append((self.u16(eo), self.u16(eo + 2), self.u32(eo + 4), eo))
        nxt = 0
        nxt_off = base + count * 12
        if nxt_off + 4 <= len(self.d):
            nxt = self.u32(nxt_off)
        return entries, nxt

    def value(self, typ: int, count: int, entry_off: int):
        size = TYPE_SIZES.get(typ, 0) * count
        if size <= 0 or count > 200000:
            return None
        if size <= 4:
            raw = self.d[entry_off + 8: entry_off + 8 + size]
        else:
            off = self.u32(entry_off + 8)
            if off < 0 or off + size > len(self.d):
                return None
            raw = self.d[off: off + size]
        if typ == 2:
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if typ in (1, 7):
            return list(raw)
        if typ == 3:
            return list(struct.unpack_from(self.e + "%dH" % count, raw))
        if typ == 4:
            return list(struct.unpack_from(self.e + "%dI" % count, raw))
        if typ in (5, 10):
            fmt = self.e + "%dI" % (count * 2)
            vals = struct.unpack_from(fmt, raw)
            out = []
            for i in range(0, len(vals), 2):
                den = vals[i + 1] or 1
                out.append((vals[i], den))
            return out
        if typ == 9:
            return list(struct.unpack_from(self.e + "%di" % count, raw))
        if typ == 8:
            return list(struct.unpack_from(self.e + "%dh" % count, raw))
        return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _ratio(pair) -> float | None:
    try:
        num, den = pair
        if not den:
            return None
        return float(num) / float(den)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms(values, ref: str) -> float | None:
    if not values or len(values) < 3:
        return None
    deg = _ratio(values[0])
    minutes = _ratio(values[1])
    secs = _ratio(values[2])
    if deg is None:
        return None
    out = deg + (minutes or 0) / 60.0 + (secs or 0) / 3600.0
    if ref.upper() in ("S", "W"):
        out = -out
    return round(out, 7)


def parse_exif_datetime(text: str):
    """'2013:08:12 19:22:33' -> datetime, tolerating junk. Returns (dt, offset)."""
    if not isinstance(text, str):
        return None, None
    s = text.strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:19], fmt), None
        except ValueError:
            continue
    return None, None


def parse_tiff(data: bytes) -> tuple[dict, int]:
    """Parse a TIFF block.

    Returns ``(meta, max_data_end)`` where ``max_data_end`` is the furthest byte
    referenced by strip/tile data inside ``data`` - that is how RAW files get a
    reliable size instead of a guess.
    """
    meta: dict = {}
    try:
        t = Tiff(data)
    except (ValueError, struct.error):
        return meta, 0

    tags: dict = {}
    max_end = 0
    seen: set = set()

    def walk(offset: int, depth: int = 0) -> None:
        nonlocal max_end
        if depth > 6 or offset in seen or offset <= 0 or offset + 2 > len(data):
            return
        seen.add(offset)
        try:
            entries, nxt = t.ifd(offset)
        except (ValueError, struct.error):
            return
        for tag, typ, count, eo in entries:
            if tag in (T_STRIP_OFFSETS, T_TILE_OFFSETS):
                try:
                    offs = t.value(typ, count, eo)
                    cnt_tag = T_STRIP_COUNTS if tag == T_STRIP_OFFSETS else T_TILE_COUNTS
                    counts = None
                    for t2, ty2, c2, eo2 in entries:
                        if t2 == cnt_tag:
                            counts = t.value(ty2, c2, eo2)
                            break
                except (ValueError, struct.error):
                    offs = counts = None
                if offs and counts:
                    try:
                        for o, c in zip(offs, counts):
                            end = int(o) + int(c)
                            if 0 < end <= len(data) and end > max_end:
                                max_end = end
                    except (TypeError, ValueError):
                        pass
                continue
            if tag in SUB_IFD_TAGS:
                try:
                    val = t.value(typ, count, eo)
                except (ValueError, struct.error):
                    val = None
                for sub in _as_list(val):
                    try:
                        walk(int(sub), depth + 1)
                    except (TypeError, ValueError):
                        continue
                continue
            if tag not in tags:
                try:
                    tags[tag] = t.value(typ, count, eo)
                except (ValueError, struct.error):
                    continue
        if nxt and nxt not in seen:
            walk(nxt, depth + 1)

    walk(t.first)

    make = tags.get(T_MAKE)
    model = tags.get(T_MODEL)
    if isinstance(make, str) and make:
        meta["make"] = make
    if isinstance(model, str) and model:
        meta["model"] = model
    software = tags.get(T_SOFTWARE)
    if isinstance(software, str) and software:
        meta["software"] = software

    orient = tags.get(T_ORIENTATION)
    if isinstance(orient, list) and orient:
        meta["orientation"] = int(orient[0])

    width = tags.get(T_EXIF_WIDTH) or tags.get(T_IMAGE_WIDTH)
    height = tags.get(T_EXIF_HEIGHT) or tags.get(T_IMAGE_LENGTH)
    if isinstance(width, list) and width:
        meta["width"] = int(width[0])
    if isinstance(height, list) and height:
        meta["height"] = int(height[0])

    when = tags.get(T_DATETIME_ORIGINAL) or tags.get(T_DATETIME_DIGITIZED) or tags.get(T_DATETIME)
    stamp, _ = parse_exif_datetime(when if isinstance(when, str) else "")
    offset_text = tags.get(T_OFFSET_ORIGINAL)
    if stamp:
        tz = None
        if isinstance(offset_text, str) and offset_text.strip():
            tz = _parse_offset(offset_text.strip())
        if tz is not None:
            stamp = stamp.replace(tzinfo=tz).astimezone(dt.timezone.utc)
        meta["captured"] = stamp.isoformat()
        meta["day"] = (stamp.astimezone(dt.timezone.utc) if stamp.tzinfo
                       else stamp).strftime("%Y-%m-%d")

    lat_v, lat_r = tags.get(2), tags.get(1)
    lon_v, lon_r = tags.get(4), tags.get(3)
    if lat_v and lon_v:
        lat = _dms(lat_v, (lat_r or ["N"])[0] if isinstance(lat_r, list) else "N")
        lon = _dms(lon_v, (lon_r or ["E"])[0] if isinstance(lon_r, list) else "E")
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            meta["gps_lat"] = lat
            meta["gps_lon"] = lon
            alt = tags.get(6)
            if isinstance(alt, list) and alt:
                a = _ratio(alt[0])
                if a is not None:
                    meta["gps_alt"] = round(a, 1)

    for tag, key in ((T_ISO, "iso"), (T_FNUMBER, "fnumber"),
                     (T_EXPOSURE, "exposure"), (T_FOCAL, "focal_mm")):
        val = tags.get(tag)
        if isinstance(val, list) and val:
            r = _ratio(val[0]) if isinstance(val[0], tuple) else val[0]
            if r is not None:
                meta[key] = round(float(r), 4)

    if T_DNG_VERSION in tags:
        meta["dng"] = True

    return meta, max_end


def _parse_offset(text: str):
    """'+08:00' -> tzinfo."""
    try:
        sign = -1 if text[0] == "-" else 1
        body = text[1:].replace(":", "")
        hours = int(body[:2])
        minutes = int(body[2:4] or 0)
        return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    except (ValueError, IndexError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# embedded thumbnails
# --------------------------------------------------------------------------- #
T_THUMB_OFFSET = 0x0201
T_THUMB_LENGTH = 0x0202


def extract_jpeg_thumbnail(data: bytes):
    """Pull the EXIF thumbnail out of a JPEG prefix - no decoder needed.

    Phones and cameras store a 160x120 preview right inside the APP1 segment,
    which makes a 100k file album cheap to build even without Pillow.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):
            return None
        length = (data[pos + 2] << 8) | data[pos + 3]
        if length < 2:
            return None
        if marker == 0xE1 and data[pos + 4: pos + 10] == b"Exif\x00\x00":
            blob = data[pos + 10: pos + 2 + length]
            try:
                thumb = _tiff_thumbnail(blob)
            except (ValueError, struct.error, IndexError):
                thumb = None
            if thumb:
                return thumb
        pos += 2 + length
    return None


def _tiff_thumbnail(blob: bytes):
    t = Tiff(blob)
    entries, next_ifd = t.ifd(t.first)
    if not next_ifd:
        return None
    entries, _ = t.ifd(next_ifd)
    offset = length = None
    for tag, typ, count, eo in entries:
        if tag == T_THUMB_OFFSET:
            val = t.value(typ, count, eo)
            if isinstance(val, list) and val:
                offset = int(val[0])
        elif tag == T_THUMB_LENGTH:
            val = t.value(typ, count, eo)
            if isinstance(val, list) and val:
                length = int(val[0])
    if offset is None or length is None or length < 64:
        return None
    if offset + length > len(blob):
        return None
    thumb = blob[offset: offset + length]
    if thumb[:2] != b"\xff\xd8":
        return None
    return thumb
