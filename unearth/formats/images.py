"""Still image carvers: JPEG, PNG, GIF, BMP, TIFF/RAW and HEIF/AVIF."""

from __future__ import annotations

from .common import CarveResult, u16, u16be, u32, u32be, s32, u64be
from .exif import parse_tiff

MIB = 1024 * 1024

JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1",
               b"msf1", b"avif", b"avis"}


# --------------------------------------------------------------------------- #
# JPEG
# --------------------------------------------------------------------------- #
def carve_jpeg(reader, off: int):
    if reader.read(off, 3) != b"\xff\xd8\xff":
        return None
    pos = off + 2
    meta: dict = {}
    width = height = None
    limit = off + MIB  # 1 MiB of headers is already absurd
    while pos < limit:
        head = reader.read(pos, 2)
        if len(head) < 2 or head[0] != 0xFF:
            return None
        marker = head[1]
        if marker == 0xFF:            # fill byte
            pos += 1
            continue
        if marker == 0xD8:            # stray SOI
            pos += 2
            continue
        if marker == 0xD9:            # EOI before any scan: not a photo
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDA:            # start of scan -> entropy coded data
            seg = reader.read(pos + 2, 2)
            if len(seg) < 2:
                return None
            ln = u16be(seg, 0)
            if ln < 2:
                return None
            return _jpeg_scan_eoi(reader, off, pos + 2 + ln, meta, width, height)
        seg = reader.read(pos + 2, 2)
        if len(seg) < 2:
            return None
        ln = u16be(seg, 0)
        if ln < 2:
            return None
        if marker in JPEG_SOF:
            sof = reader.read(pos + 4, 5)
            if len(sof) < 5:
                return None
            height = u16be(sof, 1)
            width = u16be(sof, 3)
        elif marker == 0xE1 and ln <= 0x10003:
            app1 = reader.read(pos + 4, min(ln - 2, 0x10000))
            if app1[:6] == b"Exif\x00\x00":
                exif_meta, _ = parse_tiff(app1[6:])
                for k, v in exif_meta.items():
                    meta.setdefault(k, v)
        pos += 2 + ln
    return None


def _jpeg_scan_eoi(reader, off: int, data_start: int, meta: dict,
                   width, height):
    """Scan entropy coded data for EOI, stopping early at the next SOI."""
    meta = dict(meta)
    if width:
        meta["width"] = int(width)
        meta["height"] = int(height)
    pos = data_start
    block = 2 * MIB
    cap = min(reader.limit, off + 2 * 1024 * MIB)
    step = block - 1  # 1 byte overlap so a marker split across reads is found
    while pos < cap:
        chunk = reader.read(pos, block)
        if not chunk:
            break
        i = chunk.find(b"\xff\xd9")
        soi = chunk.find(b"\xff\xd8\xff")
        if i >= 0 and (soi < 0 or i < soi):
            size = pos + i + 2 - off
            if size < 128:
                return None
            return CarveResult(size=size, complete=True, meta=meta)
        if soi >= 0:
            size = pos + soi - off
            if size < 128:
                return None
            return CarveResult(size=size, complete=False, meta=meta,
                               warnings=["truncated: next SOI before EOI"])
        if len(chunk) < block:
            break
        pos += step
    return None


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #
def carve_png(reader, off: int):
    if reader.read(off, 8) != PNG_MAGIC:
        return None
    pos = off + 8
    meta: dict = {}
    first = True
    end_cap = off + reader.remaining(off)
    while pos + 8 <= end_cap:
        hdr = reader.read(pos, 8)
        if len(hdr) < 8:
            return None
        length = u32be(hdr, 0)
        ctype = hdr[4:8]
        if length > 64 * MIB:
            return None
        if first and ctype != b"IHDR":
            return None
        first = False
        if ctype == b"IHDR":
            data = reader.read(pos + 8, 13)
            if len(data) < 13:
                return None
            meta["width"] = u32be(data, 0)
            meta["height"] = u32be(data, 4)
        elif ctype == b"eXIf":
            exif_meta, _ = parse_tiff(reader.read(pos + 8, length))
            meta.update({k: v for k, v in exif_meta.items() if k not in meta})
        if ctype == b"IEND":
            size = pos + 12 - off
            return CarveResult(size=size, complete=True, meta=meta)
        pos += 12 + length
    return None


# --------------------------------------------------------------------------- #
# GIF
# --------------------------------------------------------------------------- #
def _skip_gif_blocks(reader, pos: int, end: int) -> int:
    for _ in range(100000):
        b = reader.read(pos, 1)
        if not b:
            return -1
        n = b[0]
        pos += 1
        if n == 0:
            return pos
        pos += n
        if pos > end:
            return -1
    return -1


def carve_gif(reader, off: int):
    hdr = reader.read(off, 6)
    if hdr not in (b"GIF87a", b"GIF89a"):
        return None
    lsd = reader.read(off + 6, 7)
    if len(lsd) < 7:
        return None
    meta = {"width": u16(lsd, 0), "height": u16(lsd, 2)}
    flags = lsd[4]
    pos = off + 13
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    end = off + min(reader.remaining(off), 64 * MIB)
    frames = 0
    for _ in range(100000):
        b = reader.read(pos, 1)
        if not b:
            return None
        tag = b[0]
        if tag == 0x3B:  # trailer
            return CarveResult(size=pos + 1 - off, complete=True, meta=meta)
        if tag == 0x21:  # extension
            pos = _skip_gif_blocks(reader, pos + 2, end)
            if pos < 0:
                return None
        elif tag == 0x2C:  # image descriptor
            desc = reader.read(pos + 1, 10)
            if len(desc) < 10:
                return None
            lflags = desc[9]
            pos += 10
            if lflags & 0x80:
                pos += 3 * (2 ** ((lflags & 0x07) + 1))
            pos += 1  # LZW minimum code size
            pos = _skip_gif_blocks(reader, pos, end)
            if pos < 0:
                return None
            frames += 1
        else:
            return None
        if pos > end:
            return None
    return None


# --------------------------------------------------------------------------- #
# BMP
# --------------------------------------------------------------------------- #
def carve_bmp(reader, off: int):
    head = reader.read(off, 30)
    if len(head) < 30 or head[:2] != b"BM":
        return None
    fsize = u32(head, 2)
    pix_off = u32(head, 10)
    dib = u32(head, 14)
    if dib not in (12, 40, 52, 56, 64, 108, 124):
        return None
    if not (64 <= fsize <= reader.remaining(off)):
        return None
    w = abs(s32(head, 18))
    h = abs(s32(head, 22))
    if not (0 < w <= 100000 and 0 < h <= 100000):
        return None
    if pix_off > fsize:
        return None
    return CarveResult(size=fsize, complete=True,
                       meta={"width": w, "height": h})


# --------------------------------------------------------------------------- #
# TIFF / RAW
# --------------------------------------------------------------------------- #
def _raw_ext(meta: dict, head: bytes) -> str:
    if head[8:10] == b"CR" and head[10] == 2:
        return "cr2"
    if meta.get("dng"):
        return "dng"
    make = (meta.get("make") or "").upper()
    model = (meta.get("model") or "").upper()
    if "NIKON" in make:
        return "nef"
    if "SONY" in make:
        return "arw"
    if "OLYMPUS" in make or "OM Digital" in make:
        return "orf"
    if "PANASONIC" in make:
        return "rw2"
    if "PENTAX" in make or "RICOH" in make:
        return "pef"
    if "CANON" in make and "CR" in model:
        return "cr2"
    return "tif"


def carve_tiff(reader, off: int):
    head = reader.read(off, 16)
    if len(head) < 8:
        return None
    if head[:2] not in (b"II", b"MM"):
        return None
    if head[:4] not in (b"II*\x00", b"MM\x00*"):
        return None
    prefix = reader.read(off, min(8 * MIB, reader.remaining(off)))
    meta, max_end = parse_tiff(prefix)
    if max_end < 1024:
        return None  # no strip/tile geometry: cannot measure safely
    size = max_end
    if size > reader.remaining(off):
        return None
    meta["raw_ext"] = _raw_ext(meta, head)
    return CarveResult(size=size, complete=True, meta=meta)


# --------------------------------------------------------------------------- #
# HEIF / AVIF
# --------------------------------------------------------------------------- #
def _box_type_ok(t: bytes) -> bool:
    return len(t) == 4 and all(32 <= c < 127 for c in t)


def carve_heif(reader, off: int):
    head = reader.read(off, 12)
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    brand = head[8:12]
    if brand not in HEIF_BRANDS:
        return None
    pos = off
    end = off + reader.remaining(off)
    meta: dict = {"brand": brand.decode("latin-1")}
    last = off
    count = 0
    while pos + 8 <= end and count < 20000:
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
        pos += size
        last = pos
    if count < 2:
        return None
    size = last - off
    if size < 1024:
        return None
    ext = "avif" if brand in (b"avif", b"avis") else "heic"
    meta["ext"] = ext
    return CarveResult(size=size, complete=True, meta=meta)
