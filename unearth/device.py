"""Read-only access to raw disks, volumes and disk images.

The one rule a recovery tool must never break: never open the evidence with
write access.  Every handle created here asks for ``GENERIC_READ`` only, and
the sources expose no write method at all.

Two kinds of sources exist:

``WindowsRawSource``  ``\\\\.\\PhysicalDrive2`` or ``\\\\.\\E:`` - needs admin.
``FileSource``        a disk image (``.img`` / ``.dd`` / ``.raw`` / ``.iso``),
                      or any plain file, which is what the test suite uses.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from dataclasses import dataclass

SECTOR_DEFAULT = 512

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000

IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

ERROR_ACCESS_DENIED = 5
ERROR_NOT_READY = 21

INVALID_HANDLE_VALUE = (1 << 64) - 1

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _DISK_GEOMETRY(ctypes.Structure):
        _fields_ = [
            ("Cylinders", ctypes.c_longlong),
            ("MediaType", wintypes.DWORD),
            ("TracksPerCylinder", wintypes.DWORD),
            ("SectorsPerTrack", wintypes.DWORD),
            ("BytesPerSector", wintypes.DWORD),
            ("Sectors", ctypes.c_longlong),
        ]

    class _DISK_EXTENT(ctypes.Structure):
        _fields_ = [
            ("DiskNumber", wintypes.DWORD),
            ("StartingOffset", ctypes.c_longlong),
            ("ExtentLength", ctypes.c_longlong),
        ]

    class _VOLUME_DISK_EXTENTS(ctypes.Structure):
        _fields_ = [
            ("NumberOfDiskExtents", wintypes.DWORD),
            ("_pad", wintypes.DWORD),
            ("Extents", _DISK_EXTENT * 1),
        ]

    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.ReadFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


def win_error_text(code: int) -> str:
    if code == ERROR_ACCESS_DENIED:
        return ("access denied - raw disk/volume reads require an elevated "
                "(Run as administrator) terminal")
    if code == ERROR_NOT_READY:
        return "device not ready (no media?)"
    try:
        return ctypes.FormatError(code).strip()
    except Exception:  # pragma: no cover - defensive
        return "WinError %d" % code


# --------------------------------------------------------------------------- #
# base
# --------------------------------------------------------------------------- #
class Source:
    """A read-only byte addressable device or file."""

    kind = "source"
    readonly = True

    def __init__(self, spec: str, label: str, size: int, sector_size: int = SECTOR_DEFAULT):
        self.spec = spec
        self.label = label
        self.size = int(size)
        self.sector_size = int(sector_size or SECTOR_DEFAULT)
        self.bytes_read = 0
        self.read_calls = 0
        self.sector_size_source = "default"

    # subclasses implement this ------------------------------------------------
    def read_at(self, offset: int, length: int) -> bytes:
        raise NotImplementedError

    def read_exact(self, offset: int, length: int) -> bytes:
        data = self.read_at(offset, length)
        if len(data) < length:
            raise EOFError("short read at %d: wanted %d got %d"
                           % (offset, length, len(data)))
        return data

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self) -> str:
        return "<%s %s %d bytes>" % (type(self).__name__, self.spec, self.size)


class FileSource(Source):
    """A disk image or plain file on the current filesystem."""

    kind = "image"

    def __init__(self, path: str, label: str | None = None):
        path = os.path.abspath(path)
        st = os.stat(path)
        if not os.path.isfile(path):
            raise OSError("%s is not a regular file" % path)
        super().__init__(path, label or os.path.basename(path), st.st_size, SECTOR_DEFAULT)
        self._fh = open(path, "rb", buffering=0)
        self.sector_size_source = "assumed"

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.size or offset < 0:
            return b""
        length = min(length, self.size - offset)
        self._fh.seek(offset)
        data = self._fh.read(length)
        self.bytes_read += len(data)
        self.read_calls += 1
        return data

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


class WindowsRawSource(Source):
    """A raw physical drive or volume, opened with read-only access."""

    kind = "raw"

    def __init__(self, path: str, label: str | None = None, sector_size: int | None = None):
        if not _IS_WINDOWS:
            raise OSError("raw device access is only implemented for Windows; "
                          "pass a disk image instead")
        handle = _k32.CreateFileW(
            path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise OSError(err, "%s: %s" % (path, win_error_text(err)))
        self._handle = handle
        self._buf = None
        self._buf_len = 0

        sec = sector_size or self._query_sector_size() or SECTOR_DEFAULT
        size = self._query_length()
        if size <= 0:
            self._close_handle()
            raise OSError("cannot determine size of %s" % path)
        label = label or path
        super().__init__(path, label, size, sec)
        self.sector_size_source = "device"

    # -- ioctl helpers --------------------------------------------------------
    def _ioctl(self, code: int, out_size: int) -> bytes:
        buf = ctypes.create_string_buffer(max(out_size, 8))
        returned = ctypes.c_uint32(0)
        ok = _k32.DeviceIoControl(
            self._handle, code, None, 0, buf, len(buf),
            ctypes.byref(returned), None,
        )
        if not ok:
            return b""
        return buf.raw[: returned.value]

    def _query_sector_size(self) -> int | None:
        raw = self._ioctl(IOCTL_DISK_GET_DRIVE_GEOMETRY, ctypes.sizeof(_DISK_GEOMETRY))
        if raw and len(raw) >= ctypes.sizeof(_DISK_GEOMETRY):
            geom = _DISK_GEOMETRY.from_buffer_copy(raw)
            if 256 <= geom.BytesPerSector <= 65536:
                return int(geom.BytesPerSector)
        return None

    def _query_length(self) -> int:
        raw = self._ioctl(IOCTL_DISK_GET_LENGTH_INFO, 8)
        if raw and len(raw) >= 8:
            value = ctypes.c_longlong.from_buffer_copy(raw[:8]).value
            if value > 0:
                return int(value)
        # fall back: seek to the end
        try:
            here = self._seek(0, 1)
            end = self._seek(0, 2)
            self._seek(here, 0)
            return int(end)
        except OSError:
            return 0

    def _seek(self, distance: int, method: int) -> int:
        set_ptr = _k32.SetFilePointerEx
        set_ptr.restype = ctypes.c_int
        set_ptr.argtypes = [ctypes.c_void_p, ctypes.c_longlong,
                            ctypes.POINTER(ctypes.c_longlong), ctypes.c_uint32]
        new_pos = ctypes.c_longlong(0)
        if not set_ptr(self._handle, distance, ctypes.byref(new_pos), method):
            raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")
        return new_pos.value

    # -- reads ---------------------------------------------------------------
    def _read_raw(self, offset: int, length: int) -> bytes:
        if length > self._buf_len:
            self._buf = ctypes.create_string_buffer(length)
            self._buf_len = length
        ov = _OVERLAPPED()
        ov.Offset = offset & 0xFFFFFFFF
        ov.OffsetHigh = (offset >> 32) & 0xFFFFFFFF
        got = ctypes.c_uint32(0)
        ok = _k32.ReadFile(self._handle, self._buf, length,
                           ctypes.byref(got), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(err, "read failed at offset %d: %s" % (offset, win_error_text(err)))
        return self._buf.raw[: got.value]

    def read_at(self, offset: int, length: int) -> bytes:
        """Arbitrary read; the OS only accepts sector aligned requests."""
        if length <= 0 or offset < 0 or offset >= self.size:
            return b""
        end = min(offset + length, self.size)
        sec = self.sector_size
        aligned_start = offset - (offset % sec)
        aligned_end = min(end + (-end % sec), self.size - (self.size % sec) or self.size)
        if aligned_end <= aligned_start:
            return b""
        if aligned_end - aligned_start > 8 * 1024 * 1024:
            # avoid pathological single reads from a caller bug
            raise ValueError("read_at(%d, %d) too large" % (offset, length))
        data = self._read_raw(aligned_start, aligned_end - aligned_start)
        self.bytes_read += len(data)
        self.read_calls += 1
        cut = offset - aligned_start
        return data[cut: cut + (end - offset)]

    def _close_handle(self) -> None:
        if getattr(self, "_handle", None):
            _k32.CloseHandle(self._handle)
            self._handle = None

    def close(self) -> None:
        self._close_handle()


class PosixRawSource(Source):
    """``/dev/sdX`` style device on Linux/macOS (read-only, unbuffered)."""

    kind = "raw"

    def __init__(self, path: str, label: str | None = None, sector_size: int = SECTOR_DEFAULT):
        size = os.stat(path).st_size
        if size <= 0:
            raise OSError("cannot determine size of %s" % path)
        super().__init__(path, label or path, size, sector_size)
        self._fh = open(path, "rb", buffering=0)
        self.sector_size_source = "assumed"

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset < 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        self._fh.seek(offset)
        data = self._fh.read(length)
        self.bytes_read += len(data)
        self.read_calls += 1
        return data

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def physical_drive_path(number: int) -> str:
    return r"\\.\PhysicalDrive%d" % int(number)


def volume_path(letter: str) -> str:
    letter = letter.strip().rstrip("\\").rstrip(":").upper()
    if len(letter) != 1:
        raise ValueError("bad drive letter %r" % letter)
    return r"\\.\%s:" % letter


def volume_disk_extents(path: str) -> list[tuple[int, int, int]]:
    """[(disk_number, starting_offset, length), ...] for a volume handle."""
    if not _IS_WINDOWS:
        return []
    src = None
    try:
        src = WindowsRawSource(path)
    except OSError:
        return []
    try:
        raw = src._ioctl(IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, 8 + 24 * 4)
    finally:
        src.close()
    if not raw or len(raw) < 8:
        return []
    count = ctypes.c_uint32.from_buffer_copy(raw[:4]).value
    count = min(int(count), 8)
    out = []
    for i in range(count):
        base = 8 + i * ctypes.sizeof(_DISK_EXTENT)
        if base + ctypes.sizeof(_DISK_EXTENT) > len(raw):
            break
        ext = _DISK_EXTENT.from_buffer_copy(raw[base: base + ctypes.sizeof(_DISK_EXTENT)])
        out.append((int(ext.DiskNumber), int(ext.StartingOffset), int(ext.ExtentLength)))
    return out


_VOL_SPEC = re.compile(r"^(?:[A-Za-z]:\\?)$")


def open_source(spec: str) -> Source:
    """Open ``disk:0`` / ``vol:E:`` / ``E:`` / ``\\\\.\\PhysicalDrive0`` / an image file."""
    raw = str(spec).strip().strip('"')
    low = raw.lower()

    if low.startswith("disk:") or low.startswith("pd:"):
        num = raw.split(":", 1)[1].strip()
        if not num.isdigit():
            raise ValueError("bad disk number %r" % num)
        return WindowsRawSource(physical_drive_path(int(num)), "PhysicalDrive" + num)

    if low.startswith("vol:"):
        return WindowsRawSource(volume_path(raw.split(":", 1)[1]), raw.split(":", 1)[1].upper())

    if low.startswith("img:"):
        return FileSource(raw.split(":", 1)[1].strip())

    if low.startswith("\\\\.\\"):
        label = raw.rsplit("\\", 1)[-1]
        return WindowsRawSource(raw, label)

    if _VOL_SPEC.match(raw):
        letter = raw[0].upper()
        return WindowsRawSource(volume_path(letter), letter + ":")

    path = os.path.expandvars(os.path.expanduser(raw))
    if os.path.isfile(path):
        return FileSource(path)
    if os.path.isdir(path):
        raise OSError(
            "%s is a directory on a live filesystem. unearth needs the raw "
            "device (\u201cvol:E:\u201d or \u201cdisk:2\u201d) or a disk image "
            "(.img/.dd) - deleted files are not visible through the filesystem."
            % path
        )
    if os.path.exists(path) or re.match(r"^[A-Za-z]:$", path):
        return WindowsRawSource(path, path)
    raise OSError("no such disk, volume or image: %s" % spec)
