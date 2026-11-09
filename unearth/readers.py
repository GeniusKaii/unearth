"""Windowed reads and file materialisation on top of a read-only source."""

from __future__ import annotations

import hashlib
import os

from .util import MIB


class Reader:
    """A forward friendly cache: carving walks offsets in increasing order.

    ``read(off, n)`` returns at most ``n`` bytes starting at the absolute
    ``off``.  It pulls ahead in ``block`` sized chunks so a parser that walks
    box by box, or a JPEG that scans for its EOI marker, does not issue one
    syscall per field.
    """

    def __init__(self, src, block: int = 4 * MIB, limit: int | None = None):
        self.src = src
        self.block = block
        self.limit = src.size if limit is None else min(limit, src.size)
        self._start = -1
        self._data = b""
        self.reads = 0
        self.bytes_read = 0

    def read(self, off: int, n: int) -> bytes:
        if n <= 0 or off < 0 or off >= self.limit:
            return b""
        end = min(off + n, self.limit)
        cache_end = self._start + len(self._data)
        if off < self._start or end > cache_end:
            want = max(n, self.block)
            want = min(want, self.limit - off)
            if want <= 0:
                return b""
            self._data = self.src.read_at(off, want)
            self._start = off
            self.reads += 1
            self.bytes_read += len(self._data)
        s = off - self._start
        return self._data[s: s + n]

    def remaining(self, off: int) -> int:
        return max(0, self.limit - off)


def materialize(src, off: int, size: int, path: str,
                chunk: int = 8 * MIB) -> tuple[int, str, bool]:
    """Copy ``size`` bytes from ``src`` at ``off`` into ``path``.

    Returns ``(written, sha256, complete)``.  Reads go straight to the source
    rather than through :class:`Reader`, because a 2 GB video has no business
    being buffered twice.  Writing is always done here, never at the source.
    """
    digest = hashlib.sha256()
    written = 0
    remaining = size
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        pos = off
        while remaining > 0:
            block = src.read_at(pos, min(chunk, remaining))
            if not block:
                break
            fh.write(block)
            digest.update(block)
            written += len(block)
            pos += len(block)
            remaining -= len(block)
    complete = remaining == 0 and written == size
    if complete:
        os.replace(tmp, path)
    else:
        os.replace(tmp, path)
    return written, digest.hexdigest(), complete
