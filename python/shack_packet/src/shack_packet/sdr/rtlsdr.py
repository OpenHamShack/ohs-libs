"""An RTL-SDR on this computer's USB, through librtlsdr.

**The binding is our own, over ctypes**, not pyrtlsdr: the part of
librtlsdr a receiver needs is fifteen C functions with plain arguments,
ctypes is in every Python, and so the desktop installers bundle only the C
library itself - nothing compiled for each Python version, nothing that
breaks when pyrtlsdr changes its packaging (it split the library out into
pyrtlsdrlib in 2023). It also makes the binding testable: the tests load a
stand-in librtlsdr built from C at test time (tests/sdrhelp.py) through
exactly this code, argument types and all.

What each desktop installer must carry (and the tester guide must say):

- **Windows**: `rtlsdr.dll` and `libusb-1.0.dll` beside the app (the
  rtl-sdr-blog or osmocom Windows release). And the dongle needs the WinUSB
  driver in place of Windows' own DVB-T driver, once per dongle per USB
  port: Zadig (zadig.akeo.ie), Options > List All Devices, pick "Bulk-In,
  Interface (Interface 0)" (the one with USB ID 0BDA 2838), WinUSB,
  Replace Driver. Without it `rtlsdr_open` fails with -3 or -12
  ("busy" or "no-device" below).
- **macOS**: `librtlsdr.dylib` and `libusb-1.0.dylib` (Homebrew's, with
  their install names rewritten to @loader_path). No driver: macOS leaves
  the dongle alone.
- **Linux**: the distribution's librtlsdr (Debian/Ubuntu: librtlsdr0) as a
  package dependency, its udev rule (rtl-sdr.rules: the plugdev group may
  open 0bda:2838), and the kernel's DVB driver kept off it - the package's
  blacklist of dvb_usb_rtl28xxu, or librtlsdr's "Kernel driver is active"
  error (-6, "busy" below).

Where the library is looked for, first found wins: `lib_path`, the
SHACK_RTLSDR_LIB environment variable, the app's bundled copy (the
directory it passes as `search`), then the system's (ctypes.util).

Choices:

- **read_sync on the runner's reader thread**, not read_async: an async
  callback is a C thread calling into Python through ctypes, which is
  fragile at shutdown on every platform; a blocking read of 256 KiB (55 ms
  at 2.4 MS/s) on a thread of our own is the same data with none of that.
- **The crystal correction stays 0** in the dongle: the channeliser
  corrects in software, to fractions of a ppm (librtlsdr takes whole ppm).
- **Gain**: None is the tuner's automatic gain; a number of dB picks the
  nearest step the tuner has (`gains`). The RTL2832's own digital AGC is
  off unless asked (`rtl_agc`): it pumps on strong signals.
- **Bias-tee is never touched.** It powers whatever is on the antenna
  socket, which is the operator's to decide, in the dongle's own tool.
- librtlsdr says nothing of USB overflows; the wall-clock check in
  IqSource counts them.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

import numpy as np

from .source import IqSource, SdrError, cu8_to_complex

LIB_NAMES = {"win32": ("rtlsdr.dll", "librtlsdr.dll"), "darwin": ("librtlsdr.dylib", "librtlsdr.0.dylib")}
DEFAULT_LIB_NAMES = ("librtlsdr.so.0", "librtlsdr.so", "librtlsdr.so.2")
READ_BYTES = 1 << 18     # 256 KiB: a multiple of librtlsdr's 512-byte USB transfer unit
#: librtlsdr's open() returns libusb's error codes.
_ERRNO_REASON = {-3: "busy", -4: "no-device", -6: "busy", -12: "no-device", -1: "open-failed"}

_SIGS = {
    "rtlsdr_get_device_count": ([], ctypes.c_uint32),
    "rtlsdr_get_device_name": ([ctypes.c_uint32], ctypes.c_char_p),
    "rtlsdr_get_device_usb_strings": ([ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p],
                                      ctypes.c_int),
    "rtlsdr_open": ([ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32], ctypes.c_int),
    "rtlsdr_close": ([ctypes.c_void_p], ctypes.c_int),
    "rtlsdr_set_center_freq": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
    "rtlsdr_get_center_freq": ([ctypes.c_void_p], ctypes.c_uint32),
    "rtlsdr_set_freq_correction": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    "rtlsdr_set_sample_rate": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
    "rtlsdr_get_sample_rate": ([ctypes.c_void_p], ctypes.c_uint32),
    "rtlsdr_get_tuner_gains": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
    "rtlsdr_set_tuner_gain_mode": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    "rtlsdr_set_tuner_gain": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    "rtlsdr_set_agc_mode": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    "rtlsdr_reset_buffer": ([ctypes.c_void_p], ctypes.c_int),
    "rtlsdr_read_sync": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)],
                         ctypes.c_int),
}


def _candidates(lib_path=None, search=()) -> list[str]:
    names = LIB_NAMES.get(sys.platform, DEFAULT_LIB_NAMES)
    out = []
    if lib_path:
        out.append(str(lib_path))
    if os.environ.get("SHACK_RTLSDR_LIB"):
        out.append(os.environ["SHACK_RTLSDR_LIB"])
    for d in search:
        out += [str(Path(d) / n) for n in names]
    found = ctypes.util.find_library("rtlsdr")
    if found:
        out.append(found)
    return out + list(names)


def load(lib_path=None, search=()) -> ctypes.CDLL:
    """librtlsdr with every function this module calls typed, or SdrError
    ("no-library") saying where it looked."""
    tried = []
    for cand in _candidates(lib_path, search):
        try:
            lib = ctypes.CDLL(cand)
        except OSError as e:
            tried.append(f"{cand} ({e})")
            continue
        try:
            for name, (args, res) in _SIGS.items():
                fn = getattr(lib, name)
                fn.argtypes, fn.restype = args, res
        except AttributeError as e:
            tried.append(f"{cand} ({e})")
            continue
        return lib
    raise SdrError("no-library", "librtlsdr not found: tried " + "; ".join(tried))


def devices(lib_path=None, search=()) -> list[dict]:
    """The dongles attached: index, name, and the USB strings (the serial
    tells two apart). librtlsdr opens each device briefly to read its
    strings; one another program holds gives empty strings, nothing worse."""
    lib = load(lib_path, search)
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        ok = lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0
        out.append({"index": i, "name": (lib.rtlsdr_get_device_name(i) or b"").decode("latin-1"),
                    "manufacturer": m.value.decode("latin-1") if ok else "",
                    "product": p.value.decode("latin-1") if ok else "",
                    "serial": s.value.decode("latin-1") if ok else ""})
    return out


class RtlSdrSource(IqSource):
    kind = "rtlsdr"

    def __init__(self, centre: float = 145_312_500.0, rate: float = 2_400_000, gain: float | None = None, *,
                 index: int = 0, serial: str | None = None, lib_path=None, search=(), rtl_agc: bool = False,
                 read_bytes: int = READ_BYTES):
        super().__init__(centre, rate, gain)
        self.index = index
        self.serial = serial
        self.lib_path, self.search = lib_path, tuple(search)
        self.rtl_agc = rtl_agc
        self.read_bytes = read_bytes
        self.lib = None
        self.dev = ctypes.c_void_p()
        self.gains: list[float] = []
        self._buf = ctypes.create_string_buffer(read_bytes)

    def _check(self, ret: int, what: str, reason: str = "setup-failed") -> None:
        if ret < 0:
            raise SdrError(reason, f"rtlsdr_{what} returned {ret}")

    def _open(self) -> None:
        self.lib = lib = load(self.lib_path, self.search)
        index = self.index
        if self.serial is not None:
            found = [d for d in devices(self.lib_path, self.search) if d["serial"] == self.serial]
            if not found:
                raise SdrError("no-device", f"no RTL-SDR with serial {self.serial}")
            index = found[0]["index"]
        if lib.rtlsdr_get_device_count() <= index:
            raise SdrError("no-device", f"no RTL-SDR at index {index}")
        ret = lib.rtlsdr_open(ctypes.byref(self.dev), index)
        if ret < 0 or not self.dev:
            raise SdrError(_ERRNO_REASON.get(ret, "open-failed"), f"rtlsdr_open({index}) returned {ret}")
        try:
            self._check(lib.rtlsdr_set_sample_rate(self.dev, int(self.rate)), "set_sample_rate", "rate-unsupported")
            self._check(lib.rtlsdr_set_center_freq(self.dev, int(round(self.centre))), "set_center_freq",
                        "out-of-range")
            n = lib.rtlsdr_get_tuner_gains(self.dev, None)
            if n > 0:
                arr = (ctypes.c_int * n)()
                lib.rtlsdr_get_tuner_gains(self.dev, arr)
                self.gains = [v / 10 for v in arr]
            self._apply_gain()
            self._check(lib.rtlsdr_set_agc_mode(self.dev, 1 if self.rtl_agc else 0), "set_agc_mode")
            self._check(lib.rtlsdr_reset_buffer(self.dev), "reset_buffer")
            got = lib.rtlsdr_get_sample_rate(self.dev)
            if got and abs(got - self.rate) > 1:
                self.stats.notes.append(f"asked for {self.rate:.0f} S/s, the dongle runs at {got} S/s")
        except Exception:
            self._close()
            raise

    def _apply_gain(self) -> None:
        lib = self.lib
        if self.gain is None or not self.gains:
            self._check(lib.rtlsdr_set_tuner_gain_mode(self.dev, 0), "set_tuner_gain_mode")
            return
        step = min(self.gains, key=lambda g: abs(g - self.gain))
        self._check(lib.rtlsdr_set_tuner_gain_mode(self.dev, 1), "set_tuner_gain_mode")
        self._check(lib.rtlsdr_set_tuner_gain(self.dev, int(round(step * 10))), "set_tuner_gain")

    def set_gain(self, gain: float | None) -> None:
        self.gain = gain
        if self.dev:
            self._apply_gain()

    def set_centre(self, hz: float) -> None:
        """Retune while running. The channels were placed for the old centre:
        the app makes a new Pipeline for the new one (ChannelPlan.centre_for)."""
        self.centre = float(hz)
        if self.dev:
            self._check(self.lib.rtlsdr_set_center_freq(self.dev, int(round(hz))), "set_center_freq", "out-of-range")

    def _read(self) -> np.ndarray:
        n = ctypes.c_int(0)
        ret = self.lib.rtlsdr_read_sync(self.dev, self._buf, self.read_bytes, ctypes.byref(n))
        if ret < 0:
            # -4 (LIBUSB_ERROR_NO_DEVICE): unplugged.
            raise SdrError("disconnected" if ret == -4 else "read-failed", f"rtlsdr_read_sync returned {ret}")
        # A short read is librtlsdr's timeout, not a loss: the samples that
        # did come are contiguous with the last. Real losses show on the
        # wall clock (IqSource.read).
        got = n.value // 2 * 2
        return cu8_to_complex(np.frombuffer(self._buf, np.uint8, got).copy())

    def _close(self) -> None:
        if self.lib is not None and self.dev:
            self.lib.rtlsdr_close(self.dev)
        self.dev = ctypes.c_void_p()

    def status(self) -> dict:
        st = super().status()
        st.update({"index": self.index, "serial": self.serial, "gains": self.gains})
        return st
