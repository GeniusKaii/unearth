"""NTFS $MFT recovery: deleted files with their *original names and dates*.

Carving finds bytes; the master file table finds *stories*.  A deleted NTFS
record usually keeps its ``$FILE_NAME`` attribute, so a photo comes back as
``2013-08-12 19:22  DCIM/100CANON/IMG_1234.JPG`` instead of ``jpeg_0x1a2b3c.jpg``.
That is the difference between a pile of bytes and a shoebox of memories.

Nothing here writes to the source; extraction copies the data runs into the
output directory.
"""

from __future__ import annotations

import datetime as dt
import os
import struct
from dataclasses import dataclass, field

from .. import util
from ..util import MIB

ATTR_STANDARD_INFO = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

FILE_ATTR_DIRECTORY = 0x10000000
FILE_ATTR_REPARSE = 0x40000000


class NotNtfs(Exception):
    pass


@dataclass
class Run:
    lcn: int | None      # None = sparse
    clusters: int

    def byte_range(self, cluster_size: int):
        if self.lcn is None:
            return None
        return (self.lcn * cluster_size, self.clusters * cluster_size)


@dataclass
class Record:
    index: int
    in_use: bool
    is_dir: bool
    name: str = ""
    parent: int = 0
    size: int = 0
    cluster_runs: list = field(default_factory=list)
    resident_data: bytes | None = None
    created: dt.datetime | None = None
    modified: dt.datetime | None = None
    mft_modified: dt.datetime | None = None
    accessed: dt.datetime | None = None
    namespace: int = 0
    note: str = ""

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower().lstrip(".")


def _decode_runs(blob: bytes) -> list:
    runs = []
    pos = 0
    lcn = 0
    while pos < len(blob):
        header = blob[pos]
        pos += 1
        if header == 0:
            break
        length_size = header & 0x0F
        offset_size = (header >> 4) & 0x0F
        if length_size == 0 or pos + length_size + offset_size > len(blob):
            break
        length = int.from_bytes(blob[pos: pos + length_size], "little")
        pos += length_size
        if offset_size == 0:
            runs.append(Run(None, length))
            continue
        delta = int.from_bytes(blob[pos: pos + offset_size], "little", signed=True)
        pos += offset_size
        lcn += delta
        runs.append(Run(lcn, length))
    return runs


class Ntfs:
    """A minimal, read-only NTFS volume reader."""

    def __init__(self, src, base: int = 0, verbose: bool = False):
        self.src = src
        self.base = int(base)
        self.verbose = verbose
        boot = src.read_at(self.base, 512)
        if len(boot) < 512 or boot[3:11] != b"NTFS    ":
            raise NotNtfs("no NTFS boot sector at offset %d" % self.base)
        self.bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0]
        self.sectors_per_cluster = boot[0x0D]
        if self.bytes_per_sector not in (512, 1024, 2048, 4096):
            raise NotNtfs("bogus sector size %d" % self.bytes_per_sector)
        if not 1 <= self.sectors_per_cluster <= 128:
            raise NotNtfs("bogus cluster size")
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.total_sectors = struct.unpack_from("<Q", boot, 0x28)[0]
        self.mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
        self.mft_mirror_lcn = struct.unpack_from("<Q", boot, 0x38)[0]
        cpr = struct.unpack_from("<b", boot, 0x40)[0]
        self.record_size = (1 << (-cpr)) if cpr < 0 else (cpr * self.cluster_size)
        if self.record_size < 512 or self.record_size > 65536:
            raise NotNtfs("bogus MFT record size %d" % self.record_size)
        sb = struct.unpack_from("<b", boot, 0x44)[0]
        self.index_block_size = (1 << (-sb)) if sb < 0 else sb * self.cluster_size
        self.volume_size = min(
            self.total_sectors * self.bytes_per_sector,
            max(0, src.size - self.base),
        )
        self.mft_offset = self.base + self.mft_lcn * self.cluster_size
        self.record_count = max(0, (self.volume_size - self.mft_lcn * self.cluster_size)
                                // self.record_size)
        self._parent_cache: dict = {}

    # -- helpers -------------------------------------------------------------
    def describe(self) -> str:
        return ("NTFS cluster=%s record=%d MFT@%s clusters=%d (%.1fM records possible)"
                % (util.human_bytes(self.cluster_size), self.record_size,
                   util.human_bytes(self.mft_lcn * self.cluster_size),
                   self.record_count, self.record_count / 1e6))

    def _apply_fixup(self, raw: bytes) -> bytes | None:
        usa_off, usa_count = struct.unpack_from("<HH", raw, 4)
        if not (0 < usa_count <= 64) or usa_off + usa_count * 2 > len(raw):
            return None
        sector = self.bytes_per_sector
        if (usa_count - 1) * sector > len(raw):
            return None
        out = bytearray(raw)
        for i in range(1, usa_count):
            end = i * sector
            out[end - 2: end] = raw[usa_off + i * 2: usa_off + i * 2 + 2]
        return bytes(out)

    # -- record parsing ------------------------------------------------------
    def parse_record(self, raw: bytes, index: int = -1) -> Record | None:
        if len(raw) < 48 or raw[:4] != b"FILE":
            return None
        fixed = self._apply_fixup(raw)
        if fixed is None:
            return None
        flags = struct.unpack_from("<H", fixed, 0x16)[0]
        used = struct.unpack_from("<I", fixed, 0x18)[0]
        attr_off = struct.unpack_from("<H", fixed, 0x14)[0]
        if attr_off < 24 or attr_off >= len(fixed):
            return None
        used = min(used or len(fixed), len(fixed))

        rec = Record(index=index, in_use=bool(flags & 0x01),
                     is_dir=bool(flags & 0x02))

        pos = attr_off
        guard = 0
        while pos + 8 <= used and guard < 256:
            guard += 1
            atype = struct.unpack_from("<I", fixed, pos)[0]
            if atype == ATTR_END:
                break
            alen = struct.unpack_from("<I", fixed, pos + 4)[0]
            if alen < 16 or pos + alen > len(fixed):
                break
            non_resident = fixed[pos + 8]
            name_len = fixed[pos + 9]
            name_off = struct.unpack_from("<H", fixed, pos + 10)[0]
            attr_name = ""
            if name_len and name_off and pos + name_off + name_len * 2 <= len(fixed):
                attr_name = fixed[pos + name_off: pos + name_off + name_len * 2]\
                    .decode("utf-16-le", "replace")
            if atype == ATTR_FILE_NAME and not non_resident:
                vlen = struct.unpack_from("<I", fixed, pos + 16)[0]
                voff = struct.unpack_from("<H", fixed, pos + 20)[0]
                val = fixed[pos + voff: pos + voff + vlen]
                if len(val) >= 66:
                    self._read_file_name(rec, val)
            elif atype == ATTR_STANDARD_INFO and not non_resident:
                vlen = struct.unpack_from("<I", fixed, pos + 16)[0]
                voff = struct.unpack_from("<H", fixed, pos + 20)[0]
                val = fixed[pos + voff: pos + voff + min(vlen, 72)]
                if len(val) >= 32:
                    rec.created = util.filetime_to_dt(struct.unpack_from("<Q", val, 0)[0])
                    rec.modified = util.filetime_to_dt(struct.unpack_from("<Q", val, 8)[0])
                    rec.mft_modified = util.filetime_to_dt(struct.unpack_from("<Q", val, 16)[0])
                    rec.accessed = util.filetime_to_dt(struct.unpack_from("<Q", val, 24)[0])
            elif atype == ATTR_DATA and not attr_name:
                if non_resident:
                    start_vcn, last_vcn = struct.unpack_from("<QQ", fixed, pos + 16)
                    run_off = struct.unpack_from("<H", fixed, pos + 32)[0]
                    real_size = struct.unpack_from("<Q", fixed, pos + 48)[0]
                    rec.size = int(real_size)
                    if run_off and pos + run_off < pos + alen:
                        rec.cluster_runs.extend(_decode_runs(fixed[pos + run_off: pos + alen]))
                else:
                    vlen = struct.unpack_from("<I", fixed, pos + 16)[0]
                    voff = struct.unpack_from("<H", fixed, pos + 20)[0]
                    rec.resident_data = fixed[pos + voff: pos + voff + vlen]
                    rec.size = vlen
            pos += alen
        return rec

    @staticmethod
    def _read_file_name(rec: Record, val: bytes) -> None:
        parent = struct.unpack_from("<Q", val, 0)[0] & 0xFFFFFFFFFFFF
        name_len = val[64]
        namespace = val[65]
        raw_name = val[66: 66 + name_len * 2]
        try:
            name = raw_name.decode("utf-16-le", "replace")
        except UnicodeDecodeError:
            return
        # 2 = DOS 8.3 short name; prefer the long name unless it is empty/garbage
        if namespace == 2 and rec.name:
            return
        created = util.filetime_to_dt(struct.unpack_from("<Q", val, 8)[0])
        modified = util.filetime_to_dt(struct.unpack_from("<Q", val, 16)[0])
        mft_mod = util.filetime_to_dt(struct.unpack_from("<Q", val, 24)[0])
        accessed = util.filetime_to_dt(struct.unpack_from("<Q", val, 32)[0])
        real_size = struct.unpack_from("<Q", val, 48)[0]
        fname_flags = struct.unpack_from("<I", val, 56)[0]
        rec.name = name
        rec.parent = parent
        rec.namespace = namespace
        if fname_flags & FILE_ATTR_DIRECTORY:
            rec.is_dir = True
        if not rec.size:
            rec.size = int(real_size)
        if rec.created is None:
            rec.created = created
        if rec.modified is None:
            rec.modified = modified
        if rec.mft_modified is None:
            rec.mft_modified = mft_mod
        if rec.accessed is None:
            rec.accessed = accessed

    # -- iteration -----------------------------------------------------------
    def record_at(self, index: int) -> Record | None:
        off = self.mft_offset + index * self.record_size
        if off + self.record_size > self.src.size:
            return None
        raw = self.src.read_at(off, self.record_size)
        if len(raw) < self.record_size:
            return None
        return self.parse_record(raw, index)

    def iter_records(self, limit: int = 0, progress=None):
        """Stream every record; stops after 512 consecutive invalid records."""
        block_records = max(1, (4 * MIB) // self.record_size)
        block_bytes = block_records * self.record_size
        index = 0
        bad = 0
        emitted = 0
        max_records = self.record_count if not limit else min(limit, self.record_count)
        while index < max_records:
            off = self.mft_offset + index * self.record_size
            data = self.src.read_at(off, min(block_bytes, self.mft_offset
                                             + max_records * self.record_size - off))
            if len(data) < self.record_size:
                break
            for i in range(0, len(data) - self.record_size + 1, self.record_size):
                raw = data[i: i + self.record_size]
                if raw[:4] != b"FILE":
                    bad += 1
                    if bad > 512:
                        return
                    index += 1
                    continue
                bad = 0
                rec = self.parse_record(raw, index)
                index += 1
                if rec is None:
                    continue
                emitted += 1
                yield rec
            if progress is not None:
                progress.update(len(data), extra="%d records" % emitted)
        return

    def deleted_files(self, only_deleted: bool = True):
        for rec in self.iter_records():
            if rec.is_dir or not rec.name:
                continue
            if only_deleted and rec.in_use:
                continue
            if not rec.cluster_runs and rec.resident_data is None:
                continue
            yield rec

    # -- names / paths -------------------------------------------------------
    def parent_name(self, ref: int) -> str:
        if ref in self._parent_cache:
            return self._parent_cache[ref]
        name = ""
        try:
            parent = self.record_at(ref)
            if parent is not None:
                name = parent.name if parent.is_dir else ""
        except (OSError, ValueError):
            name = ""
        self._parent_cache[ref] = name
        return name

    def rel_path(self, rec: Record, max_depth: int = 6) -> str:
        """Best effort original folder, e.g. 'DCIM/100CANON'."""
        parts = []
        ref = rec.parent
        seen = set()
        while ref and ref not in seen and len(parts) < max_depth:
            seen.add(ref)
            if ref == 5:  # root directory
                break
            name = self.parent_name(ref)
            if not name:
                break
            parts.append(util.safe_filename(name, 72))
            try:
                parent = self.record_at(ref)
                ref = parent.parent if parent else 0
            except (OSError, ValueError):
                break
        parts.reverse()
        return os.path.join(*parts) if parts else ""

    # -- extraction ----------------------------------------------------------
    def run_bytes(self, rec: Record) -> list:
        ranges = []
        for run in rec.cluster_runs:
            span = run.byte_range(self.cluster_size)
            if span:
                ranges.append(span)
        return ranges

    def live_extents(self, max_records: int = 0, progress=None):
        """Byte ranges occupied by *in use* files - used by ``--exclude-live``.

        Uses a bounded interval list: one (start, end) pair per contiguous
        run, merged on the fly into at most one entry per adjacent cluster.
        Memory stays proportional to the number of *fragments*, not to the
        number of files or total bytes, which matters on a 1 TB disk with
        millions of MFT records.
        """
        frags = []
        for rec in self.iter_records(limit=max_records, progress=progress):
            if not rec.in_use or rec.is_dir:
                continue
            for start, length in self.run_bytes(rec):
                frags.append((self.base + start, self.base + start + length))
        frags.sort()
        out = []
        for start, end in frags:
            if out and start <= out[-1][1]:
                if end > out[-1][1]:
                    out[-1][1] = end
            else:
                out.append([start, end])
        return [(start, end) for start, end in out]


def extract(src, ntfs: Ntfs, rec: Record, dest_dir: str, taken=None):
    """Copy one record's data into ``dest_dir``; returns (path, written, complete)."""
    name = util.safe_filename(rec.name or ("record_%d" % rec.index))
    path = util.unique_path(dest_dir, name, taken)
    written = 0
    remaining = int(rec.size or 0)
    tmp = path + ".part"
    complete = True
    with open(tmp, "wb") as fh:
        if rec.resident_data is not None:
            blob = rec.resident_data[: remaining or None]
            fh.write(blob)
            written = len(blob)
        else:
            for start, length in ntfs.run_bytes(rec):
                take = min(length, max(0, remaining - written)) if remaining else length
                if take <= 0:
                    break
                pos = ntfs.base + start
                left = take
                while left > 0:
                    block = src.read_at(pos, min(4 * MIB, left))
                    if not block:
                        complete = False
                        break
                    fh.write(block)
                    written += len(block)
                    pos += len(block)
                    left -= len(block)
                if not complete:
                    break
    if remaining and written < remaining:
        complete = False
    if written == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None, 0, False
    os.replace(tmp, path)
    if not complete:
        # keep the partial bytes but say so: a half photo can still be a memory
        pass
    return path, written, complete
