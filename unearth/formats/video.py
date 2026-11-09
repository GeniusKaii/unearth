"""Video and audio carvers: MP4 / MOV / 3GP / M4A, AVI, WAV, WebP.

Video files are the ones people care about most and the ones carving gets wrong
most often, because the index (``moov``) is frequently written *after* the
payload.  The box walker therefore measures the real extent of consecutive
top-level boxes and reports ``complete=False`` when the index is missing, so a
half recovered clip is labelled instead of silently claiming success.
"""

from __future__ import annotations

from ..util import qt_to_dt
from .common import CarveResult, u16, u32, u32be, u64be
from .images import _box_type_ok

MIB = 1024 * 1024

MOV_BRANDS = {b"qt  ", b"moov"}
MP4_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash", b"mmp4",
              b"MSNV", b"F4V ", b"F4P ", b"NDAS"}
PHONE_BRANDS = {b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3gs7", b"3g2a", b"3g2b"}
AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}
ALL_BRANDS = MOV_BRANDS | MP4_BRANDS | PHONE_BRANDS | AUDIO_BRANDS


# --------------------------------------------------------------------------- #
# ISO base media file format helpers
# --------------------------------------------------------------------------- #
def iter_boxes(blob: bytes, start: int = 0):
    """Yield (offset, size, type) for sibling boxes inside ``blob``."""
    pos = start
    while pos + 8 <= len(blob):
        size = u32be(blob, pos)
        btype = blob[pos + 4: pos + 8]
        if not _box_type_ok(btype):
            return
        header = 8
        if size == 1:
            if pos + 16 > len(blob):
                return
            size = u64be(blob, pos + 8)
            header = 16
        elif size == 0:
            size = len(blob) - pos
        if size < header or pos + size > len(blob):
            return
        yield pos, size, btype
        pos += size


def _mvhd(blob: bytes) -> dict:
    if len(blob) < 28:
        return {}
    version = blob[8]
    try:
        if version == 1:
            if len(blob) < 40:
                return {}
            created = u64be(blob, 12)
            timescale = u32be(blob, 28)
            duration = u64be(blob, 32)
        else:
            created = u32be(blob, 12)
            timescale = u32be(blob, 20)
            duration = u32be(blob, 24)
    except IndexError:
        return {}
    out = {}
    if timescale:
        if duration not in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            out["duration"] = round(duration / float(timescale), 3)
    if 0 < created < 0x7FFFFFFF:
        stamp = qt_to_dt(created)
        if stamp:
            out["captured"] = stamp.isoformat()
            out["day"] = stamp.strftime("%Y-%m-%d")
    return out


def _tkhd(blob: bytes) -> dict:
    if len(blob) < 92:
        return {}
    version = blob[8]
    offset = 84 if version == 0 else 96
    if len(blob) < offset + 8:
        return {}
    width = u32be(blob, offset) >> 16
    height = u32be(blob, offset + 4) >> 16
    if 0 < width <= 20000 and 0 < height <= 20000:
        return {"width": width, "height": height}
    return {}


def _moov_meta(blob: bytes) -> dict:
    out: dict = {}
    for pos, size, btype in iter_boxes(blob, 8):
        box = blob[pos: pos + size]
        if btype == b"mvhd":
            out.update(_mvhd(box))
        elif btype == b"trak":
            for p2, s2, t2 in iter_boxes(blob, pos + 8):
                if t2 == b"tkhd":
                    cand = _tkhd(blob[p2: p2 + s2])
                    if cand.get("width") and not out.get("width"):
                        out.update(cand)
                    elif cand.get("width") and out.get("width"):
                        # keep the largest track (cover art / thumbnails are smaller)
                        if cand["width"] * cand["height"] > out["width"] * out["height"]:
                            out.update(cand)
    return out


def carve_isobmff(reader, off: int):
    head = reader.read(off, 16)
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    brand = head[8:12]
    if brand not in ALL_BRANDS:
        return None
    end = off + reader.remaining(off)
    pos = off
    last = off
    meta: dict = {"brand": brand.decode("latin-1").strip()}
    saw_moov = False
    saw_mdat = False
    count = 0
    while pos + 8 <= end and count < 30000:
        hdr = reader.read(pos, 8)
        if len(hdr) < 8:
            break
        size = u32be(hdr, 0)
        btype = hdr[4:8]
        if not _box_type_ok(btype):
            break
        header = 8
        if size == 1:
            ext = reader.read(pos + 8, 8)
            if len(ext) < 8:
                break
            size = u64be(ext, 0)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        count += 1
        if btype == b"moov":
            saw_moov = True
            blob = reader.read(pos, min(size, 256 * 1024))
            meta.update({k: v for k, v in _moov_meta(blob).items() if v is not None})
        elif btype == b"mdat":
            saw_mdat = True
        pos += size
        last = pos
    if count < 2 or last <= off:
        return None
    size = last - off
    if size < 2048:
        return None

    if brand in AUDIO_BRANDS:
        kind, ext = "audio", "m4a"
    elif brand in PHONE_BRANDS:
        kind, ext = "video", "3gp"
    elif brand in MOV_BRANDS:
        kind, ext = "video", "mov"
    else:
        kind, ext = "video", "mp4"
    meta["kind"] = kind
    meta["ext"] = ext
    warnings = []
    complete = True
    if not saw_moov:
        complete = False
        warnings.append("no moov index: partially recovered clip")
    if not saw_mdat:
        warnings.append("no mdat: metadata only")
    return CarveResult(size=size, complete=complete, meta=meta, warnings=warnings)


# --------------------------------------------------------------------------- #
# RIFF: AVI / WAV / WebP
# --------------------------------------------------------------------------- #
def carve_riff(reader, off: int):
    head = reader.read(off, 12)
    if len(head) < 12 or head[:4] != b"RIFF":
        return None
    size = u32(head, 4)
    form = head[8:12]
    total = 8 + size
    if total < 64:
        return None
    if total > reader.remaining(off):
        total = reader.remaining(off)
        complete = False
    else:
        complete = True
    meta: dict = {}
    if form == b"AVI ":
        kind, ext = "video", "avi"
    elif form == b"WAVE":
        kind, ext = "audio", "wav"
    elif form == b"WEBP":
        kind, ext = "photo", "webp"
        chunk = reader.read(off + 12, 30)
        if chunk[0:4] == b"VP8X" and len(chunk) >= 30:
            meta["width"] = 1 + int.from_bytes(chunk[12:15], "little")
            meta["height"] = 1 + int.from_bytes(chunk[15:18], "little")
        elif chunk[0:4] == b"VP8 " and len(chunk) >= 20:
            meta["width"] = u16(chunk, 14) & 0x3FFF
            meta["height"] = u16(chunk, 16) & 0x3FFF
        elif chunk[0:4] == b"VP8L":
            bits = int.from_bytes(chunk[9:13], "little")
            meta["width"] = (bits & 0x3FFF) + 1
            meta["height"] = ((bits >> 14) & 0x3FFF) + 1
    else:
        return None
    if total < 1024 and form != b"WEBP":
        return None
    meta["kind"] = kind
    meta["ext"] = ext
    return CarveResult(size=total, complete=complete, meta=meta)
