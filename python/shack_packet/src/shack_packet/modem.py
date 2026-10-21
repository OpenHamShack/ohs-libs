"""Which modem a channel runs: 1200 baud AFSK, G3RUH FSK at 9600 and up,
or the OHS-only wide modes (GFSK at 76 800 and 153 600 baud, 4-FSK).

One setting picks both ends: the channel's mode (channel.ChannelConfig.mode
or .baud, or the link's). 1200 is Bell 202 AFSK (afsk.py out,
demod.Receiver in), the default and what every 2 m station speaks; 9600,
19 200 and 38 400 are G3RUH-compatible scrambled FSK (fsk.py), which needs
a radio that passes the baseband - a 9600-capable data port, or an SDR
that is its own modulator (radios.pluto). The engine, the channel's
airtime sums and the link all ask here, so nothing else has to know which
it is.

**Modes are named** (`Mode.name`, what an app shows and a setting stores)
because the baud no longer says which modem it is: 4-FSK at 38 400 baud
carries 76 800 bit/s, the same bit rate as GFSK at 76 800 baud, and needs
a different receiver. A bare number still works for the modes that were
numbers before (`check`, `ChannelConfig(baud=9600)`), and means the
two-level mode with that bit rate.

**The wide modes** (18/10/2026) are for two OpenHamShack stations on SDRs
(a Pluto each end), not for interop: nobody else speaks them. They keep
G3RUH's whole bit stream - NRZI, the x^17 + x^12 + 1 scrambler, HDLC
flags and stuffing, FX.25 if asked - and only change what carries it:

- `gfsk76800`, `gfsk153600`: G3RUH's own shape (Gaussian BT 0.5) and
  modulation index (h = 0.625: +-3 kHz at 9600 in proportion), faster.
  Kept because it is the one that measured best at 9600-38 400 and a
  narrower index costs more than it saves: at 153 600 baud h = 0.5 is
  158 kHz wide (8 kHz less) and needs 2 dB more CNR, and BT 0.35 at
  h = 0.5 fits 146 kHz but needs 4 dB more (fsk_bench, 18/10/2026).
- `4fsk38400`, `4fsk76800`: two bits a symbol, so 76.8 and 153.6 kbit/s in
  less bandwidth than the GFSK mode of the same bit rate. M17's levels
  and Gray mapping (fsk.FOUR_LEVELS), the outer pair at +-0.625 of the
  symbol rate (h = 0.42 between neighbours), a raised-cosine step one
  symbol long so every level is reached at its symbol's middle. The
  framing needs nothing of its own: the bit stream is G3RUH's, two bits
  at a time, and HDLC's flags find the frames in it wherever the pairs
  fall - the descrambler synchronises itself on bits, not symbols, and a
  receiver that joined a stream one bit "off" in its pairing cannot exist,
  because each symbol is read as its own two bits in order. Unlike 2-FSK
  it is not polarity-free: an inverted discriminator turns 01 into 11 and
  00 into 10 (the first bit of every pair), which NRZI cannot undo. An
  SDR receiver knows its own polarity, so it costs nothing here; a radio's
  discriminator output would need the setting.

Bandwidths (99 %, fsk_bench `occupied`): see `Mode.occupied_hz`. The UK
23 cm band plan's high-speed data channels are 150 kHz (1299.000-1299.750
and 1241.000-1241.750 MHz, five channels each); 13 cm's 2311-2315 MHz high
speed data allows 200 kHz. So gfsk76800 and both 4-FSK modes fit a 23 cm
channel, and gfsk153600 (166 kHz) is a 13 cm mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import afsk, demod, fsk
from .errors import PacketError


@dataclass(frozen=True)
class Mode:
    """One modem setting. `baud` is symbols a second, `bits` bits a symbol;
    `deviation_hz` the peak (the outer symbols'); `occupied_hz` the 99 %
    bandwidth measured through the Pluto's transmitter (fsk_bench)."""
    name: str
    kind: str                      # "afsk", "g3ruh", "gfsk" or "4fsk"
    baud: int
    bits: int = 1
    deviation_hz: float = 3_000.0
    bt: float = fsk.GAUSSIAN_BT
    shape: str = "gaussian"
    occupied_hz: float = 0.0
    #: Flags a receiver needs to lock on after the radio is up (channel's
    #: TXDELAY floor): channel.MIN_PREAMBLE_S at 1200 baud, and measured
    #: through the IQ loopback at the others (fsk_bench, 18/10/2026, 20
    #: frames each its own over after noise): 2-FSK decoded 0/18-19/20 of
    #: 20 with 16/32/64 bits of flags at every speed, 4-FSK 0/6/19 with
    #: 64/128/256. Kept with half again to spare.
    lock_bits: int = 96

    @property
    def bitrate(self) -> int:
        return self.baud * self.bits

    @property
    def fast(self) -> bool:
        """Baseband FSK rather than audio tones: needs a link that carries it."""
        return self.kind != "afsk"

    def __str__(self) -> str:
        return self.name


def _g3ruh(baud: int, kind: str = "g3ruh") -> Mode:
    return Mode(f"{kind}{baud}", kind, baud, 1, fsk.deviation_hz(baud), occupied_hz=fsk.occupied_hz(baud))


#: Every mode, slowest first.
MODES: dict[str, Mode] = {m.name: m for m in (
    # 1200 baud AFSK through the Pluto's FM modulator at its 3 kHz
    # deviation (radios/pluto.py FmModulator), measured as fsk_bench's
    # `occupied` measures the fast modes (random frames, 99 %): 9.2 kHz,
    # with 27 dB of the power outside a 12.5 kHz channel (19/10/2026). It
    # was 0, and the Link test page said the mode "occupies 0 kHz".
    Mode("afsk1200", "afsk", 1200, occupied_hz=9_200.0),
    _g3ruh(9600), _g3ruh(19_200), _g3ruh(38_400),
    _g3ruh(76_800, "gfsk"), _g3ruh(153_600, "gfsk"),
    # Occupied bandwidths measured on fsk_bench's spectrum (18/10/2026).
    Mode("4fsk38400", "4fsk", 38_400, 2, 24_000.0, shape="rc", occupied_hz=70_500.0, lock_bits=384),
    Mode("4fsk76800", "4fsk", 76_800, 2, 48_000.0, shape="rc", occupied_hz=141_000.0, lock_bits=384),
)}
#: Each fast mode's number in the link adaptation's XID parameter
#: (xid.PI_OHS_MODES, adapt.py). Never renumber: two stations running
#: different versions read each other's numbers.
CODES = {"g3ruh9600": 1, "g3ruh19200": 2, "g3ruh38400": 3, "gfsk76800": 4, "gfsk153600": 5, "4fsk38400": 6,
         "4fsk76800": 7}
BY_CODE = {code: name for name, code in CODES.items()}
#: The modes that were numbers: the bit rate of a two-level mode.
BAUDS = (1200, *fsk.BAUDS)
#: The names an app shows and a setting may use, by the old numbers.
NAMES = {m.bitrate: m.name for m in MODES.values() if m.bits == 1}


def mode(spec) -> Mode:
    """A Mode from a Mode, a name ("4fsk76800") or a number (the two-level
    mode of that bit rate: 9600 is g3ruh9600, 76800 gfsk76800)."""
    if isinstance(spec, Mode):
        return spec
    if isinstance(spec, str) and spec.lower() in MODES:
        return MODES[spec.lower()]
    try:
        b = int(spec)
    except (TypeError, ValueError):
        raise PacketError(f"no modem called {spec!r} (have {', '.join(MODES)})") from None
    if b not in NAMES:
        raise PacketError(f"no modem for {b} baud (have {', '.join(map(str, NAMES))}, or a name: "
                          f"{', '.join(MODES)})")
    return MODES[NAMES[b]]


_lookup = mode                     # for the functions below whose `mode` argument hides it


def of_link(link) -> Mode:
    """The mode a radio link is set up for: its `mode` if it says, else its
    `baud`, else AFSK."""
    m = getattr(link, "mode", None)
    if m:
        return mode(m)
    return mode(int(getattr(link, "baud", 1200) or 1200))


def check(baud) -> int:
    """The baud as an int, or PacketError if no two-level modem here speaks
    it (the numbered modes; a 4-FSK mode needs its name: `mode`)."""
    try:
        b = int(baud)
    except (TypeError, ValueError):
        raise PacketError(f"{baud!r} is not a baud rate") from None
    if b not in BAUDS:
        raise PacketError(f"no modem for {b} baud (have {', '.join(map(str, BAUDS))})")
    return b


def by_name(name: str) -> int:
    """A mode name ("afsk1200", "g3ruh9600", ...) or a bare baud, as the
    mode's bit rate."""
    return mode(name).bitrate


def modulate(frames, rate: int, *, baud: int = 1200, mode=None, amplitude: float = 0.5, fx25_mode=None,
             txdelay_ms: float = 300.0, tail_ms: float = 30.0):
    """One transmission at `rate`: audio for AFSK, shaped baseband for FSK.
    `mode` (a Mode or name) wins over `baud`."""
    m = _lookup(mode if mode is not None else baud)
    if not m.fast:
        return afsk.modulate(frames, rate, amplitude=amplitude, fx25_mode=fx25_mode, txdelay_ms=txdelay_ms,
                             tail_ms=tail_ms)
    return fsk.modulate(frames, rate, baud=m.baud, bits=m.bits, shape=m.shape, bt=m.bt, amplitude=amplitude,
                        fx25_mode=fx25_mode, txdelay_ms=txdelay_ms, tail_ms=tail_ms)


def receiver(rate: int, *, baud: int = 1200, mode=None, block_ms: float = 100.0, diversity: bool = True):
    """The receiver for the mode at `rate`, demod.Receiver's interface either way."""
    m = _lookup(mode if mode is not None else baud)
    if not m.fast:
        return demod.Receiver(rate, block_ms=block_ms, diversity=diversity)
    return fsk.Receiver(rate, m.baud, bits=m.bits, block_ms=block_ms, diversity=diversity)
