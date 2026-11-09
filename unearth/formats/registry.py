"""The signature table: what to look for, in what order, for which profile."""

from __future__ import annotations

from .common import Signature
from .docs import OLE_MAGIC, carve_ole, carve_pdf, carve_zip
from .images import (PNG_MAGIC, carve_bmp, carve_gif, carve_heif, carve_jpeg,
                     carve_png, carve_tiff)
from .video import carve_isobmff, carve_riff

MIB = 1024 * 1024

# Order matters only for reporting; every signature is tried on every chunk.
SIGNATURES: list = [
    # --- photos -----------------------------------------------------------
    Signature("jpeg", "photo", "jpg", b"\xff\xd8\xff", 0, 512, 256 * MIB, carve_jpeg),
    Signature("png", "photo", "png", PNG_MAGIC, 0, 128, 128 * MIB, carve_png),
    Signature("gif", "photo", "gif", b"GIF8", 0, 64, 64 * MIB, carve_gif),
    Signature("bmp", "photo", "bmp", b"BM", 0, 128, 64 * MIB, carve_bmp),
    Signature("tiff", "photo", "tif", b"II*\x00", 0, 4096, 256 * MIB, carve_tiff),
    Signature("tiff", "photo", "tif", b"MM\x00*", 0, 4096, 256 * MIB, carve_tiff),
    Signature("heif", "photo", "heic", b"ftyp", 4, 1024, 128 * MIB, carve_heif),
    # --- video / audio ----------------------------------------------------
    Signature("isobmff", "video", "mp4", b"ftyp", 4, 2048, 8 * 1024 * MIB, carve_isobmff),
    Signature("riff", "video", "avi", b"RIFF", 0, 1024, 8 * 1024 * MIB, carve_riff),
    # --- documents --------------------------------------------------------
    Signature("pdf", "doc", "pdf", b"%PDF-", 0, 128, 512 * MIB, carve_pdf),
    Signature("zip", "doc", "zip", b"PK\x03\x04", 0, 64, 2 * 1024 * MIB, carve_zip),
    Signature("ole", "doc", "doc", OLE_MAGIC, 0, 1024, 512 * MIB, carve_ole),
]

PROFILES = {
    "media": {"photo", "video", "audio"},
    "photos": {"photo"},
    "videos": {"video", "audio"},
    "docs": {"doc"},
    "all": {"photo", "video", "audio", "doc"},
}


def signatures_for(profile: str) -> list:
    try:
        kinds = PROFILES[profile]
    except KeyError:
        raise ValueError("unknown profile %r (choose from %s)"
                         % (profile, ", ".join(PROFILES)))
    return [s for s in SIGNATURES if s.kind in kinds]


def max_magic_len() -> int:
    return max(len(s.magic) for s in SIGNATURES)
