"""Broadcast FM: stereo, RDS, and the band scanner's many-carrier RDS
(RADIO-SDR-PLAN §8.5 M3, brought forward for Radio, Brian 23/10/2026).

Radio's dial plays through `Receiver`, called on the engine's receive
thread as ohscore/dsp/wfm.py's WfmDemod is today, whose mono path it
replaces (full deviation still comes out at 0.5, with the same 20 Hz
DC high-pass):

    from shack_sdr import wfm
    rx = wfm.Receiver(rate=240_000, deemphasis_us=50)   # 75 in the Americas
    pcm = rx.process(channel_iq, t0)      # complex64 in; float32 (n, 2) at 48 kHz out
    rx.rds()      # {"pi": "C201", "ps", "rt", "pty": 10, "ptyName": "Pop music", "ta", "tp",
                  #  "ct": "2026-10-23T15:05:00+01:00", "af": [89100000, ...], "stereo", "ts", "rev"}
    rx.status()   # {"stereo", "blend", "pilot", "snrDb", "levelDb", "offsetHz", "rdsSync", "rdsSnrDb",
                  #  "blocks", "corrected", "blockErrors", "groups"}
    rx.reset()    # on every retune: RDS is cleared, the loops start again
    rx.set_deemphasis(75); rx.set_stereo(False); rx.set_blend(False)   # live

`Receiver(audio_rate=12_000, channels=1)` gives today's 12 kHz mono
contract from the same path. A fixed "listen" session runs the same
receiver through the Host instead (`host.start("wfm", {...}, "iq",
240_000)`; sound from `host.audio(sid)`, RDS as `meta` results with `pty`
a number for `pty_name`).

RDS fields appear only once they are stable (every PS and RadioText
segment received twice alike; PI, PTY, TP and TA in two groups; the clock
from a clean group) and are never shown half old, half new; `rev` rises
whenever one changes or the stereo flag does, and `ts` is the UTC (from
`t0`, else seconds into the stream) of that change. `None` is "not heard
yet". `af` lists alternative frequencies in Hz.

The band scanner calls `scan_band` on a wide capture (an RSPdx's 8 or
10 MS/s): every station on the 100 kHz grid from 87.5 to 108 MHz that
stands `min_snr_db` over the noise, with its level and RDS, decoded in
parallel within `budget_s`:

    for st in wfm.scan_band(iq, rate=8_000_000, center_hz=97_000_000, budget_s=3.0):
        st   # {"hz", "levelDb", "stereo", "pi", "ps", "pty", "ptyName", "rt", "ta", "tp", "ct", "af",
             #  "rdsSync", "complete"}

Codes, never sentences: a refused rate or setting is
`DecoderError("bad_input", "rate" | "audio_rate" | "channels" |
"deemphasis")`. The PTY names are the one text here, from the European
table (EN 62106; not RBDS's North American one), through the shack-sdr
catalog.
"""

from __future__ import annotations

import numpy as np

from . import _native
from .i18n import _

#: Radio's bank channel for wide FM; any whole multiple of 48 kHz from 144 kS/s works.
IQ_RATE = 240_000
AUDIO_RATE = 48_000
#: De-emphasis time constants, µs: Europe and most of the world, then the Americas and Korea.
DEEMPHASIS_US = (50, 75)


def _pty_names() -> tuple[str, ...]:
    # A call, not a module constant, so a language chosen after import
    # (i18n.use) is the one used; and each name is a literal in _() so
    # tools/extract_pot.py finds it.
    return (
        _("No programme type"), _("News"), _("Current affairs"), _("Information"), _("Sport"), _("Education"),
        _("Drama"), _("Culture"), _("Science"), _("Varied"), _("Pop music"), _("Rock music"), _("Easy listening"),
        _("Light classical"), _("Serious classical"), _("Other music"), _("Weather"), _("Finance"),
        _("Children's programmes"), _("Social affairs"), _("Religion"), _("Phone-in"), _("Travel"), _("Leisure"),
        _("Jazz music"), _("Country music"), _("National music"), _("Oldies music"), _("Folk music"),
        _("Documentary"), _("Alarm test"), _("Alarm"),
    )


def pty_name(pty: int | float | None) -> str | None:
    """The programme type's name (European RDS table) in the library's
    language; None for None or anything but a whole number 0-31. A Host
    session's meta carries numbers as floats (10.0), so those are taken too."""
    if pty is None or not 0 <= pty < 32 or pty != int(pty):
        return None
    return _pty_names()[int(pty)]


def _named(d: dict) -> dict:
    d["ptyName"] = pty_name(d.get("pty"))
    return d


def _f32(x) -> np.ndarray:
    a = np.ascontiguousarray(x)
    if a.dtype == np.complex64:
        return a.view(np.float32)
    if a.dtype == np.complex128:
        return np.ascontiguousarray(a, np.complex64).view(np.float32)
    return np.ascontiguousarray(a, np.float32)


class Receiver:
    """One station: channel IQ (or its composite) in, 48 kHz stereo out,
    with the stereo state and RDS. Not thread-safe: one caller at a time,
    as the engine's receive thread is. The GIL is released while it works."""

    def __init__(self, rate: float = IQ_RATE, deemphasis_us: float = 50, stereo: bool = True, blend: bool = True,
                 rds: bool = True, audio_rate: int = AUDIO_RATE, channels: int = 2):
        self._rx = _native.WfmReceiver(float(rate), float(deemphasis_us), stereo, blend, rds, True,
                                       int(audio_rate), int(channels))
        self.rate = float(rate)
        self.audio_rate = int(audio_rate)
        self.channels = int(channels)
        self._t_base: float | None = None

    def _clock(self, t0: float | None) -> None:
        if t0 is not None:
            self._t_base = float(t0) - self._rx.samples() / self.rate

    def _out(self, raw: bytes) -> np.ndarray:
        return np.frombuffer(raw, np.float32).reshape(-1, self.channels)

    def process(self, iq, t0: float | None = None) -> np.ndarray:
        """Channel IQ (complex64, centred on the station) in; audio out,
        float32 of shape (n, channels). `t0`, the UTC of the first sample,
        times the RDS changes."""
        self._clock(t0)
        return self._out(self._rx.process_iq(_f32(np.asarray(iq, np.complex64))))

    def process_composite(self, mpx, t0: float | None = None) -> np.ndarray:
        """The discriminator's output (75 kHz deviation = 1.0) in, at the
        receiver's rate; audio out as `process`."""
        self._clock(t0)
        return self._out(self._rx.process_mpx(_f32(mpx)))

    def rds(self) -> dict:
        d = _named(self._rx.rds())
        rev, sample = d.pop("rev"), d.pop("sample")
        d["rev"] = rev
        if rev == 0:
            d["ts"] = None
        else:
            d["ts"] = (self._t_base or 0.0) + sample / self.rate
        return d

    def status(self) -> dict:
        return self._rx.status()

    def reset(self) -> None:
        """A retune: RDS is cleared and the loops start again."""
        self._rx.reset()
        self._t_base = None

    def set_deemphasis(self, us: float) -> None:
        self._rx.set_deemphasis(float(us))

    def set_stereo(self, on: bool) -> None:
        self._rx.set_stereo(bool(on))

    def set_blend(self, on: bool) -> None:
        self._rx.set_blend(bool(on))


def scan_band(iq, rate: float, center_hz: float, budget_s: float = 3.0, threads: int = 0, min_snr_db: float = 10.0,
              rt: bool = False, report: bool = False):
    """Every FM station in a wide capture (complex64 centred on
    `center_hz`) with its level and RDS: [{"hz", "levelDb", "stereo", "pi",
    "ps", "pty", "ptyName", "rt", "ta", "tp", "ct", "af", "rdsSync",
    "complete"}], by frequency. A station is done once it has PI, PS and
    PTY (and RadioText with `rt`); the scan stops when all are, the
    capture runs out, or `budget_s` of wall-clock time is spent, and
    returns what each had. `threads` 0 is one per core. With `report`, a
    dict: {"stations", "usedS", "timedOut", "cpuS", "wallS", "noiseDb"}."""
    r = _native.wfm_scan(_f32(np.asarray(iq, np.complex64)), float(rate), float(center_hz), float(budget_s),
                         int(threads), float(min_snr_db), bool(rt))
    for st in r["stations"]:
        _named(st)
    return r if report else r["stations"]
