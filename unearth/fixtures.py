"""Synthetic test material: a fake disk image with known contents.

Everything the test suite and ``unearth selftest`` needs is built here from
scratch, so the whole tool can be verified on a machine with no spare disk, no
admin rights and no third-party packages.
"""

from __future__ import annotations

import io
import os
import random
import struct
import zipfile

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MIB = 1024 * 1024


def sample(name: str) -> bytes:
    with open(os.path.join(DATA_DIR, name), "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# EXIF
# --------------------------------------------------------------------------- #
def _ifd(entries, offset: int, next_ifd: int = 0) -> bytes:
    """Serialise one IFD. entries: [(tag, type, count, value_bytes_or_int)]."""
    out = bytearray()
    out += struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        out += struct.pack("<HHI", tag, typ, count)
        if isinstance(value, int):
            out += struct.pack("<I", value)
        else:
            out += value.ljust(4, b"\x00")[:4]
    out += struct.pack("<I", next_ifd)
    return bytes(out)


def build_exif(make="Canon", model="Canon EOS 5D Mark III",
               when="2013:08:12 19:22:33", lat=(31, 11, 45.0),
               lon=(121, 26, 12.0), width=4000, height=3000) -> bytes:
    """A complete little-endian EXIF block (payload of an APP1 segment)."""
    ifd0_off = 8
    ifd0_entries = 5
    ifd_exif_off = ifd0_off + 2 + 12 * ifd0_entries + 4
    ifd_exif_entries = 4
    ifd_gps_off = ifd_exif_off + 2 + 12 * ifd_exif_entries + 4
    ifd_gps_entries = 4
    pool = ifd_gps_off + 2 + 12 * ifd_gps_entries + 4

    make_b = make.encode() + b"\x00"
    model_b = model.encode() + b"\x00"
    when_b = when.encode() + b"\x00"
    lat_b = b"".join(struct.pack("<II", int(v * 1), 1) for v in lat)
    lon_b = b"".join(struct.pack("<II", int(v * 1), 1) for v in lon)

    data = bytearray()
    offsets = {}
    for key, blob in (("make", make_b), ("model", model_b), ("when", when_b),
                      ("lat", lat_b), ("lon", lon_b)):
        offsets[key] = pool + len(data)
        data += blob

    ifd0 = _ifd([
        (0x010F, 2, len(make_b), offsets["make"]),
        (0x0110, 2, len(model_b), offsets["model"]),
        (0x0112, 3, 1, 1),
        (0x8769, 4, 1, ifd_exif_off),
        (0x8825, 4, 1, ifd_gps_off),
    ], ifd0_off)

    exif_ifd = _ifd([
        (0x9003, 2, len(when_b), offsets["when"]),
        (0xA002, 4, 1, width),
        (0xA003, 4, 1, height),
        (0x8827, 3, 1, 100),
    ], ifd_exif_off)

    gps_ifd = _ifd([
        (0x0001, 2, 2, b"N\x00"),
        (0x0002, 5, 3, offsets["lat"]),
        (0x0003, 2, 2, b"E\x00"),
        (0x0004, 5, 3, offsets["lon"]),
    ], ifd_gps_off)

    head = bytearray(b"II*\x00" + struct.pack("<I", ifd0_off))
    head += ifd0 + exif_ifd + gps_ifd + data
    return bytes(head)


def jpeg_with_exif(exif: bytes | None = None) -> bytes:
    base = sample("sample.jpg")
    if exif is None:
        exif = build_exif()
    app1 = b"Exif\x00\x00" + exif
    segment = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    return base[:2] + segment + base[2:]


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def make_docx(text: str = "hello unearth") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/content-types"><Default Extension="xml" '
                    'ContentType="application/xml"/></Types>')
        zf.writestr("word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                    'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>%s</w:t>'
                    '</w:r></w:p></w:body></w:document>' % text)
        zf.writestr("docProps/app.xml", "<Properties/>")
    return buf.getvalue()


def make_png(width: int = 64, height: int = 48) -> bytes:
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: none
        for x in range(width):
            raw += bytes(((x * 4) & 0xFF, (y * 4) & 0xFF, 0x80))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def make_gif(width: int = 2, height: int = 2) -> bytes:
    """A minimal single frame GIF87a (2x2, 2 colour palette)."""
    head = b"GIF87a" + struct.pack("<HH", width, height) + bytes([0x80, 0, 0])
    gct = b"\x00\x00\x00\xff\xff\xff"
    desc = b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
    # codes: clear=4, four pixels of colour 0, end=5, 3 bits each, LSB first
    value = 4 | (5 << 15)
    payload = value.to_bytes(3, "little")
    lzw = bytes([2, len(payload)]) + payload + b"\x00"
    return head + gct + desc + lzw + b"\x3b"


def make_bmp(width: int = 16, height: int = 12) -> bytes:
    row = width * 3
    pad = (-row) % 4
    pixels = bytes((x * 7) & 0xFF for _ in range(height) for x in range(row + pad))
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                      len(pixels), 2835, 2835, 0, 0)
    body = dib + pixels
    header = b"BM" + struct.pack("<IHHI", 14 + len(body), 0, 0, 54)
    return header + body


def make_tiff(width: int = 32, height: int = 32) -> bytes:
    """A baseline TIFF with one strip, enough for the size estimator."""
    ifd_entries = 7
    ifd_off = 8
    data_off = ifd_off + 2 + 12 * ifd_entries + 4
    strip = bytes((i * 3) & 0xFF for i in range(width * height))

    def entry(tag, typ, count, value):
        if isinstance(value, bytes):
            return struct.pack("<HHI", tag, typ, count) + value.ljust(4, b"\x00")[:4]
        return struct.pack("<HHII", tag, typ, count, value)

    ifd = struct.pack("<H", ifd_entries)
    ifd += entry(0x0100, 4, 1, width)
    ifd += entry(0x0101, 4, 1, height)
    ifd += entry(0x010F, 2, 6, b"Canon\x00")
    ifd += entry(0x0110, 2, 11, b"ScanTest\x00\x00\x00")
    ifd += entry(0x0111, 4, 1, data_off)
    ifd += entry(0x0117, 4, 1, len(strip))
    ifd += entry(0x0112, 3, 1, 1)
    ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", ifd_off) + ifd + strip


def make_pdf(text: str = "unearth test") -> bytes:
    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    stream = b"BT /F1 18 Tf 72 700 Td (" + text.encode() + b") Tj ET"
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def make_pdf_updated() -> bytes:
    """A PDF after one incremental save: a second section + trailer + %%EOF.

    Carvers that stop at the first %%EOF return the old revision; the current
    revision is the whole byte string.
    """
    base = make_pdf()
    section = b"2 0 obj\n<< /Type /X >>\nendobj\n"
    xref_off = len(base) + 1 + len(section)   # +1: the separator newline
    update = (section + b"xref\n0 0\ntrailer<<>>\nstartxref\n"
              + str(xref_off).encode() + b"\n%%EOF\n")
    return base + b"\n" + update


def make_ole() -> bytes:
    """A 3 sector compound file whose directory says 'WordDocument'."""
    sector = 512
    header = bytearray(sector)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HH", header, 0x18, 0x003E, 0x0003)   # minor, major
    struct.pack_into("<H", header, 0x1C, 0xFFFE)            # byte order
    struct.pack_into("<H", header, 0x1E, 9)                 # sector shift
    struct.pack_into("<H", header, 0x20, 6)                 # mini sector shift
    struct.pack_into("<I", header, 0x2C, 1)                 # FAT sectors
    struct.pack_into("<I", header, 0x30, 1)                 # first dir sector
    struct.pack_into("<I", header, 0x38, 4096)              # mini cutoff
    struct.pack_into("<I", header, 0x3C, 0xFFFFFFFE)        # first mini FAT
    struct.pack_into("<I", header, 0x40, 0)                 # mini FAT sectors
    struct.pack_into("<I", header, 0x44, 0xFFFFFFFE)        # first DIFAT
    struct.pack_into("<I", header, 0x48, 0)                 # DIFAT sectors
    struct.pack_into("<I", header, 0x4C, 0)                 # DIFAT[0] = sector 0
    for i in range(1, 109):
        struct.pack_into("<I", header, 0x4C + 4 * i, 0xFFFFFFFF)

    fat = bytearray(sector)
    struct.pack_into("<I", fat, 0, 0xFFFFFFFD)   # sector 0 is the FAT itself
    struct.pack_into("<I", fat, 4, 0xFFFFFFFE)   # sector 1 directory, end
    struct.pack_into("<I", fat, 8, 0xFFFFFFFE)   # sector 2 data, end
    for i in range(3, sector // 4):
        struct.pack_into("<I", fat, 4 * i, 0xFFFFFFFF)

    def dir_entry(name: str, kind: int, right: int = 0xFFFFFFFF, child: int = 0xFFFFFFFF):
        b = bytearray(128)
        raw = name.encode("utf-16-le") + b"\x00\x00"
        b[0:len(raw)] = raw
        struct.pack_into("<H", b, 64, len(raw))
        b[66] = kind        # 5 = root, 2 = stream
        b[67] = 1
        struct.pack_into("<I", b, 68, right)
        struct.pack_into("<I", b, 72, child)
        return bytes(b)

    directory = bytearray(sector)
    directory[0:128] = dir_entry("Root Entry", 5, child=1)
    directory[128:256] = dir_entry("WordDocument", 2)
    directory[256:384] = dir_entry("\x05SummaryInformation", 2)

    body = bytes(header) + bytes(fat) + bytes(directory) + b"\x00" * sector
    return body


# --------------------------------------------------------------------------- #
# NTFS
# --------------------------------------------------------------------------- #
def _filetime(y, mo, d, h=0, mi=0, s=0) -> int:
    import datetime as dt
    epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    when = dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc)
    return int((when - epoch).total_seconds() * 10_000_000)


def _attr_resident(atype: int, value: bytes, attr_id: int = 0) -> bytes:
    length = 24 + len(value)
    pad = (-length) % 8
    out = bytearray(length + pad)
    struct.pack_into("<II", out, 0, atype, len(out))
    out[8] = 0                     # resident
    out[9] = 0
    struct.pack_into("<H", out, 10, 0)
    struct.pack_into("<H", out, 12, 0)
    struct.pack_into("<H", out, 14, attr_id)
    struct.pack_into("<I", out, 16, len(value))
    struct.pack_into("<H", out, 20, 24)
    out[22] = 0
    out[23] = 0
    out[24:24 + len(value)] = value
    return bytes(out)


def _encode_runlist(runs) -> bytes:
    out = bytearray()
    prev = 0
    for lcn, length in runs:
        lb = length.to_bytes(max(1, (length.bit_length() + 7) // 8), "little")
        delta = lcn - prev
        db = bytearray(delta.to_bytes(8, "little", signed=True))
        n = 8
        while n > 1:
            if db[n - 1] == 0x00 and not (db[n - 2] & 0x80):
                n -= 1
            elif db[n - 1] == 0xFF and (db[n - 2] & 0x80):
                n -= 1
            else:
                break
        db = bytes(db[:n])
        out.append((len(lb) & 0x0F) | ((len(db) & 0x0F) << 4))
        out += lb
        out += db
        prev = lcn
    out.append(0)
    return bytes(out)


def _attr_nonresident(atype: int, runs: list, real_size: int,
                      cluster_size: int, attr_id: int = 0) -> bytes:
    runlist = _encode_runlist(runs)
    header_len = 64
    length = header_len + len(runlist)
    pad = (-length) % 8
    out = bytearray(length + pad)
    struct.pack_into("<II", out, 0, atype, len(out))
    out[8] = 1
    struct.pack_into("<H", out, 10, 0)
    struct.pack_into("<H", out, 14, attr_id)
    struct.pack_into("<QQ", out, 16, 0, 0)
    struct.pack_into("<H", out, 32, header_len)
    struct.pack_into("<H", out, 34, 0)
    alloc = sum(n for _, n in runs) * cluster_size
    struct.pack_into("<QQQ", out, 40, alloc, real_size, real_size)
    out[header_len:header_len + len(runlist)] = runlist
    return bytes(out)


def make_mft_record(index: int, name: str, parent: int, flags: int,
                    times, data_runs=None, size=0, cluster_size=4096,
                    record_size=1024) -> bytes:
    created, modified = times
    si = bytearray(72)
    struct.pack_into("<QQQQ", si, 0, created, modified, modified, modified)
    struct.pack_into("<I", si, 32, 0x20 if flags & 2 else 0x80)
    attrs = _attr_resident(0x10, bytes(si), 0)

    fname = bytearray(66 + len(name) * 2)
    struct.pack_into("<Q", fname, 0, parent)
    struct.pack_into("<QQQQ", fname, 8, created, modified, modified, modified)
    struct.pack_into("<QQ", fname, 40, size, size)
    struct.pack_into("<I", fname, 56, 0x10000000 if flags & 2 else 0x20)
    fname[64] = len(name)
    fname[65] = 1
    fname[66:] = name.encode("utf-16-le")
    attrs += _attr_resident(0x30, bytes(fname), 1)

    if data_runs:
        attrs += _attr_nonresident(0x80, data_runs, size, cluster_size, 2)
    elif size:
        attrs += _attr_resident(0x80, b"\x00" * size, 2)
    attrs += struct.pack("<II", 0xFFFFFFFF, 0)

    rec = bytearray(record_size)
    rec[0:4] = b"FILE"
    usa_off, usa_count = 48, 1 + record_size // 512
    struct.pack_into("<HH", rec, 4, usa_off, usa_count)
    struct.pack_into("<Q", rec, 0x08, index)      # LSN
    struct.pack_into("<H", rec, 0x10, 1)          # sequence
    struct.pack_into("<H", rec, 0x12, 1)          # hard links
    struct.pack_into("<H", rec, 0x14, 56)         # first attr offset
    struct.pack_into("<H", rec, 0x16, flags)
    struct.pack_into("<I", rec, 0x18, 56 + len(attrs))
    struct.pack_into("<I", rec, 0x1C, record_size)
    struct.pack_into("<Q", rec, 0x20, 0)
    struct.pack_into("<H", rec, 0x28, 4)
    rec[56:56 + len(attrs)] = attrs

    # apply the update sequence array
    usn = b"\x07\x00"
    rec[usa_off:usa_off + 2] = usn
    for i in range(1, usa_count):
        end = i * 512
        rec[usa_off + i * 2: usa_off + i * 2 + 2] = rec[end - 2:end]
        rec[end - 2:end] = usn
    return bytes(rec)


def make_ntfs_image(cluster_size=4096, sector_size=512) -> bytes:
    """A tiny NTFS volume: boot sector, 4 MFT records, two deleted photos."""
    spc = cluster_size // sector_size
    mft_lcn = 4
    data_lcn = 10
    total_clusters = 40
    total = total_clusters * cluster_size
    image = bytearray(total)

    boot = bytearray(512)
    boot[0:3] = b"\xeb\x52\x90"
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 0x0B, sector_size)
    boot[0x0D] = spc
    struct.pack_into("<Q", boot, 0x28, total // sector_size)
    struct.pack_into("<Q", boot, 0x30, mft_lcn)
    struct.pack_into("<Q", boot, 0x38, mft_lcn + 1)
    struct.pack_into("<b", boot, 0x40, -10)      # 1024 byte records
    struct.pack_into("<b", boot, 0x44, 1)
    struct.pack_into("<Q", boot, 0x48, 0x1122334455667788)
    boot[510:512] = b"\x55\xaa"
    image[0:512] = boot

    photo = jpeg_with_exif()
    note = b"unearth: a deleted note file.\n" * 20

    records = [
        make_mft_record(0, "$MFT", 5, 1, (_filetime(2019, 1, 1), _filetime(2019, 1, 1)),
                        data_runs=[(mft_lcn, 1)], size=cluster_size),
        make_mft_record(1, "DCIM", 5, 3, (_filetime(2012, 5, 1), _filetime(2012, 5, 1)),
                        size=0),
        make_mft_record(2, "IMG_1234.JPG", 1, 0,
                        (_filetime(2013, 8, 12, 19, 22, 33),
                         _filetime(2014, 2, 3, 11, 5, 1)),
                        data_runs=[(data_lcn, 3)], size=len(photo)),
        make_mft_record(3, "游记草稿.txt", 1, 0,
                        (_filetime(2011, 3, 4), _filetime(2011, 3, 4)), size=len(note)),
    ]
    mft_off = mft_lcn * cluster_size
    for i, rec in enumerate(records):
        image[mft_off + i * 1024: mft_off + (i + 1) * 1024] = rec

    end = data_lcn * cluster_size + len(photo)
    image[data_lcn * cluster_size: end] = photo
    return bytes(image)


# --------------------------------------------------------------------------- #
# a whole fake disk for the end to end demo
# --------------------------------------------------------------------------- #
def build_demo_image(total_size: int = 24 * MIB, seed: int = 7,
                     varied: bool = True) -> bytes:
    """Random "unallocated" space with a believable scatter of files inside.

    With ``varied`` the photos carry different capture dates, cameras and GPS
    coordinates, so the generated album looks like a real (small) timeline
    instead of one day of one hundred identical files.
    """
    rng = random.Random(seed)
    photo = jpeg_with_exif()
    video = sample("sample.mp4")
    plain = sample("sample.jpg")
    doc = make_docx()
    pdf = make_pdf()
    ole = make_ole()

    variants = [photo, plain]
    if varied:
        cameras = [
            ("Canon", "Canon EOS 5D Mark III"),
            ("Apple", "iPhone 6"),
            ("NIKON CORPORATION", "NIKON D7000"),
            ("Xiaomi", "MI 8"),
            ("SONY", "ILCE-7M2"),
        ]
        for i in range(14):
            year = 2009 + (i * 3) % 10
            month = 1 + (i * 5) % 12
            day = 1 + (i * 7) % 27
            hour = (i * 3) % 24
            make, model = cameras[i % len(cameras)]
            exif = build_exif(
                make=make, model=model,
                when="%04d:%02d:%02d %02d:%02d:00" % (year, month, day, hour, (i * 13) % 60),
                lat=(30.0 + i * 0.37, 10, 0.0),
                lon=(120.0 + i * 0.41, 20, 0.0),
            )
            variants.append(jpeg_with_exif(exif))

    fills = variants + [video, doc, pdf, ole, variants[3], video]
    image = bytearray(total_size)
    # fill with low-entropy junk so magic bytes do not appear by accident
    chunk = bytes(rng.getrandbits(8) & 0x1F for _ in range(4096))
    for i in range(0, total_size, 4096):
        image[i:i + 4096] = chunk

    positions = []
    cursor = 64 * 1024
    while cursor < total_size - 2 * MIB:
        blob = fills[len(positions) % len(fills)]
        image[cursor: cursor + len(blob)] = blob
        positions.append((cursor, len(blob)))
        cursor += len(blob) + rng.randrange(8 * 1024, 384 * 1024)
    return bytes(image)
