"""unearth / 拾遗 - dig forgotten photos, videos and documents out of old disks.

The package is deliberately dependency free: raw device access goes through
``ctypes`` + the Win32 API, all container/metadata parsers are hand written so
that a machine with nothing but CPython can still run a recovery.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
