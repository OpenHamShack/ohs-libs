"""An SDRplay RSP, through the SDRplay API the user installed (D9).

Moved from shack_packet.sdr.sdrplay (RADIO-SDR-PLAN §6.1), behaviour
unchanged. Added for Radio (§6.3, Brian 22/10/2026: the API is use if
installed on every form): on a controller the API is the host library the
administrator installed, mounted at $SHACK_HOSTLIBS/sdrplay-api/, looked
for first; `availability()` says, without opening anything, whether the
API is there, as the code "sdrplay_api_missing" the app words as "Install
SDRplay's API to use this radio" (or, on a controller, "Ask your
controller's administrator to install SDRplay's API"); and the rates to
offer (2.4 MS/s: SoapySDRPlay3 refused 1.2, and the RSPdx on ohs-box has
only ever run at 2.4).

**Never bundled.** SDRplay's licence grants no explicit right to
redistribute the API (GUIDE §5, "An SDRplay RSP in an app container"), so
no OpenHamShack installer carries it: the operator installs SDRplay's own
API package (Windows: "SDRplay API 3.x" from sdrplay.com, which also
installs and starts its service; macOS and Linux: the same page's
installer), and this module loads it at run time, from where that installer
puts it, or from SHACK_SDRPLAY_LIB. Not found is SdrError "no-library",
which the app words as "install SDRplay's API". The binding is ours, over
ctypes, as librtlsdr's is (sdr/rtlsdr.py): only the API's C library is
needed, nothing compiled per Python version.

**SDRconnect competes.** SDRconnect does not use the API (it drives the RSP
over libusb itself), and the API's service holds the radio while anything
uses it: a radio one of them has is "busy" to the other (memory,
"SDRconnect bypasses the SDRplay API").

**The parts of the API used** (sdrplay_api.h 3.15, the version on shack):
Open, ApiVersion (major 3 required), LockDeviceApi, GetDevices,
SelectDevice, UnlockDeviceApi, GetDeviceParams, Init with a stream and an
event callback, Uninit, ReleaseDevice, Close. Only the leading members of
the parameter structures are declared here (`DevParams`: ppm and fsFreq;
`RxChannelParams`: tunerParams and ctrlParams), because only those are
written and a pointer to a prefix of a C struct reaches the same memory;
the tests compile a stand-in API against SDRplay's own headers, where they
are installed, and check that what this module writes lands in the fields
the headers name.

**Settings**: zero IF, 1.536 MHz bandwidth, no decimation, so the stream
arrives at `rate` (2.4 MS/s by default, as the RTL-SDR). Gain None is the
API's own AGC at 50 Hz with the LNA at `lna_state`; a number is a fixed
gain *reduction* in dB (SDRplay's own sense, 20-59) with AGC off. The
crystal correction stays 0 in the radio: the channeliser corrects ppm, as
for every source. Bias-T and the RSPdx's antenna choice are left as they
are, which the operator sets in SDRplay's own tool.

**The stream** arrives as two arrays of int16, I and Q, on the API's own
thread; the callback copies them onto a queue and returns, and `_read`
takes from it. A "device removed" event, or no samples for READ_TIMEOUT_S,
is SdrError "disconnected" / "read-failed".

NOT YET RUN ON AN RSP: shack's RSPdx belongs to the apps that use it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import sys

import numpy as np

from .source import IqSource, SdrError

READ_TIMEOUT_S = 3.0
BLOCK_S = 0.05
QUEUE_BLOCKS = 64
BW_1_536 = 1536
IF_ZERO = 0
LO_AUTO = 1
AGC_DISABLE, AGC_50HZ = 0, 2
TUNER_A = 1
UPDATE_TUNER_GR, UPDATE_TUNER_FRF, UPDATE_DEV_FS, UPDATE_CTRL_AGC = 0x8000, 0x20000, 0x1, 0x1000000
EVENT_DEVICE_REMOVED = 2
GRDB_RANGE = (20, 59)

LIB_PLACES = {
    "win32": [r"C:\Program Files\SDRplay\API\x64\sdrplay_api.dll", "sdrplay_api.dll"],
    "darwin": ["/usr/local/lib/libsdrplay_api.so.3", "/usr/local/lib/libsdrplay_api.dylib"],
}
DEFAULT_PLACES = ["/usr/local/lib/libsdrplay_api.so.3", "/usr/local/lib/libsdrplay_api.so", "libsdrplay_api.so.3"]


class DeviceT(ctypes.Structure):
    _fields_ = [("SerNo", ctypes.c_char * 64), ("hwVer", ctypes.c_ubyte), ("tuner", ctypes.c_int),
                ("rspDuoMode", ctypes.c_int), ("valid", ctypes.c_ubyte), ("rspDuoSampleFreq", ctypes.c_double),
                ("dev", ctypes.c_void_p)]


class FsFreqT(ctypes.Structure):
    _fields_ = [("fsHz", ctypes.c_double), ("syncUpdate", ctypes.c_ubyte), ("reCal", ctypes.c_ubyte)]


class DevParamsPrefix(ctypes.Structure):
    _fields_ = [("ppm", ctypes.c_double), ("fsFreq", FsFreqT)]


class GainT(ctypes.Structure):
    _fields_ = [("gRdB", ctypes.c_int), ("LNAstate", ctypes.c_ubyte), ("syncUpdate", ctypes.c_ubyte),
                ("minGr", ctypes.c_int), ("curr", ctypes.c_float), ("max", ctypes.c_float), ("min", ctypes.c_float)]


class RfFreqT(ctypes.Structure):
    _fields_ = [("rfHz", ctypes.c_double), ("syncUpdate", ctypes.c_ubyte)]


class DcOffsetTunerT(ctypes.Structure):
    _fields_ = [("dcCal", ctypes.c_ubyte), ("speedUp", ctypes.c_ubyte), ("trackTime", ctypes.c_int),
                ("refreshRateTime", ctypes.c_int)]


class TunerParamsT(ctypes.Structure):
    _fields_ = [("bwType", ctypes.c_int), ("ifType", ctypes.c_int), ("loMode", ctypes.c_int), ("gain", GainT),
                ("rfFreq", RfFreqT), ("dcOffsetTuner", DcOffsetTunerT)]


class ControlParamsT(ctypes.Structure):
    _fields_ = [("DCenable", ctypes.c_ubyte), ("IQenable", ctypes.c_ubyte),
                ("decEnable", ctypes.c_ubyte), ("decimationFactor", ctypes.c_ubyte), ("wideBandSignal", ctypes.c_ubyte),
                ("agcEnable", ctypes.c_int), ("setPoint_dBfs", ctypes.c_int), ("attack_ms", ctypes.c_ushort),
                ("decay_ms", ctypes.c_ushort), ("decay_delay_ms", ctypes.c_ushort),
                ("decay_threshold_dB", ctypes.c_ushort), ("agcSyncUpdate", ctypes.c_int), ("adsbMode", ctypes.c_int)]


class RxChannelPrefix(ctypes.Structure):
    _fields_ = [("tunerParams", TunerParamsT), ("ctrlParams", ControlParamsT)]


class DeviceParamsT(ctypes.Structure):
    _fields_ = [("devParams", ctypes.POINTER(DevParamsPrefix)), ("rxChannelA", ctypes.POINTER(RxChannelPrefix)),
                ("rxChannelB", ctypes.POINTER(RxChannelPrefix))]


STREAM_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_short), ctypes.POINTER(ctypes.c_short), ctypes.c_void_p,
                             ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
EVENT_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class CallbackFnsT(ctypes.Structure):
    _fields_ = [("StreamACbFn", STREAM_CB), ("StreamBCbFn", STREAM_CB), ("EventCbFn", EVENT_CB)]


_E = ctypes.c_int
_SIGS = {
    "sdrplay_api_Open": ([], _E), "sdrplay_api_Close": ([], _E),
    "sdrplay_api_ApiVersion": ([ctypes.POINTER(ctypes.c_float)], _E),
    "sdrplay_api_LockDeviceApi": ([], _E), "sdrplay_api_UnlockDeviceApi": ([], _E),
    "sdrplay_api_GetDevices": ([ctypes.POINTER(DeviceT), ctypes.POINTER(ctypes.c_uint), ctypes.c_uint], _E),
    "sdrplay_api_SelectDevice": ([ctypes.POINTER(DeviceT)], _E),
    "sdrplay_api_ReleaseDevice": ([ctypes.POINTER(DeviceT)], _E),
    "sdrplay_api_GetErrorString": ([_E], ctypes.c_char_p),
    "sdrplay_api_GetDeviceParams": ([ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(DeviceParamsT))], _E),
    "sdrplay_api_Init": ([ctypes.c_void_p, ctypes.POINTER(CallbackFnsT), ctypes.c_void_p], _E),
    "sdrplay_api_Uninit": ([ctypes.c_void_p], _E),
    "sdrplay_api_Update": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int], _E),
}


def _hostlib_places() -> list[str]:
    root = os.environ.get("SHACK_HOSTLIBS")
    if not root:
        return []
    d = os.path.join(root, "sdrplay-api")
    return [os.path.join(d, n) for n in ("libsdrplay_api.so.3", "libsdrplay_api.so")]


def load(lib_path=None) -> ctypes.CDLL:
    places = ([str(lib_path)] if lib_path else []) + \
        ([os.environ["SHACK_SDRPLAY_LIB"]] if os.environ.get("SHACK_SDRPLAY_LIB") else []) + \
        _hostlib_places() + LIB_PLACES.get(sys.platform, DEFAULT_PLACES)
    found = ctypes.util.find_library("sdrplay_api")
    if found:
        places.append(found)
    tried = []
    for cand in places:
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
    raise SdrError("no-library", "SDRplay's API is not installed (it is never bundled): tried " + "; ".join(tried))


class SdrplaySource(IqSource):
    kind = "sdrplay"
    RATES = (2_400_000.0,)

    def __init__(self, centre: float = 145_312_500.0, rate: float = 2_400_000, gain: float | None = None, *,
                 serial: str | None = None, lna_state: int = 3, lib_path=None):
        super().__init__(centre, rate, gain)
        self.serial = serial
        self.lna_state = int(lna_state)
        self.lib_path = lib_path
        self.lib = None
        self.device: DeviceT | None = None
        self.params = None
        self.api_version = 0.0
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._gather: list = []
        self._gathered = 0
        self._removed = False
        self._cbs = CallbackFnsT(STREAM_CB(self._on_stream), STREAM_CB(lambda *a: None), EVENT_CB(self._on_event))
        self._opened_api = self._selected = self._running = False

    def _check(self, err: int, what: str, reason: str = "setup-failed") -> None:
        if err != 0:
            words = self.lib.sdrplay_api_GetErrorString(err) if self.lib is not None else b""
            raise SdrError(reason, f"sdrplay_api_{what}: {err} {(words or b'').decode('latin-1')}")

    def _open(self) -> None:
        self.lib = lib = load(self.lib_path)
        try:
            # Open fails when the API's service is not running (Windows: its
            # service; Linux and macOS: sdrplay_apiService).
            self._check(lib.sdrplay_api_Open(), "Open", "open-failed")
            self._opened_api = True
            ver = ctypes.c_float(0)
            self._check(lib.sdrplay_api_ApiVersion(ctypes.byref(ver)), "ApiVersion", "no-library")
            self.api_version = round(ver.value, 2)
            if int(self.api_version) != 3:
                raise SdrError("no-library", f"SDRplay API {self.api_version} installed; version 3.x is needed")
            self._check(lib.sdrplay_api_LockDeviceApi(), "LockDeviceApi", "busy")
            try:
                devs = (DeviceT * 8)()
                n = ctypes.c_uint(0)
                self._check(lib.sdrplay_api_GetDevices(devs, ctypes.byref(n), 8), "GetDevices", "no-device")
                pick = [d for d in devs[:n.value] if self.serial is None or d.SerNo.decode() == self.serial]
                if not pick:
                    raise SdrError("no-device", "no SDRplay RSP found" if self.serial is None
                                   else f"no SDRplay RSP with serial {self.serial}")
                self.device = pick[0]
                if self.device.tuner == 0:
                    self.device.tuner = TUNER_A
                self._check(lib.sdrplay_api_SelectDevice(ctypes.byref(self.device)), "SelectDevice", "busy")
                self._selected = True
            finally:
                lib.sdrplay_api_UnlockDeviceApi()
            p = ctypes.POINTER(DeviceParamsT)()
            self._check(lib.sdrplay_api_GetDeviceParams(self.device.dev, ctypes.byref(p)), "GetDeviceParams")
            self.params = p.contents
            dev = self.params.devParams.contents
            dev.fsFreq.fsHz = float(self.rate)
            ch = self.params.rxChannelA.contents
            ch.tunerParams.bwType = BW_1_536
            ch.tunerParams.ifType = IF_ZERO
            ch.tunerParams.loMode = LO_AUTO
            ch.tunerParams.rfFreq.rfHz = float(self.centre)
            ch.ctrlParams.decEnable = 0
            self._set_gain_fields(ch)
            self._check(lib.sdrplay_api_Init(self.device.dev, ctypes.byref(self._cbs), None), "Init", "open-failed")
            self._running = True
        except Exception:
            self._close()
            raise

    def _set_gain_fields(self, ch) -> None:
        ch.tunerParams.gain.LNAstate = self.lna_state
        if self.gain is None:
            ch.ctrlParams.agcEnable = AGC_50HZ
            ch.ctrlParams.setPoint_dBfs = -30
        else:
            ch.ctrlParams.agcEnable = AGC_DISABLE
            ch.tunerParams.gain.gRdB = int(min(max(round(self.gain), GRDB_RANGE[0]), GRDB_RANGE[1]))

    def set_gain(self, gain: float | None) -> None:
        self.gain = gain
        if self._running:
            self._set_gain_fields(self.params.rxChannelA.contents)
            self._check(self.lib.sdrplay_api_Update(self.device.dev, TUNER_A, UPDATE_TUNER_GR | UPDATE_CTRL_AGC, 0),
                        "Update")

    def set_centre(self, hz: float) -> None:
        self.centre = float(hz)
        if self._running:
            self.params.rxChannelA.contents.tunerParams.rfFreq.rfHz = float(hz)
            self._check(self.lib.sdrplay_api_Update(self.device.dev, TUNER_A, UPDATE_TUNER_FRF, 0), "Update",
                        "out-of-range")

    def _on_stream(self, xi, xq, params, n, reset, ctx) -> None:
        # The API calls back with a USB packet's worth (about 1300 samples
        # at 2.4 MS/s, some 1800 times a second); handed on one by one, the
        # per-block cost downstream outruns the stream. So they are gathered
        # into BLOCK_S blocks here, as librtlsdr's read is (256 KiB).
        try:
            z = np.empty(n, np.complex64)
            z.real = np.ctypeslib.as_array(xi, (n,)) / 32768.0
            z.imag = np.ctypeslib.as_array(xq, (n,)) / 32768.0
            self._gather.append(z)
            self._gathered += n
            if self._gathered >= self.rate * BLOCK_S:
                block = np.concatenate(self._gather)
                self._gather, self._gathered = [], 0
                try:
                    self._q.put_nowait(block)
                except queue.Full:
                    self.lose(len(block))
        except Exception:                           # noqa: BLE001 - never raise into the API's thread
            pass

    def _on_event(self, event, tuner, params, ctx) -> None:
        if event == EVENT_DEVICE_REMOVED:
            self._removed = True
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    def _read(self) -> np.ndarray:
        try:
            z = self._q.get(timeout=READ_TIMEOUT_S)
        except queue.Empty:
            raise SdrError("read-failed", f"no samples from the RSP for {READ_TIMEOUT_S:.0f} s") from None
        if z is None or self._removed:
            raise SdrError("disconnected", "the RSP was unplugged")
        return z

    def _close(self) -> None:
        lib = self.lib
        if lib is None:
            return
        if self._running:
            lib.sdrplay_api_Uninit(self.device.dev)
            self._running = False
        if self._selected:
            lib.sdrplay_api_LockDeviceApi()
            lib.sdrplay_api_ReleaseDevice(ctypes.byref(self.device))
            lib.sdrplay_api_UnlockDeviceApi()
            self._selected = False
        if self._opened_api:
            lib.sdrplay_api_Close()
            self._opened_api = False

    def status(self) -> dict:
        st = super().status()
        st.update({"serial": self.serial, "api": self.api_version,
                   "device": self.device.SerNo.decode() if self.device is not None else None})
        return st


def devices(lib_path=None) -> list[dict]:
    """The RSPs the API can see, without selecting any (serial, hwVer)."""
    lib = load(lib_path)
    if lib.sdrplay_api_Open() != 0:
        return []
    try:
        if lib.sdrplay_api_LockDeviceApi() != 0:
            return []
        try:
            devs = (DeviceT * 8)()
            n = ctypes.c_uint(0)
            if lib.sdrplay_api_GetDevices(devs, ctypes.byref(n), 8) != 0:
                return []
            return [{"serial": d.SerNo.decode("latin-1"), "hwVer": d.hwVer} for d in devs[:n.value]]
        finally:
            lib.sdrplay_api_UnlockDeviceApi()
    finally:
        lib.sdrplay_api_Close()


def find_library_places() -> list[str]:
    """Where load() looks, for the setup page's "not found" message."""
    return _hostlib_places() + LIB_PLACES.get(sys.platform, DEFAULT_PLACES)


def availability(lib_path=None) -> dict:
    """Whether SDRplay's API is installed: {"available", "why", "host",
    "places"}, `why` "sdrplay_api_missing" when it is not, `host` true on a
    controller (where only its administrator can install it). Loads the
    library only: never opens the API, its service or a radio."""
    host = bool(os.environ.get("SHACK_HOSTLIBS"))
    try:
        load(lib_path)
    except SdrError:
        return {"available": False, "why": "sdrplay_api_missing", "host": host, "places": find_library_places()}
    return {"available": True, "why": None, "host": host, "places": find_library_places()}


__all__ = ["SdrplaySource", "availability", "devices", "find_library_places", "load"]
