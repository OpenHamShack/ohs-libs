"""A HackRF One as a packet radio: half-duplex FM through libhackrf, the
same modulator and receiver as PlutoLink's (D9, Brian 18/10/2026).

**Half-duplex.** A HackRF has one RF path: it receives until keyed, then
stops receiving (`hackrf_stop_rx`), transmits, and on the unkey stops
transmitting (`hackrf_stop_tx`, which puts the transceiver OFF) and starts
receiving again. The station hears nothing while it sends, which packet
never relies on. The switch costs a few tens of milliseconds a way, so
DEFAULT_TURNAROUND_S is 0.15 s until radios.turnaround measures it.

**Transmit**: radios.pluto.FmModulator at 2.4 MS/s, the channel `ifo_hz`
above the tuned frequency so the LO's leakage lands outside it, as 8-bit
I,Q (the HackRF's DAC format, 127 full scale, amplitude 0.7). libhackrf
pulls samples through a callback on its own transfer thread, 262 144 bytes
(55 ms) at a time with four transfers in flight; the callback modulates
from the audio the base class paces in, and when that runs short it sends
unmodulated carrier rather than a hole (the IC-9700 played 20 ms holes as
holes and nothing decoded, PlutoLink's "no holes mid-over"). Up to four
transfers are queued in libusb and the firmware's 32 KiB buffer, so what
the radio still has to play at the unkey (`_radio_drain_s`) counts them.

**What happens when the process dies mid-over** - read from the firmware
this HackRF runs (hackrf_usb via PortaPack Mayhem, USB API 0x0113, Sep
2026; firmware/hackrf_usb/usb_api_transceiver.c and sgpio_m0.s), NOT YET
SEEN ON A RADIO:

- libusb stops sending; the M0 core that feeds the DAC meets a *shortfall*
  and writes zeroes, so the **modulation stops at once**. Since the 2022
  firmware it no longer replays its old buffer; firmware older than that
  did [guess, from the M0 rewrite's history: check the PortaPack's version].
- But nothing sends "transceiver OFF": that only comes from the host
  (`hackrf_stop_tx`, `hackrf_close`). The M4 core's TX loop runs on, the
  MAX2837 and the mixer stay in TX and the RF amplifier stays as it was.
  So **an unmodulated carrier - the LO leakage at the tuned frequency, ifo_hz
  outside the channel - stays on** until the next program opens the HackRF
  or it is unplugged. Its level is the chip's carrier leakage, tens of dB
  below the over's, plus the amplifier if it was on.
- `hackrf_set_tx_underrun_limit` (USB API 0x0106+) makes the M0 go IDLE
  after that many missing bytes, but the M4 never acts on the M0's error,
  so the RF path is still not switched off; what the DAC holds after the
  M0 stops is not documented. This link sets it (UNDERRUN_LIMIT_S) because
  it can only help, and says so in the report rather than relying on it.

There is nothing on a HackRF to run a guard on, as the Pluto has
(shack_pluto.guard). So the protections are those this process can give,
in order: the base class's own watchdogs and the lock-free emergency unkey
(`hackrf_stop_tx` from any thread), atexit, and at every open the
transceiver put OFF before anything else (a HackRF a crashed run left
transmitting is quieted the next time the app opens it). And transmit is
refused - the link opens receive-only, "tx-firmware" - on firmware older
than USB API 0x0106, whose shortfall behaviour is the old one. A process
killed mid-over therefore leaves a weak carrier; session R4b (the keyed
checklist) measures how weak, on the RSPdx, into a dummy load.

**Receive**: radios.sdrrx.FmChannelReceiver on the HackRF's 8-bit IQ, LNA
and VGA gains as given (no AGC on a HackRF). libhackrf's RX callback only
copies the buffer onto a queue; a thread of this link's own runs the
receiver, so the transfer thread is never held by a demodulator.

**The library** is libhackrf (GPL-2.0-or-later, as this program), loaded
with ctypes as librtlsdr is (sdr/rtlsdr.py): Linux the distribution's
libhackrf0; Windows `hackrf.dll` and `libusb-1.0.dll` from Great Scott
Gadgets' release, and no driver to install - the firmware answers
Microsoft's WCID request, so Windows binds WinUSB by itself; macOS
`libhackrf.dylib` and `libusb-1.0.dylib` (Homebrew's, install names
rewritten). SHACK_HACKRF_LIB overrides, as SHACK_RTLSDR_LIB does.
"""

from __future__ import annotations

import collections
import ctypes
import ctypes.util
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .base import AudioRadioLink, LinkRefused
from .pluto import FmModulator
from .sdrrx import FmChannelReceiver

log = logging.getLogger(__name__)

AUDIO_RATE = 12_000
IQ_AMPLITUDE = 0.7
TX_PIECE_S = 0.02
#: libhackrf's own transfer size and count (hackrf.c TRANSFER_BUFFER_SIZE,
#: TRANSFER_COUNT), plus the firmware's 32 KiB sample buffer.
TRANSFER_BYTES = 262_144
TRANSFERS = 4
FIRMWARE_BUFFER = 32_768
UNDERRUN_LIMIT_S = 0.25
MIN_TX_API = 0x0106
TX_VGA_MAX = 47
LNA_STEPS = tuple(range(0, 41, 8))
VGA_STEPS = tuple(range(0, 63, 2))
#: m0 active_mode values that mean the DAC is being fed (hackrf_m0_state).
M0_TX_MODES = (3, 4)

LIB_NAMES = {"win32": ("hackrf.dll", "libhackrf.dll"), "darwin": ("libhackrf.dylib", "libhackrf.0.dylib")}
DEFAULT_LIB_NAMES = ("libhackrf.so.0", "libhackrf.so")


class Transfer(ctypes.Structure):
    _fields_ = [("device", ctypes.c_void_p), ("buffer", ctypes.POINTER(ctypes.c_uint8)),
                ("buffer_length", ctypes.c_int), ("valid_length", ctypes.c_int),
                ("rx_ctx", ctypes.c_void_p), ("tx_ctx", ctypes.c_void_p)]


class M0State(ctypes.Structure):
    _fields_ = [("requested_mode", ctypes.c_uint16), ("request_flag", ctypes.c_uint16),
                ("active_mode", ctypes.c_uint32), ("m0_count", ctypes.c_uint32), ("m4_count", ctypes.c_uint32),
                ("num_shortfalls", ctypes.c_uint32), ("longest_shortfall", ctypes.c_uint32),
                ("shortfall_limit", ctypes.c_uint32), ("threshold", ctypes.c_uint32),
                ("next_mode", ctypes.c_uint32), ("error", ctypes.c_uint32)]


CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(Transfer))
_P = ctypes.c_void_p
_SIGS = {
    "hackrf_init": ([], ctypes.c_int),
    "hackrf_exit": ([], ctypes.c_int),
    "hackrf_open": ([ctypes.POINTER(_P)], ctypes.c_int),
    "hackrf_open_by_serial": ([ctypes.c_char_p, ctypes.POINTER(_P)], ctypes.c_int),
    "hackrf_close": ([_P], ctypes.c_int),
    "hackrf_start_rx": ([_P, CALLBACK, _P], ctypes.c_int),
    "hackrf_stop_rx": ([_P], ctypes.c_int),
    "hackrf_start_tx": ([_P, CALLBACK, _P], ctypes.c_int),
    "hackrf_stop_tx": ([_P], ctypes.c_int),
    "hackrf_is_streaming": ([_P], ctypes.c_int),
    "hackrf_set_freq": ([_P, ctypes.c_uint64], ctypes.c_int),
    "hackrf_set_sample_rate": ([_P, ctypes.c_double], ctypes.c_int),
    "hackrf_set_baseband_filter_bandwidth": ([_P, ctypes.c_uint32], ctypes.c_int),
    "hackrf_set_lna_gain": ([_P, ctypes.c_uint32], ctypes.c_int),
    "hackrf_set_vga_gain": ([_P, ctypes.c_uint32], ctypes.c_int),
    "hackrf_set_txvga_gain": ([_P, ctypes.c_uint32], ctypes.c_int),
    "hackrf_set_amp_enable": ([_P, ctypes.c_uint8], ctypes.c_int),
    "hackrf_usb_api_version_read": ([_P, ctypes.POINTER(ctypes.c_uint16)], ctypes.c_int),
    "hackrf_version_string_read": ([_P, ctypes.c_char_p, ctypes.c_uint8], ctypes.c_int),
    "hackrf_set_tx_underrun_limit": ([_P, ctypes.c_uint32], ctypes.c_int),
    "hackrf_get_m0_state": ([_P, ctypes.POINTER(M0State)], ctypes.c_int),
    "hackrf_error_name": ([ctypes.c_int], ctypes.c_char_p),
}
#: Present in older libraries or not at all: the link carries on without.
_OPTIONAL = {"hackrf_set_tx_underrun_limit", "hackrf_get_m0_state", "hackrf_open_by_serial"}


def load(lib_path=None, search=()) -> ctypes.CDLL:
    names = LIB_NAMES.get(sys.platform, DEFAULT_LIB_NAMES)
    cands = [str(lib_path)] if lib_path else []
    if os.environ.get("SHACK_HACKRF_LIB"):
        cands.append(os.environ["SHACK_HACKRF_LIB"])
    for d in search:
        cands += [str(Path(d) / n) for n in names]
    found = ctypes.util.find_library("hackrf")
    cands += ([found] if found else []) + list(names)
    tried = []
    for cand in cands:
        try:
            lib = ctypes.CDLL(cand)
        except OSError as e:
            tried.append(f"{cand} ({e})")
            continue
        missing = []
        for name, (args, res) in _SIGS.items():
            fn = getattr(lib, name, None)
            if fn is None:
                missing.append(name)
                continue
            fn.argtypes, fn.restype = args, res
        if [m for m in missing if m not in _OPTIONAL]:
            tried.append(f"{cand} (missing {', '.join(missing)})")
            continue
        lib.shack_missing = set(missing)
        return lib
    raise LinkRefused("setup", "libhackrf not found: tried " + "; ".join(tried))


def iq_int8(iq: np.ndarray) -> bytes:
    out = np.empty(2 * len(iq), np.int8)
    out[0::2] = np.round(np.clip(iq.real, -1.0, 1.0) * 127.0).astype(np.int8)
    out[1::2] = np.round(np.clip(iq.imag, -1.0, 1.0) * 127.0).astype(np.int8)
    return out.tobytes()


class HackRFLink(AudioRadioLink):
    """`frequency` is the channel on the air (Hz); `serial` picks one HackRF
    of several; `tx_vga_db` 0-47 and `amp` the 14 dB RF amplifier (off by
    default: a PA after a band filter is the way to power, D9's warning);
    `lna_db`/`vga_db` the receive gains."""

    rate = AUDIO_RATE
    DEFAULT_TURNAROUND_S = 0.15

    def __init__(self, frequency: float | None = None, *, serial: str | None = None, ifo_hz: float = 100_000.0,
                 rate_iq: int = 2_400_000, deviation_hz: float = 3_000.0, tx_vga_db: int = 20, amp: bool = False,
                 lna_db: int = 16, vga_db: int = 20, deemphasis_us: float | None = None, transmit: bool = True,
                 lib_path=None, search=(), **kw):
        super().__init__(**kw)
        if not frequency:
            raise LinkRefused("setup", "a HackRF link needs the channel's frequency")
        if not 1e6 <= frequency <= 6e9:
            raise LinkRefused("out-of-range", f"a HackRF tunes 1 MHz-6 GHz, not {frequency / 1e6:.3f} MHz")
        if not 0 <= int(tx_vga_db) <= TX_VGA_MAX:
            raise LinkRefused("setup", f"TX gain {tx_vga_db} dB is outside 0-{TX_VGA_MAX} dB")
        self.frequency = float(frequency)
        self.serial = serial
        self.ifo_hz = float(ifo_hz)
        self.lo_hz = int(round(self.frequency - self.ifo_hz))
        self.rate_iq = int(rate_iq)
        self.deviation_hz = float(deviation_hz)
        self.tx_vga_db = int(tx_vga_db)
        self.amp = bool(amp)
        self.lna_db = min(LNA_STEPS, key=lambda g: abs(g - lna_db))
        self.vga_db = min(VGA_STEPS, key=lambda g: abs(g - vga_db))
        self.deemphasis_us = deemphasis_us
        self.lib_path, self.search = lib_path, tuple(search)
        #: False: receive-only by the app's choice (transmit not set up).
        self.transmit = bool(transmit)
        self.lib = None
        self.dev = ctypes.c_void_p()
        self.api_version = 0
        self.firmware = ""
        #: Why transmit is off, or None (module docstring: old firmware).
        self.tx_refused_why: str | None = None
        self.receiver: FmChannelReceiver | None = None
        self._mod = FmModulator(self.rate_iq, deviation_hz, ifo_hz, amplitude=IQ_AMPLITUDE)
        # Kept referenced for as long as libhackrf may call them.
        self._rx_cb = CALLBACK(self._on_rx)
        self._tx_cb = CALLBACK(self._on_tx)
        self._iq_q: queue.Queue = queue.Queue(maxsize=64)
        self._iq_stop = threading.Event()
        self._iq_thread: threading.Thread | None = None
        self._tx_audio: collections.deque = collections.deque()
        self._tx_audio_n = 0
        self._pending = bytearray()
        self._tx_bytes_out = 0
        self._tx_started_at = 0.0
        self._mode = "off"
        self._hw_lock = threading.Lock()

    # -- the library ---------------------------------------------------------------

    def _check(self, ret: int, what: str, reason: str = "radio-error") -> None:
        if ret < 0:
            name = self.lib.hackrf_error_name(ret) if self.lib is not None else b""
            raise LinkRefused(reason, f"hackrf_{what}: {ret} {(name or b'').decode('latin-1')}")

    @property
    def level_db(self) -> float | None:
        return self.receiver.level_db if self.receiver is not None else None

    # -- open and close ------------------------------------------------------------

    def _open_radio(self) -> None:
        self.lib = lib = load(self.lib_path, self.search)
        self._check(lib.hackrf_init(), "init", "setup")
        if self.serial and "hackrf_open_by_serial" not in lib.shack_missing:
            ret = lib.hackrf_open_by_serial(self.serial.encode(), ctypes.byref(self.dev))
        else:
            ret = lib.hackrf_open(ctypes.byref(self.dev))
        if ret < 0 or not self.dev:
            lib.hackrf_exit()
            raise LinkRefused("radio-error", f"no HackRF could be opened ({ret}); is another program using it?")
        try:
            # A HackRF a crashed run left transmitting goes OFF before
            # anything else (module docstring).
            lib.hackrf_stop_tx(self.dev)
            v = ctypes.c_uint16(0)
            if lib.hackrf_usb_api_version_read(self.dev, ctypes.byref(v)) >= 0:
                self.api_version = v.value
            buf = ctypes.create_string_buffer(255)
            if lib.hackrf_version_string_read(self.dev, buf, 255) >= 0:
                self.firmware = buf.value.decode("latin-1")
            if self.api_version < MIN_TX_API or "hackrf_set_tx_underrun_limit" in lib.shack_missing:
                self.tx_refused_why = (f"firmware {self.firmware or '?'} (USB API {self.api_version:#06x}) is older "
                                       f"than {MIN_TX_API:#06x}: update it to transmit")
                log.warning("HackRF opens receive-only: %s", self.tx_refused_why)
                self._event("radio", "tx-firmware", {"why": self.tx_refused_why})
            self._check(lib.hackrf_set_sample_rate(self.dev, float(self.rate_iq)), "set_sample_rate", "setup")
            self._check(lib.hackrf_set_baseband_filter_bandwidth(self.dev, 1_750_000), "set_baseband_filter", "setup")
            self._check(lib.hackrf_set_freq(self.dev, self.lo_hz), "set_freq", "out-of-range")
            self.receiver = FmChannelReceiver(self.rate_iq, self.lo_hz, self.frequency,
                                              deviation_hz=self.deviation_hz, deemphasis_us=self.deemphasis_us)
            self._iq_stop.clear()
            self._iq_thread = threading.Thread(target=self._read_iq, name="HackRFLink IQ", daemon=True)
            self._iq_thread.start()
            self._start_rx()
        except BaseException:
            self._release()
            raise

    def _close_radio(self) -> None:
        self._release()

    def _release(self) -> None:
        lib, dev = self.lib, self.dev
        if lib is not None and dev:
            with self._hw_lock:
                lib.hackrf_stop_tx(dev)              # OFF, whatever it was doing
                self._mode = "off"
                lib.hackrf_close(dev)
            lib.hackrf_exit()
        self.dev = ctypes.c_void_p()
        self._iq_stop.set()
        try:
            self._iq_q.put_nowait(None)
        except queue.Full:
            pass
        t, self._iq_thread = self._iq_thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    # -- receive -------------------------------------------------------------------

    def _start_rx(self) -> None:
        lib = self.lib
        self._check(lib.hackrf_set_amp_enable(self.dev, 0), "set_amp_enable")
        self._check(lib.hackrf_set_lna_gain(self.dev, self.lna_db), "set_lna_gain")
        self._check(lib.hackrf_set_vga_gain(self.dev, self.vga_db), "set_vga_gain")
        self._check(lib.hackrf_start_rx(self.dev, self._rx_cb, None), "start_rx")
        self._mode = "rx"

    def _on_rx(self, t) -> int:
        tr = t.contents
        try:
            self._iq_q.put_nowait(ctypes.string_at(tr.buffer, tr.valid_length))
        except queue.Full:
            self.stats["rx_overflow"] += 1
        return 0

    def _read_iq(self) -> None:
        while not self._iq_stop.is_set():
            raw = self._iq_q.get()
            if raw is None or self._iq_stop.is_set():
                return
            if self._mode != "rx":
                continue                              # the tail of a receive that has been stopped
            x = np.frombuffer(raw, np.int8).astype(np.float32) / 128.0
            pcm = self.receiver.hear(x.view(np.complex64))
            if pcm:
                self._rx(pcm)

    def _radio_busy_now(self) -> bool | None:
        if self._mode != "rx" or self.receiver is None:
            return None
        return self.receiver.busy

    # -- transmit ------------------------------------------------------------------

    def _refusal(self) -> str | None:
        why = super()._refusal()
        if why is None and not self.transmit:
            return "receive-only"
        if why is None and self.tx_refused_why is not None:
            return "tx-firmware"
        return why

    def _radio_key(self) -> None:
        lib = self.lib
        if lib is None or not self.dev:
            raise LinkRefused("not-open")
        with self._hw_lock:
            self._check(lib.hackrf_stop_rx(self.dev), "stop_rx", "radio-refused")
            self._mode = "keying"
            self._tx_audio.clear()
            self._tx_audio_n = 0
            self._pending = bytearray()
            self._tx_bytes_out = 0
            limit = int(UNDERRUN_LIMIT_S * self.rate_iq * 2)
            self._check(lib.hackrf_set_tx_underrun_limit(self.dev, limit), "set_tx_underrun_limit", "radio-refused")
            self._check(lib.hackrf_set_txvga_gain(self.dev, self.tx_vga_db), "set_txvga_gain", "radio-refused")
            self._check(lib.hackrf_set_amp_enable(self.dev, 1 if self.amp else 0), "set_amp_enable", "radio-refused")
            self._tx_started_at = time.monotonic()
            self._check(lib.hackrf_start_tx(self.dev, self._tx_cb, None), "start_tx", "radio-refused")
            self._mode = "tx"

    def _radio_audio(self, pcm: bytes) -> bool:
        if self._mode != "tx":
            return False
        audio = np.frombuffer(pcm, "<i2").astype(np.float64) / 32768.0
        self._tx_audio.append(audio)
        self._tx_audio_n += len(audio)
        return True

    def _on_tx(self, t) -> int:
        """libhackrf's transfer thread: fill one buffer (module docstring)."""
        tr = t.contents
        need = tr.buffer_length
        piece = int(AUDIO_RATE * TX_PIECE_S)
        try:
            while len(self._pending) < need:
                if self._tx_audio:
                    a = self._tx_audio.popleft()
                    self._tx_audio_n = max(0, self._tx_audio_n - len(a))
                else:
                    a = np.zeros(piece)
                    self.stats["tx_fill_s"] += TX_PIECE_S
                self._pending += iq_int8(self._mod(a))
            ctypes.memmove(tr.buffer, bytes(self._pending[:need]), need)
            del self._pending[:need]
            tr.valid_length = need
            self._tx_bytes_out += need
        except Exception:                           # noqa: BLE001 - never raise into C; zeros instead
            log.exception("HackRF transmit callback")
            ctypes.memset(tr.buffer, 0, need)
            tr.valid_length = need
        return 0

    def _radio_drain_s(self) -> float:
        queued = self._tx_audio_n / AUDIO_RATE + len(self._pending) / 2 / self.rate_iq
        in_flight = (TRANSFERS * TRANSFER_BYTES + FIRMWARE_BUFFER) / 2 / self.rate_iq
        return queued + in_flight

    def _radio_audio_abort(self) -> None:
        self._tx_audio.clear()
        self._tx_audio_n = 0

    def _radio_unkey(self) -> None:
        lib = self.lib
        if lib is None or not self.dev:
            return
        with self._hw_lock:
            was = self._mode
            ret = lib.hackrf_stop_tx(self.dev)      # transceiver OFF: the RF path off
            self._check(ret, "stop_tx")
            self._mode = "off"
            if was in ("tx", "keying") and self._open:
                self._start_rx()

    # -- confirmation and the way out ------------------------------------------------

    @property
    def CONFIRMS_UNKEY(self) -> bool:            # noqa: N802 - the base's name
        return self.lib is not None and "hackrf_get_m0_state" not in self.lib.shack_missing

    def _radio_ptt_readback(self) -> bool | None:
        """The firmware's own M0 mode: TX_START or TX_RUN is transmitting.
        After hackrf_stop_tx the firmware puts the radio OFF and the M0 IDLE
        together, so IDLE confirms an unkey; it does not prove the RF path
        is off after an underrun stop (module docstring)."""
        if self.lib is None or not self.dev:
            return None
        st = M0State()
        if self.lib.hackrf_get_m0_state(self.dev, ctypes.byref(st)) < 0:
            return None
        return st.active_mode in M0_TX_MODES

    def _emergency_unkey(self) -> None:
        """hackrf_stop_tx from any thread, without the link's lock or the
        hardware lock a stuck key may hold: libhackrf's own transfer lock
        is all it needs."""
        if self.lib is not None and self.dev:
            ret = self.lib.hackrf_stop_tx(self.dev)
            if ret < 0:
                raise OSError(f"hackrf_stop_tx returned {ret}")
