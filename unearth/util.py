"""Small shared helpers: sizes, timestamps, hashing, names, progress bars."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sys
import time

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
TIB = 1024 * GIB

_UNITS = "BKMGT"

# Windows forbids these in file names, and a few names are devices.
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *("com%d" % i for i in range(1, 10)),
    *("lpt%d" % i for i in range(1, 10)),
}


# --------------------------------------------------------------------------- #
# numbers / sizes
# --------------------------------------------------------------------------- #
def human_bytes(n) -> str:
    """1536 -> '1.50 KiB'. None -> '-'."""
    if n is None:
        return "-"
    n = float(n)
    if n < 0:
        return "-" + human_bytes(-n)
    i = 0
    while n >= 1024 and i < len(_UNITS) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return "%d B" % int(n)
    return "%.2f %siB" % (n, _UNITS[i])


def human_rate(bytes_per_sec) -> str:
    if not bytes_per_sec:
        return "0 B/s"
    return human_bytes(bytes_per_sec) + "/s"


def human_duration(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def parse_size(text) -> int:
    """'16M' / '512k' / '1.5G' / '1073741824' -> bytes."""
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().replace("_", "")
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([bBkKmMgGtT]?)[bB]?", s)
    if not m:
        raise ValueError("cannot parse size %r" % text)
    value = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"": 1, "B": 1, "K": KIB, "M": MIB, "G": GIB, "T": TIB}[unit]
    return int(value * mult)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #
_EPOCH_1601 = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
_EPOCH_1904 = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
_EPOCH_1970 = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def filetime_to_dt(value):
    """NTFS 100ns-since-1601 -> aware datetime in UTC (None if implausible)."""
    if not value:
        return None
    try:
        return _EPOCH_1601 + dt.timedelta(microseconds=value / 10.0)
    except (OverflowError, ValueError):
        return None


def unix_to_dt(value):
    if value in (None, 0):
        return None
    try:
        return _EPOCH_1970 + dt.timedelta(seconds=float(value))
    except (OverflowError, ValueError):
        return None


def qt_to_dt(value):
    """QuickTime/MP4 seconds since 1904-01-01 -> datetime."""
    if not value:
        return None
    try:
        return _EPOCH_1904 + dt.timedelta(seconds=float(value))
    except (OverflowError, ValueError):
        return None


def fmt_dt(value, local=True) -> str:
    """Format a datetime for humans, in local time by default."""
    if not value:
        return "-"
    try:
        if value.tzinfo is not None and local:
            value = value.astimezone()
        return value.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "-"


def iso_or_empty(value) -> str:
    if not value:
        return ""
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return ""


def day_key(value) -> str:
    """UTC day (YYYY-MM-DD) used for timeline clustering; '' when unknown."""
    if not value:
        return ""
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return ""


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk: int = 4 * MIB) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# file names
# --------------------------------------------------------------------------- #
def safe_filename(name: str, maxlen: int = 96, fallback: str = "recovered") -> str:
    """Turn an arbitrary recovered name into something Windows accepts."""
    if not name:
        return fallback
    # strip any directory part, both separators
    name = name.replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip().strip(".")
    if not name:
        return fallback
    stem, ext = os.path.splitext(name)
    if stem.lower() in _RESERVED:
        stem = stem + "_"
    if len(stem) + len(ext) > maxlen:
        stem = stem[: max(1, maxlen - len(ext))]
    # keep it valid unicode, never split a surrogate pair
    stem = stem.encode("utf-8", "replace").decode("utf-8")
    return stem + ext


def unique_path(directory: str, name: str, taken: set | None = None) -> str:
    """Return a path in `directory` that does not exist yet (and is not in `taken`)."""
    stem, ext = os.path.splitext(name)
    candidate = name
    i = 1
    while os.path.exists(os.path.join(directory, candidate)) or (
        taken is not None and candidate.lower() in taken
    ):
        candidate = "%s~%d%s" % (stem, i, ext)
        i += 1
    if taken is not None:
        taken.add(candidate.lower())
    return os.path.join(directory, candidate)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def rel_posix(path: str, start: str) -> str:
    return os.path.relpath(path, start).replace("\\", "/")


def locate_file(out_dir: str, row) -> str:
    """Resolve a catalogue row's ``path`` to a real file on disk.

    Rows written by ``scan`` store a path relative to the *output* directory
    (the bytes were copied there).  Rows written by ``ingest`` point at files
    that never moved, relative to the *source* directory.  Try the ingest
    interpretation first - it is the one that can be verified - and fall back.
    """
    try:
        rel = row["path"] or ""
    except (KeyError, IndexError):
        return ""
    if not rel:
        return ""
    if os.path.isabs(rel):
        return rel if os.path.exists(rel) else ""
    candidates = []
    try:
        src = row["src"] or ""
    except (KeyError, IndexError):
        src = ""
    if src and os.path.isdir(src):
        candidates.append(os.path.join(src, rel))
    candidates.append(os.path.join(out_dir, rel))
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1] if candidates else ""


# --------------------------------------------------------------------------- #
# progress
# --------------------------------------------------------------------------- #
class Progress:
    """Cheap stderr progress line: percentage, rate, ETA, and a free-form note."""

    def __init__(self, total: int = 0, label: str = "scan", stream=None,
                 interval: float = 1.0, enabled: bool = True):
        self.total = int(total or 0)
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.enabled = enabled
        self.done = 0
        self.start = time.time()
        self._last = 0.0
        self.extra = ""
        self._dirty = False

    def update(self, n: int = 0, extra: str | None = None) -> None:
        self.done += n
        if extra is not None:
            self.extra = extra
        now = time.time()
        if self.enabled and now - self._last >= self.interval:
            self._render(now)
            self._last = now
            self._dirty = True

    def _render(self, now: float) -> None:
        elapsed = max(now - self.start, 1e-9)
        rate = self.done / elapsed
        pct = (self.done / self.total * 100.0) if self.total else 0.0
        eta = ""
        if self.total and rate > 0:
            eta = " eta %s" % human_duration((self.total - self.done) / rate)
        line = "\r[%s] %5.1f%% %s %s%s %s" % (
            self.label, pct, human_bytes(self.done), human_rate(rate), eta, self.extra,
        )
        self.stream.write(line[:180].ljust(120))
        self.stream.flush()

    def close(self, note: str = "") -> None:
        if self.enabled and self._dirty:
            self.stream.write("\n")
            self.stream.flush()
        elif self.enabled:
            self.stream.write("\n")
            self.stream.flush()
        self._dirty = False


class NullProgress:
    def update(self, n: int = 0, extra: str | None = None) -> None:
        pass

    def close(self, note: str = "") -> None:
        pass
