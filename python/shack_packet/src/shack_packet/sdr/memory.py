"""Keep glibc from handing the heap back between blocks (Linux only).

Found while measuring the channeliser (18/10/2026, shack): the same code
on the same kind of data cost 23-30 ms a second of 2.4 MS/s IQ in one
process and 86 ms in another, depending only on what had been allocated
before. The difference was all page faults - 42,000 a second, 56 ms of
system time - from numpy's FFT, whose scratch for a 64,000-point
transform is megabytes a call: freed to the top of glibc's heap, the heap
was trimmed, and the next block's FFT faulted the same pages back in,
zeroed, forty times a second. Which regime a process lands in depends on
glibc's sliding mmap threshold, i.e. on the history of the process, so it
cannot be left to chance in a desktop app.

`M_TOP_PAD` of 64 MiB (glibc keeps that much slack at the heap's top
instead of trimming to the byte) takes the faults to none: 23 ms a second,
every time. Measured alternatives: a 16 MiB pad, 44 ms; a fixed 32 MiB
mmap threshold with a 128 MiB trim threshold, 27 ms; numpy's fft with
`out=` and contiguous rows, no better (the scratch is numpy's own).

The cost is up to 64 MiB of address space held at the heap's top, which a
desktop app running sixteen channels can spare. It is process-wide, so it
is done once, when a Pipeline is made, and never undone. macOS and Windows
have other allocators (not measurable here); `keep_heap()` does nothing
there and says so, and the CPU figures there are for the testers' machines
to confirm.
"""

from __future__ import annotations

import ctypes
import sys

M_TOP_PAD = -2
TOP_PAD = 64 << 20

_done: bool | None = None


def keep_heap() -> bool:
    """Set glibc's top pad once; True if it is in effect."""
    global _done
    if _done is not None:
        return _done
    _done = False
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None)
            if hasattr(libc, "gnu_get_libc_version") and hasattr(libc, "mallopt"):
                _done = libc.mallopt(M_TOP_PAD, TOP_PAD) == 1
        except OSError:
            pass
    return _done
