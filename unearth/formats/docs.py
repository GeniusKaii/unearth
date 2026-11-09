"""Document carvers: PDF, ZIP (and the OOXML office formats inside it), OLE.

Work files are the second act of this tool: years of ``.docx`` / ``.xlsx`` /
``.pptx`` / ``.pdf`` are exactly the "lost work" that people pay for.  The ZIP
carver walks the local headers and the central directory, so it recovers the
real file name list and knows exactly where the archive ends.
"""

from __future__ import annotations

from .common import CarveResult, u16, u32

MIB = 1024 * 1024

ZIP_LOCAL = b"PK\x03\x04"
ZIP_CENTRAL = b"PK\x01\x02"
ZIP_EOCD = b"PK\x05\x06"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _pdf_eol_len(reader, end: int) -> int:
    """Bytes of EOL right after an %%EOF marker (0, 1 or 2)."""
    tail = reader.read(end, 2)
    if tail[:2] == b"\r\n":
        return 2
    if tail[:1] in (b"\n", b"\r"):
        return 1
    return 0


def _pdf_trailer_valid(reader, off: int, eof_end: int) -> bool:
    """True when this %%EOF belongs to a real trailer.

    A PDF trailer is ``startxref\\n<offset>\\n%%EOF`` where the offset points
    at an xref table (or xref stream object) *inside* the file.  Checking that
    is what lets us pick the LAST %%EOF of an incrementally saved document
    without running past the file into whatever sits next on disk.
    """
    window = reader.read(max(off, eof_end - 133), 133)
    if not window:
        return False
    k = window.rfind(b"startxref")
    if k < 0:
        return False
    rest = window[k + 9:]
    j = 0
    while j < len(rest) and rest[j:j + 1] in (b" ", b"\r", b"\n", b"\t"):
        j += 1
    d = j
    while d < len(rest) and 0x30 <= rest[d] <= 0x39:
        d += 1
    digits = rest[j:d]
    if not digits:
        return False
    target = off + int(digits)
    if not (off < target < eof_end):
        return False
    peek = reader.read(target, 16)
    if not peek:
        return False
    return peek.startswith(b"xref") or b" obj" in peek


def carve_pdf(reader, off: int):
    """Measure a PDF up to its LAST trailer.

    Incrementally saved PDFs (every "save" in a viewer appends a new xref +
    trailer) contain several ``%%EOF`` markers; stopping at the first one
    truncates the document at an old revision.  So we keep scanning and take
    the last %%EOF that has a valid ``startxref`` in front of it.  Scanning
    stops at the next ``%PDF-`` header so an adjacent document is never
    swallowed (tradeoff: a literal ``%PDF-`` inside an *uncompressed* stream
    of this file would end the measurement early - rare in practice).
    """
    if not reader.read(off, 5) == b"%PDF-":
        return None
    pos = off + 5
    cap = min(reader.limit, off + 512 * MIB)
    best_end = -1         # end of the last %%EOF with a valid trailer
    fallback_end = -1     # end of the last %%EOF before the next document
    while pos < cap:
        chunk = reader.read(pos, MIB)
        if not chunk:
            break
        limit = len(chunk)
        j = chunk.find(b"%PDF-")           # a new document starts here
        if j >= 0:
            limit = min(limit, j)
        i = chunk.find(b"%%EOF", 0, limit)
        while i >= 0:
            end = pos + i + 5
            if end > fallback_end:
                fallback_end = end
            if end > best_end and _pdf_trailer_valid(reader, off, end):
                best_end = end
            i = chunk.find(b"%%EOF", i + 1, limit)
        if j >= 0:
            break
        if len(chunk) < MIB:
            break
        pos += MIB - 8
    end = best_end if best_end > 0 else fallback_end
    if end < 0:
        return None
    size = end - off + _pdf_eol_len(reader, end)
    if size < 128:
        return None
    return CarveResult(size=size, complete=True, meta={"kind": "doc"})


# --------------------------------------------------------------------------- #
# ZIP / OOXML
# --------------------------------------------------------------------------- #
def _zip_probe(reader, pos: int) -> int:
    """Find the next ZIP structure header, -1 when this is not a ZIP."""
    start = pos
    stop = pos + 8 * MIB
    while start < stop:
        chunk = reader.read(start, MIB)
        if not chunk:
            return -1
        best = -1
        for sig in (ZIP_LOCAL, ZIP_CENTRAL, ZIP_EOCD):
            i = chunk.find(sig)
            if i >= 0 and (best < 0 or i < best):
                best = i
        if best >= 0:
            return start + best
        if len(chunk) < MIB:
            return -1
        start += MIB - 3
    return -1


def classify_zip(names: list) -> tuple:
    """(kind, ext) for an archive, recognising the OOXML office formats."""
    low = [n.lower() for n in names]
    has_types = "[content_types].xml" in low
    if has_types and any(n.startswith("word/") for n in low):
        return "doc", "docx"
    if has_types and any(n.startswith("xl/") for n in low):
        return "doc", "xlsx"
    if has_types and any(n.startswith("ppt/") for n in low):
        return "doc", "pptx"
    if has_types and any(n.startswith("xl/") or n.startswith("docprops/") for n in low):
        return "doc", "xlsx"
    return "doc", "zip"


def carve_zip(reader, off: int):
    if reader.read(off, 4) != ZIP_LOCAL:
        return None
    end = off + reader.remaining(off)
    pos = off
    names: list = []
    for _ in range(20000):
        sig = reader.read(pos, 4)
        if sig == ZIP_LOCAL:
            hdr = reader.read(pos + 4, 26)
            if len(hdr) < 26:
                return None
            flags = u16(hdr, 2)
            csize = u32(hdr, 14)
            nlen = u16(hdr, 22)
            elen = u16(hdr, 24)
            if nlen > 4096 or elen > 4096:
                return None
            name = reader.read(pos + 30, nlen).decode("utf-8", "replace")
            body = pos + 30 + nlen + elen
            if name:
                names.append(name)
            if flags & 0x0008:      # sizes live in a trailing data descriptor
                nxt = _zip_probe(reader, body)
                if nxt < 0:
                    return None
                pos = nxt
            else:
                pos = body + csize
        elif sig == ZIP_CENTRAL:
            hdr = reader.read(pos + 4, 42)
            if len(hdr) < 42:
                return None
            nlen = u16(hdr, 24)
            elen = u16(hdr, 26)
            clen = u16(hdr, 28)
            pos = pos + 46 + nlen + elen + clen
        elif sig == ZIP_EOCD:
            eocd = reader.read(pos, 22)
            if len(eocd) < 22:
                return None
            clen = u16(eocd, 20)
            size = pos + 22 + clen - off
            if size < 64 or size > reader.remaining(off):
                return None
            kind, ext = classify_zip(names)
            return CarveResult(size=size, complete=True, meta={
                "kind": kind, "ext": ext, "entries": len(names),
                "names": names[:20],
            })
        else:
            return None
        if pos > end or pos < off:
            return None
    return None


# --------------------------------------------------------------------------- #
# OLE compound file (legacy .doc / .xls / .ppt / .msg)
# --------------------------------------------------------------------------- #
def _ole_dir_names(reader, start: int, length: int) -> str:
    blob = reader.read(start, min(length, 64 * 1024))
    out = []
    for i in range(0, len(blob) - 127, 128):
        name_len = u16(blob, i + 64)
        if not 2 <= name_len <= 64:
            continue
        raw = blob[i: i + name_len - 2]
        try:
            out.append(raw.decode("utf-16-le", "replace"))
        except UnicodeDecodeError:
            continue
    return " ".join(out)


def _ole_ext(names: str) -> str:
    if "WordDocument" in names:
        return "doc"
    if "PowerPoint" in names:
        return "ppt"
    if "Workbook" in names or " Book" in names or names.startswith("Book"):
        return "xls"
    if "__substg1.0_" in names:
        return "msg"
    if "VisioDocument" in names:
        return "vsd"
    return "doc"


def carve_ole(reader, off: int):
    head = reader.read(off, 512)
    if len(head) < 512 or head[:8] != OLE_MAGIC:
        return None
    sector_shift = u16(head, 0x1E)
    if sector_shift not in (9, 12):
        return None
    sector = 1 << sector_shift
    num_fat = u32(head, 0x2C)
    first_dir = u32(head, 0x30)
    if num_fat > 65536:
        return None

    fat_sectors = []
    for i in range(109):
        if len(fat_sectors) >= num_fat:
            break
        val = u32(head, 0x4C + 4 * i)
        if val not in (0xFFFFFFFF, 0xFFFFFFFE):
            fat_sectors.append(val)

    # A sector is in use when its FAT entry is anything but FREE; the last
    # sector of a chain holds ENDOFCHAIN and has no back-reference, so reading
    # the table itself is the only way to see it.
    per_sector = sector // 4
    max_sector = -1
    for fs in fat_sectors[:8192]:
        data = reader.read(off + (fs + 1) * sector, sector)
        if len(data) < 4:
            continue
        for j in range(0, len(data) - 3, 4):
            val = u32(data, j)
            if val != 0xFFFFFFFF:
                index = fs * per_sector + j // 4
                if index > max_sector:
                    max_sector = index
    if max_sector < 1:
        return None
    size = (max_sector + 2) * sector
    if size > reader.remaining(off):
        return None

    names = ""
    if first_dir < 0xFFFFFFFA:
        names = _ole_dir_names(reader, off + (first_dir + 1) * sector, min(sector * 4, 64 * 1024))
    ext = _ole_ext(names)
    return CarveResult(size=size, complete=True, meta={
        "kind": "doc", "ext": ext, "streams": names[:200],
    })
