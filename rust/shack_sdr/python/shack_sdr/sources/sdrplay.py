"""An SDRplay RSP, through the SDRplay API the user installed (D9).

Moved from shack_packet.sdr.sdrplay (RADIO-SDR-PLAN §6.1); since
23/10/2026 it opens an RSP as SoapySDRPlay3 does ("Settings are
SoapySDRPlay3's" below). Added for Radio (§6.3, Brian 22/10/2026: the API is use if
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

**Settings are SoapySDRPlay3's** (0.5.2, the driver Radio ran ohs-box's
RSPdx through until store 0.1.22-beta.2; the README's "SDRplay: parity
with SoapySDRPlay3" table lists every field with its line in that
driver's source). Zero IF, the bandwidth SoapySDRPlay3 picks for the rate
(1.536 MHz at 2.4 MS/s), no decimation, so the stream arrives at `rate`.
Everything SoapySDRPlay3 sets on its own we set the same way:

- the AGC set point -30 dBfs at open, whatever the gain (Settings.cpp 65);
- the tuner's DC offset tracking: dcCal 4, speedUp 0, trackTime 63
  (Streaming.cpp 359-361). This is the one real difference found: until
  23/10/2026 this module left SDRplay's default (3, 0, 1), which retracks
  the tuner's DC offset for 9 us every 18 ms. On ohs-box a repeater ident
  measured 3.2 dB worse and jittered about +-1 dB where SoapySDRPlay3
  held +-0.2; this is the change expected to close that gap (README);
- a power overload event acknowledged at once with
  Update(Ctrl_OverloadMsgAck), both when detected and when corrected
  (Streaming.cpp 173-185); counted in `status()["overloads"]`;
- the RF frequency truncated to whole hertz, as its uint32_t cast does.

Everything it leaves at SDRplay's default we leave too: LO mode auto, DC
and IQ correction on, ppm 0, isochronous USB, the notches, bias-T and HDR
off. Those last four are options (`bias_t`, `rf_notch`, `dab_notch`,
`hdr`), None meaning "leave the API's default", as SoapySDRPlay3 does
without its `biasT_ctrl`, `rfnotch_ctrl`, `dabnotch_ctrl`, `hdr_ctrl`
arguments; `set_options()` changes them live with the same Update
reasons it uses. A model without the switch ignores it, as it does.

The crystal correction stays 0 in the radio: the channeliser corrects
ppm, as for every source.

**Gain** has two models (`gain_model`):

- "reduction" (the default, Packet's): None is the API's own AGC at 50 Hz
  with the LNA at `lna_state`; a number is a fixed gain *reduction* in dB
  (SDRplay's own sense, gRdB 20-59) with AGC off.
- "soapysdrplay3": SoapySDR's overall gain under SoapySDRPlay3, so a
  station moved off SoapySDR hears and reads exactly what it did. A number
  is split the way SoapySDR's base Device::setGain splits it over
  SoapySDRPlay3's two elements, IFGR (gRdB 20-59) then RFGR (the LNA
  state, 0 to the model's top), each truncated to a whole number
  (`soapy_gain_split`): both are reductions, so a higher number is less
  gain - SoapySDRPlay3's quirk, kept on purpose. None is AGC_CTRL_EN at
  `agc_setpoint` with gRdB and the LNA state left where they were
  (`lna_state` - its `rfgain_sel` - at open, the last manual split after
  one). The gain reported (`gain_now()`, `status()["gain"]`) is its
  getGain: (gRdB - 20) + LNA state, read from the API's structure.

`gain_sense()` says which (`gainIsReduction`, `gainModel`, `rfgrRange`),
the three fields Radio's S-meter reads.

**The stream** arrives as two arrays of int16, I and Q, on the API's own
thread, some 1800 packets a second. The callback copies each straight
into a preallocated block (two memmoves, no Python objects: the less it
holds the API's thread, the less the API's own buffers can overrun) and
hands a complex64 block of about BLOCK_S to a queue for `_read`, scaled
x/32768 exactly as SoapySDRPlay3's CF32 is (Streaming.cpp 151-152). The
packets' sample numbers are checked: a jump the API did not flag as a
reset is counted in `status()["apiGaps"]`/`["apiLost"]` (counted only,
not yet trusted to move the block index: never seen on a real RSP). A
"device removed" event, or no samples for READ_TIMEOUT_S, is SdrError
"disconnected" / "read-failed".

**Antenna/input selection** (added for Radio, RADIO-SDR-PLAN §6; task brief
22/10/2026): an RSP1/RSP1A/RSP1B has one input and `antennas()` falls back
to `IqSource`'s default ("RX"), unswitchable. The others switch live, with
no re-open (`sdrplay_api_Update`, never Uninit/Init):

- **RSPdx/RSPdxR2**: "Antenna A", "Antenna B", "Antenna C" -
  `devParams.rspDxParams.antennaSel`, pushed with reason extension
  `UPDATE_RSPDX_ANTENNA_CONTROL` (the selector is on the device, not the
  channel: both tuners would see the same antenna on a dx, which has one).
- **RSP2**: "Antenna A", "Antenna B", "Hi-Z" - the antenna select and the
  AM port are two fields, but only ever one `Update` call: moving off
  Hi-Z pushes the port alone (which also engages whichever antenna is
  already selected), and moving onto it, or between A and B while already
  off it, pushes the matching one field (SoapySDRPlay3's `setAntenna`,
  "changeToAntennaA_B").
- **RSPduo**: "Tuner 1 50 ohm", "Tuner 1 Hi-Z", "Tuner 2 50 ohm". Tuner 1
  and Tuner 2 are the device's two SMA inputs; this module always asks for
  Single Tuner mode (a duo running Dual Tuner or as an API slave is out of
  scope: Radio opens one channel). Moving between the two tuners calls
  `sdrplay_api_SwapRspDuoActiveTuner`, which is why both of the stream's
  callbacks run the same code (`_on_stream`): after a swap, samples arrive
  on whichever tuner's callback the API now drives, never the one asked
  for at `open()`.

The struct and reason-flag names and the three model's own antenna names
are SoapySDRPlay3's (`Settings.cpp` `listAntennas`/`setAntenna`/
`getAntenna`; MIT, github.com/pothosware/SoapySDRPlay3, read 22/10/2026),
kept so a setting saved through either path means the same input. Unlike
SoapySDRPlay3, nothing here is asked to drive a second (diversity) channel.

NOT YET RUN ON AN RSP: shack's RSPdx belongs to the apps that use it. The
RSP2 and RSPduo paths are checked only against the stand-in API below,
never against real hardware of either kind.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import sys

import numpy as np

from ..i18n import _
from .source import IqSource, SdrError

READ_TIMEOUT_S = 3.0
BLOCK_S = 0.05
QUEUE_BLOCKS = 64
BW_1_536 = 1536
IF_ZERO = 0
LO_AUTO = 1
AGC_DISABLE, AGC_50HZ = 0, 2
TUNER_A, TUNER_B = 1, 2
AGC_CTRL_EN = 4
UPDATE_TUNER_GR, UPDATE_TUNER_FRF, UPDATE_DEV_FS, UPDATE_CTRL_AGC = 0x8000, 0x20000, 0x1, 0x1000000
UPDATE_CTRL_OVERLOAD_ACK = 0x04000000
EVENT_POWER_OVERLOAD, EVENT_DEVICE_REMOVED = 1, 2
OVERLOAD_DETECTED, OVERLOAD_CORRECTED = 0, 1
GRDB_RANGE = (20, 59)

#: SoapySDRPlay3's AGC set point, set at open whatever the gain
#: (Settings.cpp 65), and its tuner DC offset tracking, set before Init
#: (Streaming.cpp 359-361; SDRplay's defaults are 3, 0, 1).
AGC_SETPOINT_DBFS = -30
DC_CAL, DC_SPEED_UP, DC_TRACK_TIME = 4, 0, 63

#: The two gain models (module docstring).
GAIN_MODELS = ("reduction", "soapysdrplay3")

# Hardware ids (sdrplay_api.h SDRPLAY_RSP*_ID; DeviceT.hwVer).
HW_RSP1, HW_RSP1A, HW_RSP2, HW_RSPDUO, HW_RSPDX, HW_RSP1B, HW_RSPDXR2 = 1, 255, 2, 3, 4, 6, 7
#: Single Tuner, the only RSPduo mode this module asks for (module docstring).
RSPDUO_MODE_SINGLE_TUNER = 1

# Antenna/AM-port update reasons (sdrplay_api.h ReasonForUpdateT/Ext1T).
UPDATE_NONE = 0
UPDATE_RSP2_AMPORT_SELECT = 0x100
UPDATE_RSP2_ANTENNA_CONTROL = 0x200
UPDATE_RSPDUO_AMPORT_SELECT = 0x10000000
EXT1_NONE = 0
EXT1_RSPDX_ANTENNA_CONTROL = 0x4

# Antenna/AM-port selector values (sdrplay_api_rsp2.h, _rspDuo.h, _rspDx.h).
RSP2_ANTENNA_A, RSP2_ANTENNA_B = 5, 6
RSP2_AMPORT_2, RSP2_AMPORT_1 = 0, 1           # AMPORT_2 is the 50 ohm pair, AMPORT_1 is Hi-Z
RSPDUO_AMPORT_2, RSPDUO_AMPORT_1 = 0, 1        # same two values, Tuner 1 only
RSPDX_ANTENNA_A, RSPDX_ANTENNA_B, RSPDX_ANTENNA_C = 0, 1, 2

# Option update reasons (sdrplay_api.h), as SoapySDRPlay3's writeSetting
# pushes them (Settings.cpp 1661-1826).
UPDATE_RSP1A_BIAST, UPDATE_RSP1A_RF_NOTCH, UPDATE_RSP1A_DAB_NOTCH = 0x10, 0x20, 0x40
UPDATE_RSP2_BIAST, UPDATE_RSP2_RF_NOTCH = 0x80, 0x400
UPDATE_RSPDUO_BIAST, UPDATE_RSPDUO_AM_NOTCH = 0x08000000, 0x20000000
UPDATE_RSPDUO_RF_NOTCH, UPDATE_RSPDUO_DAB_NOTCH = 0x40000000, 0x80000000
EXT1_RSPDX_HDR, EXT1_RSPDX_BIAST, EXT1_RSPDX_RF_NOTCH, EXT1_RSPDX_DAB_NOTCH = 0x1, 0x2, 0x8, 0x10

#: The switches `bias_t`, `rf_notch`, `dab_notch` and `hdr` name.
OPTIONS = ("bias_t", "rf_notch", "dab_notch", "hdr")

# SoapySDRPlay3's gain elements (Settings.cpp 498-640): IFGR is gRdB 20-59;
# RFGR is the LNA state, 0 to this top per model, and 20-59 for a model it
# does not name (its own fallthrough, kept).
SOAPY_IFGR = (20, 59)
SOAPY_RFGR_TOP = {HW_RSP1: 3, HW_RSP2: 8, HW_RSPDUO: 9, HW_RSP1A: 9, HW_RSP1B: 9, HW_RSPDX: 27, HW_RSPDXR2: 27}


def soapy_rfgr_range(hw_ver: int) -> tuple[int, int]:
    """SoapySDRPlay3's getGainRange("RFGR") for hardware id `hw_ver`."""
    return (0, SOAPY_RFGR_TOP[hw_ver]) if hw_ver in SOAPY_RFGR_TOP else SOAPY_IFGR


def soapy_gain_split(gain: float, hw_ver: int) -> tuple[int, int]:
    """(gRdB, LNA state) for SoapySDR's overall setGain(RX, gain) on an RSP
    of hardware id `hw_ver` under SoapySDRPlay3.

    SoapySDRPlay3 overrides only the per-element setGain, so the overall
    call is SoapySDR's base Device::setGain (lib/Device.cpp): each element
    in listGains order (IFGR, RFGR) takes g = min(left, max - min), is set
    to g + min, and `left` drops by what it then holds less its min. Its
    per-element setGain stores `(int)value`, truncated toward zero as
    int() does, so a fraction carries on to RFGR and is truncated there."""
    left, got = float(gain), []
    for lo, hi in (SOAPY_IFGR, soapy_rfgr_range(hw_ver)):
        v = int(min(left, hi - lo) + lo)
        got.append(v)
        left -= v - lo
    return got[0], got[1]


def soapy_gain_of(grdb: int, lna: int, hw_ver: int) -> float:
    """SoapySDR's overall getGain under SoapySDRPlay3: each element's value
    less its range's minimum, summed (lib/Device.cpp getGain)."""
    return float((grdb - SOAPY_IFGR[0]) + (lna - soapy_rfgr_range(hw_ver)[0]))


def bw_for_rate(rate: float) -> int:
    """SoapySDRPlay3's getBwEnumForRate (Settings.cpp 1091-1101): the
    widest IF filter narrower than the rate, in kHz (sdrplay_api_Bw_MHzT)."""
    for limit, bw in ((300_000, 200), (600_000, 300), (1_536_000, 600), (5_000_000, 1536),
                      (6_000_000, 5000), (7_000_000, 6000), (8_000_000, 7000)):
        if rate < limit:
            return bw
    return 8000


def _signed(reason: int) -> int:
    """An update reason as the C int the API takes (RspDuo_RfDabNotchControl
    is 0x80000000, the sign bit)."""
    return reason - (1 << 32) if reason >= 1 << 31 else reason


#: Each switchable model's input names, in SoapySDRPlay3's own words and
#: order (module docstring). A hwVer not here (RSP1/1A/1B) has one input:
#: IqSource.ANTENNAS's default ("RX") applies unchanged.
ANTENNAS = {
    HW_RSP2: ("Antenna A", "Antenna B", "Hi-Z"),
    HW_RSPDX: ("Antenna A", "Antenna B", "Antenna C"),
    HW_RSPDXR2: ("Antenna A", "Antenna B", "Antenna C"),
    HW_RSPDUO: ("Tuner 1 50 ohm", "Tuner 1 Hi-Z", "Tuner 2 50 ohm"),
}

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


class SyncUpdateT(ctypes.Structure):
    _fields_ = [("sampleNum", ctypes.c_uint), ("period", ctypes.c_uint)]


class ResetFlagsT(ctypes.Structure):
    _fields_ = [("resetGainUpdate", ctypes.c_ubyte), ("resetRfUpdate", ctypes.c_ubyte),
                ("resetFsUpdate", ctypes.c_ubyte)]


class Rsp1aParamsT(ctypes.Structure):
    _fields_ = [("rfNotchEnable", ctypes.c_ubyte), ("rfDabNotchEnable", ctypes.c_ubyte)]


class Rsp2ParamsT(ctypes.Structure):
    _fields_ = [("extRefOutputEn", ctypes.c_ubyte)]


class RspDuoParamsT(ctypes.Structure):
    _fields_ = [("extRefOutputEn", ctypes.c_int)]


class RspDxParamsT(ctypes.Structure):
    #: hdrEnable/biasTEnable/rfNotchEnable/rfDabNotchEnable are written
    #: only when asked (the `hdr`, `bias_t`, `rf_notch`, `dab_notch`
    #: options), else left as SDRplay's own defaults, as SoapySDRPlay3
    #: leaves them.
    _fields_ = [("hdrEnable", ctypes.c_ubyte), ("biasTEnable", ctypes.c_ubyte), ("antennaSel", ctypes.c_int),
                ("rfNotchEnable", ctypes.c_ubyte), ("rfDabNotchEnable", ctypes.c_ubyte)]


class DevParamsPrefix(ctypes.Structure):
    #: Every field up to and including the per-model structs, in the
    #: header's order (sdrplay_api_dev.h `sdrplay_api_DevParamsT`), so
    #: `rspDxParams` lands where the real API expects it: ctypes computes
    #: each field's offset from the ones before it, exactly as the C
    #: compiler that built the real library did.
    _fields_ = [("ppm", ctypes.c_double), ("fsFreq", FsFreqT), ("syncUpdate", SyncUpdateT),
                ("resetFlags", ResetFlagsT), ("mode", ctypes.c_int), ("samplesPerPkt", ctypes.c_uint),
                ("rsp1aParams", Rsp1aParamsT), ("rsp2Params", Rsp2ParamsT), ("rspDuoParams", RspDuoParamsT),
                ("rspDxParams", RspDxParamsT)]


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


class Rsp1aTunerParamsT(ctypes.Structure):
    _fields_ = [("biasTEnable", ctypes.c_ubyte)]


class Rsp2TunerParamsT(ctypes.Structure):
    _fields_ = [("biasTEnable", ctypes.c_ubyte), ("amPortSel", ctypes.c_int), ("antennaSel", ctypes.c_int),
                ("rfNotchEnable", ctypes.c_ubyte)]


class RspDuoResetSlaveFlagsT(ctypes.Structure):
    _fields_ = [("resetGainUpdate", ctypes.c_ubyte), ("resetRfUpdate", ctypes.c_ubyte)]


class RspDuoTunerParamsT(ctypes.Structure):
    _fields_ = [("biasTEnable", ctypes.c_ubyte), ("tuner1AmPortSel", ctypes.c_int),
                ("tuner1AmNotchEnable", ctypes.c_ubyte), ("rfNotchEnable", ctypes.c_ubyte),
                ("rfDabNotchEnable", ctypes.c_ubyte), ("resetSlaveFlags", RspDuoResetSlaveFlagsT)]


class RspDxTunerParamsT(ctypes.Structure):
    _fields_ = [("hdrBw", ctypes.c_int)]


class RxChannelPrefix(ctypes.Structure):
    #: Same reasoning as DevParamsPrefix: every field up to rspDxTunerParams
    #: in the header's order (sdrplay_api_rx_channel.h), so rsp2TunerParams
    #: and rspDuoTunerParams land at the real offsets.
    _fields_ = [("tunerParams", TunerParamsT), ("ctrlParams", ControlParamsT),
                ("rsp1aTunerParams", Rsp1aTunerParamsT), ("rsp2TunerParams", Rsp2TunerParamsT),
                ("rspDuoTunerParams", RspDuoTunerParamsT), ("rspDxTunerParams", RspDxTunerParamsT)]


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
    "sdrplay_api_SwapRspDuoActiveTuner": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_int], _E),
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
                 serial: str | None = None, lna_state: int = 3, lib_path=None, antenna: str | None = None,
                 gain_model: str = "reduction", agc_setpoint: int = AGC_SETPOINT_DBFS,
                 bias_t: bool | None = None, rf_notch: bool | None = None, dab_notch: bool | None = None,
                 hdr: bool | None = None):
        super().__init__(centre, rate, gain)
        if gain_model not in GAIN_MODELS:
            raise ValueError(_("gain_model {gain_model!r} is not one of {gain_models}").format(
                gain_model=gain_model, gain_models=GAIN_MODELS))
        self.serial = serial
        self.lna_state = int(lna_state)
        self.gain_model = gain_model
        self.agc_setpoint = int(agc_setpoint)
        # None: leave SDRplay's default (off), as SoapySDRPlay3 does when
        # not asked (module docstring).
        self.options = {"bias_t": bias_t, "rf_notch": rf_notch, "dab_notch": dab_notch, "hdr": hdr}
        self.overloads = 0
        self.overloaded = False
        self.api_gaps = self.api_lost = 0
        self._next_sample = None
        self.lib_path = lib_path
        # The model (and so the input names) is only known once a device is
        # found, so unlike IqSource.ANTENNAS this stays whatever was asked
        # for (or None, for the model's default) until _open() resolves it.
        self.antenna = antenna
        self.lib = None
        self.device: DeviceT | None = None
        self.params = None
        self.api_version = 0.0
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._block_n = max(1, int(float(rate) * BLOCK_S))
        self._bi = self._bq = None
        self._gathered = 0
        self._removed = False
        # Both callbacks run the same code: in Single Tuner mode exactly one
        # of them fires at a time, and which one can change under a Tuner
        # 1/2 switch on an RSPduo (module docstring).
        self._cbs = CallbackFnsT(STREAM_CB(self._on_stream), STREAM_CB(self._on_stream), EVENT_CB(self._on_event))
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
                names = ANTENNAS.get(self.device.hwVer, IqSource.ANTENNAS)
                if self.antenna is None:
                    self.antenna = names[0]
                elif self.antenna not in names:
                    raise SdrError("bad-antenna",
                                    f"{self.antenna!r} is not one of this receiver's inputs: {list(names)}")
                if self.device.hwVer == HW_RSPDUO:
                    # Single Tuner mode only (module docstring); GetDevices
                    # reports rspDuoMode as a bitmask of what the device
                    # offers, not a choice, so it must be set before
                    # SelectDevice, same as the tuner the antenna wants.
                    self.device.rspDuoMode = RSPDUO_MODE_SINGLE_TUNER
                    self.device.tuner = TUNER_B if self.antenna == "Tuner 2 50 ohm" else TUNER_A
                elif self.device.tuner == 0:
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
            ch = self._ch()
            # SoapySDRPlay3's setSampleRate above 2 MS/s: zero IF, no
            # decimation (decimationFactor already 1, so it touches neither
            # that nor wideBandSignal), its bandwidth for the rate.
            ch.tunerParams.bwType = bw_for_rate(self.rate)
            ch.tunerParams.ifType = IF_ZERO
            ch.tunerParams.loMode = LO_AUTO
            ch.tunerParams.rfFreq.rfHz = float(int(self.centre))
            ch.ctrlParams.decEnable = 0
            ch.ctrlParams.setPoint_dBfs = self.agc_setpoint
            ch.tunerParams.dcOffsetTuner.dcCal = DC_CAL
            ch.tunerParams.dcOffsetTuner.speedUp = DC_SPEED_UP
            ch.tunerParams.dcOffsetTuner.trackTime = DC_TRACK_TIME
            self._set_gain_fields(ch)
            self._apply_antenna(self.antenna, live=False)
            # After the antenna: an RSPduo's notch is a different field on
            # its Hi-Z port.
            for name, on in self.options.items():
                if on is not None:
                    self._apply_option(name, bool(on), live=False)
            cap = self._block_n + 65536            # a block plus the largest packet the API sends
            self._bi, self._bq = np.empty(cap, np.int16), np.empty(cap, np.int16)
            self._gathered = 0
            self._next_sample = None
            self._check(lib.sdrplay_api_Init(self.device.dev, ctypes.byref(self._cbs), None), "Init", "open-failed")
            self._running = True
        except Exception:
            self._close()
            raise

    def _ch(self):
        """The channel params for the tuner the API currently has active -
        always A, except an RSPduo after a Tuner 1/2 swap moved it to B."""
        return (self.params.rxChannelB if self.device.tuner == TUNER_B else self.params.rxChannelA).contents

    def _hw(self) -> int:
        """The open model's hardware id; an RSPdx before one is open (the
        only model a gain is reported for before open is ohs-box's)."""
        return int(self.device.hwVer) if self.device is not None else HW_RSPDX

    def _set_gain_fields(self, ch) -> None:
        if self.gain_model == "soapysdrplay3":
            if self.gain is None:
                # gRdB and the LNA state stay where they were, as
                # SoapySDRPlay3's structure kept them.
                ch.ctrlParams.agcEnable = AGC_CTRL_EN
            else:
                ch.ctrlParams.agcEnable = AGC_DISABLE
                grdb, self.lna_state = soapy_gain_split(self.gain, self._hw())
                ch.tunerParams.gain.gRdB = grdb
            ch.ctrlParams.setPoint_dBfs = self.agc_setpoint
            ch.tunerParams.gain.LNAstate = self.lna_state
            return
        ch.tunerParams.gain.LNAstate = self.lna_state
        if self.gain is None:
            ch.ctrlParams.agcEnable = AGC_50HZ
            ch.ctrlParams.setPoint_dBfs = self.agc_setpoint
        else:
            ch.ctrlParams.agcEnable = AGC_DISABLE
            ch.tunerParams.gain.gRdB = int(min(max(round(self.gain), GRDB_RANGE[0]), GRDB_RANGE[1]))

    def _live(self) -> bool:
        # The API's structure is only ours to read while the stream runs:
        # after Uninit/Close the pointer is the service's again.
        return self._running and self.params is not None

    def gain_now(self) -> float | None:
        """The gain as this model reports it: SoapySDRPlay3's getGain,
        (gRdB - 20) + LNA state, or the gain reduction gRdB. Read from the
        API's structure while running (under AGC, whatever is there);
        before that from the gain asked for, None under AGC."""
        if self._live():
            g = self._ch().tunerParams.gain
            grdb, lna = int(g.gRdB), int(g.LNAstate)
        elif self.gain is None:
            return None
        elif self.gain_model == "soapysdrplay3":
            grdb, lna = soapy_gain_split(self.gain, self._hw())
        else:
            return float(min(max(round(self.gain), GRDB_RANGE[0]), GRDB_RANGE[1]))
        if self.gain_model == "soapysdrplay3":
            return soapy_gain_of(grdb, lna, self._hw())
        return float(grdb)

    def gain_sense(self) -> dict:
        """What a gain number means here, in the fields Radio's S-meter
        reads: both models are reductions; `rfgrRange` is the LNA state's
        range under SoapySDRPlay3 for the open model (an RSPdx's before)."""
        return {"gainIsReduction": True, "gainModel": self.gain_model,
                "rfgrRange": list(soapy_rfgr_range(self._hw()))}

    def set_gain(self, gain: float | None) -> None:
        self.gain = gain
        if self._running:
            self._set_gain_fields(self._ch())
            self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner,
                        UPDATE_TUNER_GR | UPDATE_CTRL_AGC, 0), "Update")

    def set_centre(self, hz: float) -> None:
        self.centre = float(hz)
        if self._running:
            self._ch().tunerParams.rfFreq.rfHz = float(int(hz))
            self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner, UPDATE_TUNER_FRF, 0),
                        "Update", "out-of-range")

    def antennas(self) -> list[str]:
        """This receiver's input names for the model `open()` found, or []
        before that (the model decides the list, not the class; module
        docstring and IqSource.antennas)."""
        if self.device is None:
            return []
        return list(ANTENNAS.get(self.device.hwVer, IqSource.ANTENNAS))

    def set_antenna(self, name: str) -> None:
        """Switch live, with no re-open: always exactly one
        `sdrplay_api_Update` (RSPdx; RSP2 either field), or one
        `sdrplay_api_SwapRspDuoActiveTuner` (RSPduo changing tuner) or
        `Update` (RSPduo staying put). A single-input model (RSP1/1A/1B)
        just confirms "RX". Refuses a name this model does not have:
        SdrError("bad-antenna")."""
        names = self.antennas()
        if not names:
            raise SdrError("bad-antenna", f"{name!r}: no device is open to say what its inputs are")
        if name not in names:
            raise SdrError("bad-antenna", f"{name!r} is not one of this receiver's inputs: {names}")
        if self._running:
            self._apply_antenna(name, live=True)
        self.antenna = name

    def _apply_antenna(self, name: str, *, live: bool) -> None:
        """Write the struct fields for `name` (and, if `live`, push them)."""
        hw = self.device.hwVer
        if hw == HW_RSP2:
            ch = self._ch()
            hi_z = name == "Hi-Z"
            was_hi_z = ch.rsp2TunerParams.amPortSel == RSP2_AMPORT_1
            if hi_z:
                ch.rsp2TunerParams.amPortSel = RSP2_AMPORT_1
                if live:
                    self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner,
                                 UPDATE_RSP2_AMPORT_SELECT, EXT1_NONE), "Update")
            else:
                ch.rsp2TunerParams.antennaSel = RSP2_ANTENNA_A if name == "Antenna A" else RSP2_ANTENNA_B
                if was_hi_z:
                    # Leaving Hi-Z: switching the port off it is the only
                    # update pushed - it also engages whichever antenna is
                    # already selected, so a second AntennaControl update
                    # is not sent (SoapySDRPlay3's setAntenna
                    # "changeToAntennaA_B": the two branches are either/or).
                    ch.rsp2TunerParams.amPortSel = RSP2_AMPORT_2
                    if live:
                        self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner,
                                     UPDATE_RSP2_AMPORT_SELECT, EXT1_NONE), "Update")
                elif live:
                    self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner,
                                 UPDATE_RSP2_ANTENNA_CONTROL, EXT1_NONE), "Update")
        elif hw in (HW_RSPDX, HW_RSPDXR2):
            # Device-level, not per-channel: a dx has one antenna port for
            # both tuners (module docstring).
            sel = {"Antenna A": RSPDX_ANTENNA_A, "Antenna B": RSPDX_ANTENNA_B, "Antenna C": RSPDX_ANTENNA_C}[name]
            self.params.devParams.contents.rspDxParams.antennaSel = sel
            if live:
                self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner, UPDATE_NONE,
                             EXT1_RSPDX_ANTENNA_CONTROL), "Update")
        elif hw == HW_RSPDUO:
            want_tuner = TUNER_B if name == "Tuner 2 50 ohm" else TUNER_A
            port = RSPDUO_AMPORT_1 if name == "Tuner 1 Hi-Z" else RSPDUO_AMPORT_2
            self._ch().rspDuoTunerParams.tuner1AmPortSel = port
            if live and want_tuner != self.device.tuner:
                tuner = ctypes.c_int(self.device.tuner)
                self._check(self.lib.sdrplay_api_SwapRspDuoActiveTuner(self.device.dev, ctypes.byref(tuner), port),
                             "SwapRspDuoActiveTuner")
                self.device.tuner = tuner.value
            elif live:
                self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner,
                             UPDATE_RSPDUO_AMPORT_SELECT, EXT1_NONE), "Update")
        # RSP1/1A/1B: one input, "RX"; nothing in the struct to change.

    def set_options(self, **kw) -> None:
        """Change `bias_t`, `rf_notch`, `dab_notch` or `hdr` (True/False),
        live if streaming: one Update each, SoapySDRPlay3's reasons. None
        is refused here: once written, SDRplay's default is just False."""
        for name, on in kw.items():
            if name not in OPTIONS or on is None:
                raise ValueError(_("{name}={on!r}: set_options takes {options} as True or False").format(
                    name=name, on=on, options=OPTIONS))
        for name, on in kw.items():
            self.options[name] = bool(on)
            if self._running:
                self._apply_option(name, bool(on), live=True)

    def _option_targets(self, name: str) -> list:
        """(struct, field, reason, ext1) for option `name` on the open
        model, as SoapySDRPlay3's writeSetting maps it (Settings.cpp
        1660-1825); [] where the model has no such switch, which it
        ignores and so do we."""
        hw, ch, dev = self.device.hwVer, self._ch(), self.params.devParams.contents
        dx = hw in (HW_RSPDX, HW_RSPDXR2)
        one_a = hw in (HW_RSP1A, HW_RSP1B)
        if name == "bias_t":
            if hw == HW_RSP2:
                return [(ch.rsp2TunerParams, "biasTEnable", UPDATE_RSP2_BIAST, EXT1_NONE)]
            if hw == HW_RSPDUO:
                return [(ch.rspDuoTunerParams, "biasTEnable", UPDATE_RSPDUO_BIAST, EXT1_NONE)]
            if one_a:
                return [(ch.rsp1aTunerParams, "biasTEnable", UPDATE_RSP1A_BIAST, EXT1_NONE)]
            if dx:
                return [(dev.rspDxParams, "biasTEnable", UPDATE_NONE, EXT1_RSPDX_BIAST)]
        elif name == "rf_notch":
            if hw == HW_RSP2:
                return [(ch.rsp2TunerParams, "rfNotchEnable", UPDATE_RSP2_RF_NOTCH, EXT1_NONE)]
            if hw == HW_RSPDUO:
                # Tuner 1's Hi-Z port has its own AM notch; the 50 ohm
                # ports share the RF notch (SoapySDRPlay3's two ifs).
                duo = ch.rspDuoTunerParams
                if self.device.tuner == TUNER_A and duo.tuner1AmPortSel == RSPDUO_AMPORT_1:
                    return [(duo, "tuner1AmNotchEnable", UPDATE_RSPDUO_AM_NOTCH, EXT1_NONE)]
                if duo.tuner1AmPortSel == RSPDUO_AMPORT_2:
                    return [(duo, "rfNotchEnable", UPDATE_RSPDUO_RF_NOTCH, EXT1_NONE)]
            if one_a:
                return [(dev.rsp1aParams, "rfNotchEnable", UPDATE_RSP1A_RF_NOTCH, EXT1_NONE)]
            if dx:
                return [(dev.rspDxParams, "rfNotchEnable", UPDATE_NONE, EXT1_RSPDX_RF_NOTCH)]
        elif name == "dab_notch":
            if hw == HW_RSPDUO:
                return [(ch.rspDuoTunerParams, "rfDabNotchEnable", UPDATE_RSPDUO_DAB_NOTCH, EXT1_NONE)]
            if one_a:
                return [(dev.rsp1aParams, "rfDabNotchEnable", UPDATE_RSP1A_DAB_NOTCH, EXT1_NONE)]
            if dx:
                return [(dev.rspDxParams, "rfDabNotchEnable", UPDATE_NONE, EXT1_RSPDX_DAB_NOTCH)]
        elif name == "hdr" and dx:
            return [(dev.rspDxParams, "hdrEnable", UPDATE_NONE, EXT1_RSPDX_HDR)]
        return []

    def _apply_option(self, name: str, on: bool, *, live: bool) -> None:
        for struct, fld, reason, ext in self._option_targets(name):
            setattr(struct, fld, int(on))
            if live:
                self._check(self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner, _signed(reason), ext),
                            "Update")

    def option_values(self) -> dict:
        """Each switch this model has, as the API's structure holds it now
        (False where never written: SDRplay's default); {} before open."""
        if self.device is None or self.params is None:
            return {}
        out = {}
        for name in OPTIONS:
            targets = self._option_targets(name)
            if targets:
                struct, fld, _, _ = targets[0]
                out[name] = bool(getattr(struct, fld))
        return out

    def _on_stream(self, xi, xq, params, n, reset, ctx) -> None:
        # The API calls back with a USB packet's worth (about 1300 samples
        # at 2.4 MS/s, some 1800 times a second) and waits for us; handed
        # on one by one, the per-block cost downstream outruns the stream.
        # So packets are copied raw into one preallocated block (two
        # memmoves: no numpy object made per packet, the API's thread held
        # as briefly as Python allows) and scaled once per BLOCK_S block,
        # as librtlsdr's read is gathered (256 KiB).
        try:
            if params:
                # StreamCbParamsT.firstSampleNum: a jump the API did not
                # flag as a reset means it dropped samples before us.
                first = ctypes.c_uint.from_address(params).value
                if self._next_sample is not None and not reset and first != self._next_sample:
                    self.api_gaps += 1
                    self.api_lost += (first - self._next_sample) & 0xFFFFFFFF
                self._next_sample = (first + n) & 0xFFFFFFFF
            k = self._gathered
            if k + n > len(self._bi):
                self._flush()
                k = 0
                if n > len(self._bi):
                    self._bi, self._bq = np.empty(n, np.int16), np.empty(n, np.int16)
            ctypes.memmove(self._bi.ctypes.data + 2 * k, xi, 2 * n)
            ctypes.memmove(self._bq.ctypes.data + 2 * k, xq, 2 * n)
            self._gathered = k + n
            if self._gathered >= self._block_n:
                self._flush()
        except Exception:                           # noqa: BLE001 - never raise into the API's thread
            pass

    def _flush(self) -> None:
        m, self._gathered = self._gathered, 0
        if not m:
            return
        # x/32768, SoapySDRPlay3's CF32 scale (1/32768 is exact in float32,
        # so the product is the quotient).
        block = np.empty(m, np.complex64)
        block.real = self._bi[:m] * np.float32(1 / 32768)
        block.imag = self._bq[:m] * np.float32(1 / 32768)
        try:
            self._q.put_nowait(block)
        except queue.Full:
            self.lose(m)

    def _on_event(self, event, tuner, params, ctx) -> None:
        if event == EVENT_POWER_OVERLOAD:
            # Acknowledged at once, detected or corrected, as SoapySDRPlay3
            # does (Streaming.cpp 173-185): the API expects the ack before
            # it reports the next change.
            try:
                kind = ctypes.c_int.from_address(params).value if params else OVERLOAD_DETECTED
                if kind == OVERLOAD_DETECTED:
                    self.overloads += 1
                self.overloaded = kind == OVERLOAD_DETECTED
                if self.lib is not None and self.device is not None:
                    self.lib.sdrplay_api_Update(self.device.dev, self.device.tuner, UPDATE_CTRL_OVERLOAD_ACK,
                                                EXT1_NONE)
            except Exception:                       # noqa: BLE001 - never raise into the API's thread
                pass
        elif event == EVENT_DEVICE_REMOVED:
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
                   "device": self.device.SerNo.decode() if self.device is not None else None,
                   "overloads": self.overloads, "overloaded": self.overloaded,
                   "apiGaps": self.api_gaps, "apiLost": self.api_lost, "options": self.option_values()})
        st.update(self.gain_sense())
        if self.gain_model == "soapysdrplay3":
            st["gain"] = self.gain_now()
        if self._live():
            g = self._ch().tunerParams.gain
            st.update(gRdB=int(g.gRdB), lnaState=int(g.LNAstate))
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


__all__ = ["GAIN_MODELS", "SdrplaySource", "availability", "bw_for_rate", "devices", "find_library_places", "load",
           "soapy_gain_of", "soapy_gain_split", "soapy_rfgr_range"]
