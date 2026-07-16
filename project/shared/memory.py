"""Small Linux helpers for observing and releasing transient analytics memory."""
from __future__ import annotations

import ctypes
import gc
from pathlib import Path


def current_rss_mb() -> float | None:
    """Read the current resident set size from /proc without extra dependencies."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


def release_unused_memory() -> float | None:
    """Collect Python cycles and return free glibc arenas to Railway when possible."""
    gc.collect()
    try:
        libc = ctypes.CDLL("libc.so.6")
        malloc_trim = libc.malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        malloc_trim(0)
    except (OSError, AttributeError):
        pass
    return current_rss_mb()
