"""Shared pieces of the carving layer: integer helpers and signature types."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# --- integer helpers (both endiannesses are needed) -------------------------
def u16(b: bytes, o: int = 0) -> int:
    return b[o] | (b[o + 1] << 8)


def u16be(b: bytes, o: int = 0) -> int:
    return (b[o] << 8) | b[o + 1]


def u32(b: bytes, o: int = 0) -> int:
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


def u32be(b: bytes, o: int = 0) -> int:
    return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]


def u64(b: bytes, o: int = 0) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def u64be(b: bytes, o: int = 0) -> int:
    return struct.unpack_from(">Q", b, o)[0]


def s32(b: bytes, o: int = 0) -> int:
    return struct.unpack_from("<i", b, o)[0]


@dataclass
class CarveResult:
    """What a format carver learned about one candidate."""

    size: int
    complete: bool = True
    meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def meta_for_db(self) -> dict:
        """Flatten the interesting bits into catalogue column names."""
        m = self.meta
        out = {}
        if m.get("width") and m.get("height"):
            out["width"] = int(m["width"])
            out["height"] = int(m["height"])
        for key in ("duration", "orientation", "gps_lat", "gps_lon", "make",
                    "model", "captured", "day"):
            if m.get(key) not in (None, ""):
                out[key] = m[key]
        return out


@dataclass(frozen=True)
class Signature:
    """A file format we can find by magic and measure by parsing."""

    fmt: str
    kind: str           # photo | video | audio | doc
    ext: str
    magic: bytes
    magic_at: int       # offset of `magic` inside the file (4 for ISO BMFF)
    min_size: int
    max_size: int
    carve: Callable[[Any, int], Optional[CarveResult]]
    note: str = ""

    def covers(self, offset: int) -> bool:
        return offset >= self.magic_at
