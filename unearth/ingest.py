"""Ingest a directory of already-recovered files into the catalogue.

photo-digger's home turf: data recovery software dumps a folder of renamed
files (00001.jpg, 00002.jpg ...) from a dying disk, and the task is triage,
not carving.  unearth's pipeline assumed the files came from a raw device;
this module lets it start from a plain directory instead.

Reads each file's header (only the first 2 MiB - EXIF lives there), parses
metadata with the same parsers the carvers use, and registers rows pointing
at the *original* files in place.  Nothing is copied; the catalogue row's
``path`` is the file's real location.
"""

from __future__ import annotations

import os

from . import util
from .readers import Reader
from .formats.registry import SIGNATURES

HEADER_BYTES = 2 * 1024 * 1024   # EXIF virtually always sits in the file head


class _FileSlice:
    """A minimal Source-like view over one regular file, for Reader()."""

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)
        self.spec = path
        self.sector_size = 512
        self._fh = open(path, "rb")

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset < 0 or offset >= self.size:
            return b""
        self._fh.seek(offset)
        return self._fh.read(min(length, self.size - offset))

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _carve_for(ext: str):
    """Pick the carver that fits a file extension, or None to skip."""
    ext = (ext or "").lower().lstrip(".")
    fmt = {"jpg": "jpeg", "jpeg": "jpeg", "jpe": "jpeg",
           "png": "png", "gif": "gif", "bmp": "bmp",
           "tif": "tiff", "tiff": "tiff",
           "mp4": "isobmff", "mov": "isobmff", "m4v": "isobmff",
           "3gp": "isobmff", "m4a": "isobmff",
           "pdf": "pdf", "zip": "zip", "docx": "zip", "xlsx": "zip",
           "pptx": "zip", "doc": "ole", "xls": "ole", "ppt": "ole",
           "msg": "ole", "avi": "riff", "wav": "riff", "webp": "riff"}.get(ext)
    if not fmt:
        return None
    for sig in SIGNATURES:
        if sig.fmt == fmt:
            return sig
    return None


def ingest_file(path: str, sig) -> dict:
    """Parse one file's header; returns catalogue-ready fields (or empty)."""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return {}
        src = _FileSlice(path)
    except OSError:
        return {}
    try:
        reader = Reader(src)
        reader.limit = min(size, HEADER_BYTES + 64 * 1024)
        res = sig.carve(reader, 0)
    except (OSError, ValueError, IndexError, KeyError):
        res = None
    finally:
        src.close()
    if res is None:
        return {"size": size, "complete": 1}
    fields = {
        "size": size,
        "complete": int(bool(res.complete)) if res.size >= size else 0,
        "note": "; ".join(res.warnings)[:300] if res.warnings else "",
    }
    fields.update(res.meta_for_db())
    return fields


def ingest_dir(root: str, cat, scan_id: int, exts=None,
               progress=None, log=None) -> dict:
    """Walk ``root`` and add one catalogue row per recognisable file.

    The row's ``path`` points at the file where it lies - the caller passes
    ``out_dir=root`` when opening the catalogue, so exports and the album
    resolve to the real location.
    """
    root = os.path.abspath(root)
    exts = set(exts or PHOTO_EXTS)
    stats = {"seen": 0, "ingested": 0, "skipped_ext": 0, "skipped_parse": 0,
             "bytes": 0}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext not in exts:
                stats["skipped_ext"] += 1
                continue
            path = os.path.join(dirpath, name)
            stats["seen"] += 1
            sig = _carve_for(ext)
            fields = {}
            if sig is not None:
                fields = ingest_file(path, sig)
            if not fields:
                stats["skipped_parse"] += 1
                continue
            kind = sig.kind if sig else "other"
            meta_kind = fields.pop("kind", None)
            fmt = sig.fmt if sig else "file"
            row = {
                "scan_id": scan_id, "src": root, "kind": meta_kind or kind,
                "fmt": fmt, "ext": ext,
                "off": 0, "size": fields.get("size", os.path.getsize(path)),
                "complete": fields.get("complete", 1),
                "name": name,
                "path": util.rel_posix(path, root),
                "note": fields.get("note", ""),
            }
            for key in ("width", "height", "duration", "captured", "day",
                        "make", "model", "gps_lat", "gps_lon", "orientation",
                        "exif_params"):
                if fields.get(key) is not None:
                    row[key] = fields[key]
            cat.add_file(**row)
            stats["ingested"] += 1
            stats["bytes"] += row["size"]
            if progress is not None:
                progress.update(1, extra="%d/%d" % (stats["ingested"],
                                                    stats["seen"]))
            if log and stats["ingested"] % 500 == 0:
                log("  ... 已摄取 %d 个" % stats["ingested"])
    cat.commit()
    return stats


PHOTO_EXTS = {
    "jpg", "jpeg", "jpe", "png", "gif", "bmp", "tif", "tiff", "webp",
    "heic", "heif", "avif",
    "mp4", "mov", "m4v", "3gp", "avi", "m4a", "wav",
    "pdf", "zip", "docx", "xlsx", "pptx", "doc", "xls", "ppt", "msg",
}
