"""The fast modes as an app's settings: one place that says what the
settings are, checks them, and turns them into the objects the radio
link, the channel and the data link take. For the desktop app's radio and
channel settings (OpenHamShack Packet Radio 0.2.1), so the app wires one
dataclass instead of a dozen keyword arguments.

| setting | values | default | what |
|---|---|---|---|
| `mode` | modem.MODES name: afsk1200, g3ruh9600/19200/38400, gfsk76800/153600, 4fsk38400/76800 | g3ruh9600 | the one mode, when `adapt` is off |
| `adapt` | bool | False | link adaptation (adapt.py): both stations must have it on |
| `top_mode`, `floor_mode` | fast mode names | 4fsk76800, g3ruh9600 | the ladder's ends; the floor carries every S and U frame |
| `window` | 1-63 | 32 | k; over 7 is modulo 128 (SABME) |
| `paclen` | 1-2048 | 512 | I field bytes |
| `srej` | bool | True | selective reject, when the other station offers it too |
| `txdelay_ms` | 0-1000 | 0 | TXDELAY asked for; never below the link's turnaround plus the mode's lock bits |
| `turnaround_ms` | 0-500, or None | None | the radio's key-up time; None is the link's own (a Pluto 100 ms until measured) |
| `ptt_output` | none, gpo0-gpo3, firmware | none | radios/pluto.py "PTT output" |
| `relay_lead_ms`, `relay_tail_ms` | 0-500 | 20, 10 | the relay's sequencing |
| `max_tx_gain_db` | -89.75-0, or None | None | an amplifier's drive cap, per band: the app stores one per band and gives the one for the frequency |
| `adapt_start` | a ladder mode, or None | None | a path test's best mode for receiving (pathtest.py) |

`problems()` gives every reason key (PROBLEMS) the settings are refused
for, without opening anything. `wide_data_refusal()` is the UK band-plan
check for modes wider than 25 kHz: the RSGB 2026 plan's high-speed data
segments, held here as WIDE_DATA_SEGMENTS because the shared band-plan
dataset (data/bandplan/plans/rsgb-2026.json) does not yet carry the 23 cm
DD segments (1241.000-1241.750, 1299.000-1299.750); when it does, the app
should take them from there. A Pluto's LO leakage and IQ image sit ifo
and 2 x ifo below the channel and should be in the segment too
(`wide_data_notes`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from . import adapt as _adapt
from . import modem

WINDOW_MAX = 63
PACLEN_MAX = 2048
PTT_OUTPUTS = ("none", "gpo0", "gpo1", "gpo2", "gpo3", "firmware")
WIDE_HZ = 25_000.0
#: RSGB band plan 2026 (effective 1 January 2024): 23 cm p.23, 13 cm p.25.
#: (low Hz, high Hz, widest allowed Hz, what it says)
WIDE_DATA_SEGMENTS = (
    (1_241_000_000, 1_241_750_000, 150_000, "23 cm DD high speed digital data, 5 x 150 kHz channels"),
    (1_299_000_000, 1_299_750_000, 150_000, "23 cm DD high speed digital data, 5 x 150 kHz channels"),
    (2_311_000_000, 2_315_000_000, 200_000, "13 cm high speed data"),
)
#: Reason keys the app translates.
PROBLEMS = {
    "fast-unknown-mode": "no such mode",
    "fast-ladder": "the adaptive top and floor do not make a ladder (the floor must be the more robust)",
    "fast-window": "the window is 1-63",
    "fast-paclen": "the packet length is 1-2048 bytes",
    "fast-txdelay": "TXDELAY is 0-1000 ms",
    "fast-turnaround": "the turnaround is 0-500 ms",
    "fast-ptt-output": "no such PTT output",
    "fast-relay": "the relay lead and tail are 0-500 ms",
    "fast-drive-cap": "the amplifier's drive cap is -89.75 to 0 dB",
    "fast-adapt-start": "the adaptive start mode is not on the ladder",
    "fast-out-of-plan": "this mode is wider than the band plan allows at this frequency",
}


@dataclass
class FastSettings:
    mode: str = "g3ruh9600"
    adapt: bool = False
    top_mode: str = _adapt.DEFAULT_TOP
    floor_mode: str = _adapt.DEFAULT_FLOOR
    window: int = 32
    paclen: int = 512
    srej: bool = True
    txdelay_ms: float = 0.0
    turnaround_ms: float | None = None
    ptt_output: str = "none"
    relay_lead_ms: float = 20.0
    relay_tail_ms: float = 10.0
    max_tx_gain_db: float | None = None
    adapt_start: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> FastSettings:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)

    def ladder(self) -> tuple[str, ...] | None:
        return _adapt.ladder(self.top_mode, self.floor_mode) if self.adapt else None

    def widest(self) -> modem.Mode:
        names = self.ladder() or (self.mode,)
        return max((modem.mode(n) for n in names), key=lambda m: m.occupied_hz)

    def problems(self, frequency_hz: float | None = None) -> list[str]:
        out = []
        try:
            modem.mode(self.mode)
            if self.adapt:
                lad = self.ladder()
                if self.adapt_start is not None and self.adapt_start not in lad:
                    out.append("fast-adapt-start")
        except Exception:                           # noqa: BLE001 - said as a key
            out.append("fast-ladder" if self.adapt else "fast-unknown-mode")
        if not 1 <= int(self.window) <= WINDOW_MAX:
            out.append("fast-window")
        if not 1 <= int(self.paclen) <= PACLEN_MAX:
            out.append("fast-paclen")
        if not 0 <= float(self.txdelay_ms) <= 1000:
            out.append("fast-txdelay")
        if self.turnaround_ms is not None and not 0 <= float(self.turnaround_ms) <= 500:
            out.append("fast-turnaround")
        if self.ptt_output not in PTT_OUTPUTS:
            out.append("fast-ptt-output")
        if not (0 <= self.relay_lead_ms <= 500 and 0 <= self.relay_tail_ms <= 500):
            out.append("fast-relay")
        if self.max_tx_gain_db is not None and not -89.75 <= float(self.max_tx_gain_db) <= 0:
            out.append("fast-drive-cap")
        if frequency_hz is not None and not out and wide_data_refusal(frequency_hz, self.widest()):
            out.append("fast-out-of-plan")
        return out

    # -- what the objects take ------------------------------------------------------

    def pluto_kwargs(self) -> dict:
        """radios.PlutoLink's keyword arguments for these settings."""
        kw = {"ptt_output": self.ptt_output, "relay_lead_ms": self.relay_lead_ms,
              "relay_tail_ms": self.relay_tail_ms, "max_tx_gain_db": self.max_tx_gain_db}
        if self.adapt:
            kw["modes"] = self.ladder()
        else:
            kw["mode"] = modem.mode(self.mode).name
        if self.turnaround_ms is not None:
            kw["turnaround_s"] = self.turnaround_ms / 1000.0
        return kw

    def guard_kwargs(self) -> dict:
        """shack_pluto.guard.Guard's: drop a driven GPO, keep the cap."""
        return {"gpo": self.ptt_output.startswith("gpo"), "max_gain_db": self.max_tx_gain_db}

    def channel_config(self, **over):
        from .channel import ChannelConfig
        return ChannelConfig(txdelay_s=self.txdelay_ms / 1000.0, mode=None if self.adapt else
                             modem.mode(self.mode).name, max_frames=self.window + 1, **over)

    def link_config(self, **over):
        from .datalink import LinkConfig
        return LinkConfig(k=self.window, n1=self.paclen, modulo=128 if self.window > 7 else 8, srej=self.srej,
                          adapt=self.ladder(), adapt_start=self.adapt_start if self.adapt else None, **over)


def wide_data_refusal(frequency_hz: float, mode) -> str | None:
    """Why `mode` may not be sent at `frequency_hz` in the UK, or None. A
    mode no wider than WIDE_HZ is not this check's business."""
    m = modem.mode(mode)
    if m.occupied_hz <= WIDE_HZ:
        return None
    lo, hi = frequency_hz - m.occupied_hz / 2, frequency_hz + m.occupied_hz / 2
    for low, high, widest, what in WIDE_DATA_SEGMENTS:
        if low <= lo and hi <= high:
            if m.occupied_hz > widest:
                return f"{m.name} is {m.occupied_hz / 1e3:.0f} kHz wide; {what} allows {widest / 1e3:.0f} kHz"
            return None
    return (f"{m.name} ({m.occupied_hz / 1e3:.0f} kHz) at {frequency_hz / 1e6:.4f} MHz is not inside a band-plan "
            f"segment for wide data")


def wide_data_notes(frequency_hz: float, mode, ifo_hz: float) -> list[str]:
    """The LO leakage and IQ image when they fall outside the segment."""
    m = modem.mode(mode)
    for low, high, _w, _what in WIDE_DATA_SEGMENTS:
        if low <= frequency_hz <= high:
            return [f"{name} at {f / 1e6:.4f} MHz is outside {low / 1e6:.3f}-{high / 1e6:.3f} MHz"
                    for name, f in (("the LO leakage", frequency_hz - ifo_hz),
                                    ("the IQ image", frequency_hz - 2 * ifo_hz)) if not low <= f <= high]
    return [] if m.occupied_hz <= WIDE_HZ else ["not in a wide-data segment"]
