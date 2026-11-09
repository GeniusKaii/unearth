"""The carving engine: walk the device once, try every signature, keep what parses."""

from __future__ import annotations

import bisect
import os
import time
from dataclasses import dataclass, field

from . import util
from .formats.registry import max_magic_len, signatures_for
from .readers import Reader, materialize
from .util import MIB

# A hit is measured from the bytes on disk; anything larger than this is either
# a false positive or a file we cannot afford to copy in one piece.
DEFAULT_CHUNK = 16 * MIB


@dataclass
class ScanOptions:
    profile: str = "media"
    chunk: int = DEFAULT_CHUNK
    start: int = 0
    end: int | None = None
    out_dir: str = "."
    keep_partial: bool = True
    min_size: int = 0
    max_size: int = 0            # 0 = per signature default
    verbose: bool = False
    skip_ranges: list = field(default_factory=list)   # live file extents
    stop_after: int = 0          # stop after N files (debug / demo)


class IntervalSet:
    """Sorted, merged ranges; answers 'is this offset inside a live file?'."""

    def __init__(self, ranges=()):
        merged = []
        for start, end in sorted((int(a), int(b)) for a, b in ranges if b > a):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self.starts = [r[0] for r in merged]
        self.ends = [r[1] for r in merged]

    def __bool__(self) -> bool:
        return bool(self.starts)

    def contains(self, offset: int, length: int = 1) -> bool:
        i = bisect.bisect_right(self.starts, offset) - 1
        if i < 0:
            return False
        return self.ends[i] >= offset + length


def scan_source(src, catalog, scan_id: int, opts: ScanOptions,
                progress=None) -> dict:
    """Carve ``src`` into ``opts.out_dir`` and record every hit in ``catalog``."""
    sigs = signatures_for(opts.profile)
    start = max(0, int(opts.start))
    end = src.size if opts.end is None else min(int(opts.end), src.size)
    if end <= start:
        raise ValueError("empty scan range (%d..%d)" % (start, end))

    carved_root = util.ensure_dir(os.path.join(opts.out_dir, "carved"))
    reader = Reader(src)
    live = IntervalSet(opts.skip_ranges) if opts.skip_ranges else None
    overlap = max_magic_len() + 16

    stats = {"files": 0, "partial": 0, "bytes": 0, "errors": 0,
             "skipped_live": 0, "by_fmt": {}, "by_kind": {}, "size": 0}
    claimed_end = start
    off = start
    last_ckpt = time.time()
    last_bytes = 0

    while off < end:
        n = min(opts.chunk, end - off)
        pre = 8 if off > start else 0
        buf = src.read_at(off - pre, n + pre + overlap)
        stats["bytes"] += n
        if progress is not None:
            extra = "%d files" % stats["files"]
            if stats["skipped_live"]:
                extra += ", %d live skipped" % stats["skipped_live"]
            progress.update(n, extra=extra)

        # Collect every magic hit in this chunk, then carve them *in offset
        # order*.  That matters: a JPEG inside an MP4 must not be carved first
        # and swallow the video that contains it.
        candidates = []
        for sig in sigs:
            magic = sig.magic
            i = buf.find(magic)
            while i >= 0:
                abs_off = off - pre + i - sig.magic_at
                if start <= abs_off < off + n:
                    candidates.append((abs_off, sig))
                i = buf.find(magic, i + 1)
        candidates.sort(key=lambda item: (item[0], -len(item[1].magic)))

        for abs_off, sig in candidates:
            if abs_off < claimed_end:
                continue
            try:
                res = sig.carve(reader, abs_off)
            except (OSError, ValueError, IndexError, KeyError):
                stats["errors"] += 1
                continue
            if res is None:
                continue
            if res.size < max(sig.min_size, opts.min_size):
                continue
            if res.size > sig.max_size:
                continue
            if opts.max_size and res.size > opts.max_size:
                continue
            if abs_off + res.size > src.size:
                continue
            if live and live.contains(abs_off, res.size):
                stats["skipped_live"] += 1
                claimed_end = max(claimed_end, abs_off + res.size)
                continue

            kind = res.meta.get("kind") or sig.kind
            ext = res.meta.get("ext") or res.meta.get("raw_ext") or sig.ext
            claimed_end = max(claimed_end, abs_off + res.size)

            digest, wrote, complete = _store(src, abs_off, res, kind, ext,
                                            sig.fmt, carved_root)
            if not complete and not opts.keep_partial:
                try:
                    os.remove(wrote)
                except OSError:
                    pass
                continue

            fields = {
                "scan_id": scan_id, "src": src.spec, "kind": kind,
                "fmt": sig.fmt, "ext": ext, "off": abs_off, "size": res.size,
                "complete": int(complete),
                "path": util.rel_posix(wrote, opts.out_dir),
                "sha256": digest,
                "note": "; ".join(res.warnings)[:300],
            }
            fields.update(res.meta_for_db())
            catalog.add_file(**fields)
            stats["files"] += 1
            stats["size"] += res.size
            stats["by_fmt"][sig.fmt] = stats["by_fmt"].get(sig.fmt, 0) + 1
            stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
            if not complete:
                stats["partial"] += 1
            if opts.verbose:
                print("  + %-10s %-8s %12s  at 0x%-10x %s" % (
                    kind, ext, util.human_bytes(res.size), abs_off,
                    "ok" if complete else "PARTIAL"), flush=True)
            if opts.stop_after and stats["files"] >= opts.stop_after:
                catalog.commit()
                return stats

        off += n
        now = time.time()
        if now - last_ckpt > 5 or (stats["bytes"] - last_bytes) > 256 * MIB:
            catalog.checkpoint(scan_id, off, stats["files"])
            last_ckpt = now
            last_bytes = stats["bytes"]

    catalog.checkpoint(scan_id, end, stats["files"])
    return stats


def _store(src, abs_off, res, kind, ext, fmt, carved_root):
    subdir = util.ensure_dir(os.path.join(carved_root, kind))
    name = "%s_%012x.%s" % (fmt, abs_off, ext)
    path = os.path.join(subdir, name)
    written, digest, complete = materialize(src, abs_off, res.size, path)
    return digest, path, bool(complete and res.complete)
