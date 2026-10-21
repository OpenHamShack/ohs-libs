"""The Yaesu radios this backend knows, and what differs between them.

A table, as icom/models.py is, and for its reason: CAT is one language
whose dialects differ in small, tabular ways - the CAT speed a radio ships
at, the IF layout, whether RI exists, where in the menu MOD SOURCE lives.
Adding a radio is a row here, never a change to the protocol.

**FTX-1** (Field and Optima; they differ in CAT only in AC, the internal
tuner, p. 6): every value below is from Yaesu's FTX-1 CAT Operation
Reference Manual 2508-C, pages as printed. Two things in that manual are
open and are marked where they are used:

- **Which MOD SOURCE governs DATA-FM.** The menu has a MODE FM block
  (EX 01 03, MOD SOURCE at P3 12) and a MODE DATA block (EX 01 04, MOD
  SOURCE at P3 13) and does not say which applies in DATA-FM. Both are
  listed, and a packet link sets both (and puts both back): whichever one
  the radio reads, it reads USB.
- **The USB codec's rate and the radio's key-up time** are not in the CAT
  manual at all. The sound device is opened at its own rate
  (shack_rig.sound), and the key-up time is measured (shack_packet's
  turnaround tool), not assumed.

**FT-991A**: only what has a source outside memory, and marked unverified
until it meets a radio. hamlib 4.6 (`rigctl -m 1035 -u`, the FT-991 driver
the 991A uses) says 4800-38400 baud at 8N2 with CTS/RTS handshaking, the
receive ranges below and the power limits; this
library holds RTS low (serialio.py), so the 991A's own CAT RTS menu item
must be off - the note says so. hamlib also says it cannot read the
radio's DCD, so there is no squelch-as-busy (the packet link uses its own
carrier detect). Its MD codes and TX are the newcat ones hamlib sends both
radios; its IF layout is not the FTX-1's and is not parsed; its menu is
flat-numbered and not in this table, so no MOD SOURCE row - the operator
sets the DATA modulation source to USB on the radio.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..i18n import _
from ..model import HF, SEVENTY_CM, SIX, TWO, RigModel


@dataclass(frozen=True)
class MenuItem:
    """One EX menu item: where it is, its width, and its values by name."""
    p1: int
    p2: int
    p3: int
    digits: int
    values: dict
    confirmed: bool
    source: str

    def value(self, name: str) -> int | None:
        return self.values.get(name)

    def name(self, value: int) -> str | None:
        for k, v in self.values.items():
            if v == value:
                return k
        return None


@dataclass(frozen=True, kw_only=True)
class YaesuModel(RigModel):
    """A RigModel with the CAT columns."""

    maker: str = "yaesu"
    #: What ID; answers (p. 17: "0840 (Fixed)" on an FTX-1).
    cat_id: str = ""
    #: The Enhanced COM port (CAT-1) as the radio ships: speed and stop bits.
    cat_baud: int = 38400
    cat_stop_bits: int = 1
    #: The speeds its menu offers, for a settings page.
    cat_bauds: tuple = (4800, 9600, 19200, 38400, 115200)
    #: "ftx1" for the 30-character IF (cat.parse_if); "" where IF is not parsed.
    if_layout: str = ""
    #: Whether RI0; reports the squelch (busy) and the TX state.
    has_radio_info: bool = False
    #: MOD SOURCE per mode block, {"data": MenuItem, "fm": MenuItem}. Empty
    #: where the menu is not sourced: nothing is written then.
    mod_source: dict = field(default_factory=dict, hash=False, compare=False)
    #: USB MOD GAIN per mode block, as `mod_source`: {"data": MenuItem,
    #: "fm": MenuItem}, 000-100. Empty where it is not sourced.
    mod_gain: dict = field(default_factory=dict, hash=False, compare=False)
    #: Whether the data set-up's switches (cat.py: BC, RL, NL, VX, ML, PR)
    #: are this radio's, from its CAT manual.
    data_switches: bool = False
    #: What a packet station's FM data mode is called in cat.VOCABULARY terms.
    packet_mode: tuple = ("FM", True, False)
    verified: bool = False

    def public(self) -> dict:
        got = super().public()
        got.update({"cat_baud": self.cat_baud, "cat_id": self.cat_id, "verified": self.verified})
        return got


MOD_VALUES = {"mic": 0, "usb": 1, "bluetooth": 2, "auto": 3}

FTX1_MOD_SOURCE = {
    # RADIO SETTING > MODE DATA > MOD SOURCE, EX 01 04 13 (Table 3, p. 11).
    "data": MenuItem(1, 4, 13, 1, MOD_VALUES, True, "FTX-1 CAT manual p. 11"),
    # RADIO SETTING > MODE FM > MOD SOURCE, EX 01 03 12 (Table 3, p. 10).
    "fm": MenuItem(1, 3, 12, 1, MOD_VALUES, True, "FTX-1 CAT manual p. 10"),
}

FTX1_MOD_GAIN = {
    # RADIO SETTING > MODE DATA > USB MOD GAIN, EX 01 04 14, 000-100 (p. 11).
    "data": MenuItem(1, 4, 14, 3, {}, True, "FTX-1 CAT manual p. 11"),
    # RADIO SETTING > MODE FM > USB MOD GAIN, EX 01 03 13, 000-100 (p. 10).
    "fm": MenuItem(1, 3, 13, 3, {}, True, "FTX-1 CAT manual p. 10"),
}

#: FTX-1 coverage, "P1 000030000 - 470000000 (Hz)" for FA (p. 16): the
#: hardware's, which is what it will be told to tune (model.py, "Receive
#: ranges are the hardware's").
FTX1_COVERAGE = [(30_000, 470_000_000)]

MODELS: dict[str, YaesuModel] = {}


def _add(m: YaesuModel) -> None:
    MODELS[m.key] = m


_add(YaesuModel(
    key="ftx-1", name="FTX-1", cat_id="0840", cat_baud=38400, cat_stop_bits=1,
    modes=["LSB", "USB", "CW", "CWR", "FM", "AM", "RTTY", "RTTYR", "PSK"],
    rx_ranges=FTX1_COVERAGE,
    tx_ranges=HF + [SIX, TWO, SEVENTY_CM],
    # The Field head's 10 W on an external supply (6 W on its battery); the
    # Optima's 100 W on HF is more, and the radio does not say which head it
    # is - so the smaller, as icom/models.py does for the IC-705's battery.
    tx_power={lo: (500, 10_000) for lo, _hi in HF + [SIX, TWO, SEVENTY_CM]},
    if_layout="ftx1", has_radio_info=True, mod_source=FTX1_MOD_SOURCE, mod_gain=FTX1_MOD_GAIN,
    data_switches=True, verified=False,
    note="From the FTX-1 CAT manual 2508-C; not yet checked against a radio. CAT on the Enhanced "
         "COM port (CAT-1) at 38400 8N1. Set MOD SOURCE to USB for DATA (and FM): the packet link does "
         "it and puts it back. RPTT SELECT stays OFF unless a PTT line is chosen.",
))

_add(YaesuModel(
    key="ft-991a", name="FT-991A", cat_id="", cat_baud=4800, cat_stop_bits=2,
    modes=["LSB", "USB", "CW", "CWR", "FM", "AM", "RTTY", "RTTYR"],
    rx_ranges=[(30_000, 56_000_000), (118_000_000, 164_000_000), (420_000_000, 470_000_000)],
    tx_ranges=HF + [SIX, TWO, SEVENTY_CM],
    tx_power={lo: (5_000, 100_000) for lo, _hi in HF + [SIX]} | {TWO[0]: (5_000, 50_000),
                                                                  SEVENTY_CM[0]: (5_000, 50_000)},
    if_layout="", has_radio_info=False, verified=False,
    note="Unverified. From hamlib 4.6's FT-991 driver: 4800 baud 8N2, the receive ranges and the 5-100 W "
         "(HF, 6 m) and 5-50 W (2 m, 70 cm) power; its ID is not sourced, so it is not checked. Turn the "
         "radio's CAT RTS menu item off "
         "(this library holds RTS low), and set its DATA MOD SOURCE to USB by hand: its flat menu "
         "numbers are not in this table, so nothing is written.",
))

DEFAULT_MODEL = "ftx-1"


def model(key: str) -> YaesuModel:
    got = MODELS.get((key or "").strip().lower())
    if got is None:
        raise ValueError(_("unknown radio {key}; this module knows {known}").format(
            key=repr(key), known=", ".join(sorted(MODELS))))
    return got
