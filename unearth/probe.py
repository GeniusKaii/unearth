"""Discovery of physical disks, volumes and their filesystems.

Windows blocks WMI from some confined processes, so everything here is built on
plain ``CreateFile`` + ``DeviceIoControl`` through :mod:`unearth.device`.  That
also means the same code path proves whether we actually *can* read the device
before a scan is started.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from . import device
from .util import human_bytes

PARTITION_TYPES = {
    0x07: "NTFS/exFAT", 0x0B: "FAT32", 0x0C: "FAT32 (LBA)", 0x0E: "FAT16",
    0x83: "Linux", 0xAF: "HFS+", 0xEE: "GPT protective", 0xEF: "EFI system",
    0x82: "Linux swap", 0xA5: "FreeBSD",
}


@dataclass
class VolumeInfo:
    letter: str
    spec: str
    label: str = ""
    fs: str = ""
    size: int = 0
    free: int = 0
    disk_number: int | None = None
    offset: int | None = None
    readable: bool = True
    error: str = ""

    def as_row(self) -> str:
        fs = self.fs or "?"
        where = ""
        if self.disk_number is not None:
            where = "disk%d@%s" % (self.disk_number, human_bytes(self.offset or 0))
        return "%-4s %-8s %-12s %12s  %s" % (
            self.letter + ":", where, fs, human_bytes(self.size), self.label,
        )


@dataclass
class DiskInfo:
    number: int
    spec: str
    size: int = 0
    readable: bool = True
    error: str = ""
    partitions: list = field(default_factory=list)  # [(offset, size, type, fs)]

    def as_row(self) -> str:
        return "%-10s %12s  %s" % (
            "disk:%d" % self.number, human_bytes(self.size),
            "" if self.readable else self.error,
        )


# --------------------------------------------------------------------------- #
# boot sector sniffing
# --------------------------------------------------------------------------- #
def sniff_filesystem(boot: bytes) -> str:
    """Best effort filesystem name from a 512 byte boot sector / VBR."""
    if len(boot) < 512:
        return ""
    oem = boot[3:11]
    if oem == b"NTFS    ":
        return "NTFS"
    if oem == b"EXFAT   ":
        return "exFAT"
    if boot[510:512] != b"\x55\xaa":
        if boot[0:4] in (b"EXT2", b"EXT3", b"EXT4") or boot[0x438:0x43A] == b"\x53\xef":
            return "ext2/3/4"
        if boot[0x400:0x406] == b"LABEL=":
            return "ext2/3/4"
        return ""
    fat32 = boot[0x52:0x5A]
    fat16 = boot[0x36:0x3E]
    if fat32[:7] == b"FAT32  ":
        return "FAT32"
    if fat16[:7] == b"FAT16  ":
        return "FAT16"
    if fat16[:7] == b"FAT12  ":
        return "FAT12"
    # FAT32 with a broken label, but the BPB still looks like FAT
    if boot[0x42:0x46] in (b"FAT3",) or (boot[0x52:0x57] == b"FAT32"):
        return "FAT32"
    if boot[0x36:0x3B] == b"FAT16" or boot[0x36:0x3B] == b"FAT12":
        return "FAT"
    return ""


# --------------------------------------------------------------------------- #
# enumeration
# --------------------------------------------------------------------------- #
def _working_drive_letters() -> list:
    letters = []
    for code in range(ord("A"), ord("Z") + 1):
        letter = chr(code)
        try:
            if os.path.exists(letter + ":\\"):
                letters.append(letter)
        except OSError:
            continue
    return letters


def _volume_metadata(letter: str) -> tuple:
    """(label, filesystem) via GetVolumeInformationW - no admin needed."""
    if os.name != "nt":
        return "", ""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    label = ctypes.create_unicode_buffer(261)
    fsname = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    maxlen = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    ok = k32.GetVolumeInformationW(
        ctypes.c_wchar_p(letter + ":\\"), label, 261,
        ctypes.byref(serial), ctypes.byref(maxlen), ctypes.byref(flags),
        fsname, 261,
    )
    if not ok:
        return "", ""
    return label.value, fsname.value


def list_volumes(probe_boot: bool = True) -> list:
    """Mounted volumes plus whether the raw device is actually openable."""
    out = []
    for letter in _working_drive_letters():
        path = device.volume_path(letter)
        info = VolumeInfo(letter=letter, spec="vol:%s:" % letter)
        info.label, info.fs = _volume_metadata(letter)
        try:
            usage = _disk_usage(letter + ":\\")
        except OSError:
            usage = None
        if usage:
            info.size, info.free = usage[0], usage[2]
        try:
            with device.WindowsRawSource(path, letter + ":") as src:
                info.size = src.size or info.size
                if probe_boot:
                    boot = src.read_at(0, 512)
                    sniffed = sniff_filesystem(boot)
                    if sniffed:
                        info.fs = sniffed
                ext = device.volume_disk_extents(path)
                if ext:
                    info.disk_number = ext[0][0]
                    info.offset = ext[0][1]
        except OSError as exc:
            info.readable = False
            info.error = device.win_error_text(getattr(exc, "errno", 0) or 0) or str(exc)
        out.append(info)
    return out


def _disk_usage(root: str):
    import shutil
    try:
        return shutil.disk_usage(root)
    except OSError:
        return None


def list_disks(max_index: int = 24) -> list:
    """Probe ``\\\\.\\PhysicalDriveN`` for N in 0..max_index-1.

    Without elevation every ``CreateFile`` fails with access denied and there is
    no way to ask Windows which indexes exist, so we bail out with an empty list
    instead of printing two dozen identical errors.
    """
    out = []
    denied = 0
    for number in range(max_index):
        spec = "disk:%d" % number
        info = DiskInfo(number=number, spec=spec)
        try:
            with device.WindowsRawSource(device.physical_drive_path(number)) as src:
                info.size = src.size
                info.partitions = _read_partition_table(src)
        except OSError as exc:
            code = getattr(exc, "errno", 0) or 0
            # ERROR_FILE_NOT_FOUND (2) just means: no such disk index
            if code == 2:
                continue
            if code == 5:
                denied += 1
                if not out and denied >= 2:
                    return []
                continue
            info.readable = False
            info.error = device.win_error_text(code) or str(exc)
        out.append(info)
    return out


def _read_partition_table(src) -> list:
    """Parse the MBR (or GPT protective MBR) into (offset, size, type, fs)."""
    parts = []
    try:
        mbr = src.read_at(0, 512)
    except OSError:
        return parts
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return parts
    for i in range(4):
        base = 446 + i * 16
        ptype = mbr[base + 4]
        start_lba, sectors = struct.unpack_from("<II", mbr, base + 8)
        if ptype == 0 or sectors == 0:
            continue
        offset = start_lba * 512
        size = sectors * 512
        fs = ""
        try:
            boot = src.read_at(offset, 512)
            fs = sniff_filesystem(boot)
        except OSError:
            pass
        parts.append((offset, size, PARTITION_TYPES.get(ptype, "type 0x%02x" % ptype), fs))
    return parts


def describe_targets(volumes: list, disks: list) -> str:
    lines = []
    if volumes:
        lines.append("Mounted volumes")
        lines.append("  %-4s %-22s %-12s %12s  %s" % ("vol", "physical", "filesystem", "size", "label"))
        for v in volumes:
            lines.append("  " + v.as_row())
    if disks:
        if lines:
            lines.append("")
        lines.append("Physical disks")
        lines.append("  %-10s %12s  %s" % ("spec", "size", "status"))
        for d in disks:
            lines.append("  " + d.as_row())
            for offset, size, ptype, fs in d.partitions[:8]:
                lines.append("      %12s at %-12s %-12s %s" % (
                    human_bytes(size), human_bytes(offset), fs or ptype,
                    "partition",
                ))
    return "\n".join(lines)
