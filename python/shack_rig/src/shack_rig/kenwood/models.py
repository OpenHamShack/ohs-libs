"""The Kenwood radios this backend knows: the TM-V71 (E and A).

A table, as yaesu/models.py and icom/models.py are, though there is one
row: the TM-V71E (Europe) and TM-V71A (Americas) are one radio to a
computer - the same PC port, the same commands, the same DATA jack - and
differ in what they are allowed to transmit on, which is the operator's
licence's business (the lease and shack_bandplan), not the radio's
identity. `TY` says which it is (P1 M for the EU version, K for the US:
LA3QMA TY.md) and the rig reports it; nothing depends on it.

Sources, as for cat.py (LA3QMA's command list, hamlib's tmd710.c
`tmv71_caps`, CHIRP's kenwood_live.TMV71Radio), plus the Kenwood RC-D710
instruction manual (the TM-V71's remote head: its "AUX <RC-D710 + TM-V71>"
menu table, p. 24, and DATA terminal table, p. 17 - manual.kenwood.com,
document 1000006167), and hamvoip.org's "Kenwood TM-V71" set-up note for
the menu numbers as the radio's own panel shows them:

- **PC port**: 8-pin mini-DIN "PC" on the rear (hamvoip), Kenwood's PG-5G
  cable; menu 519 PC PORT BAUDRATE 9600/19200/38400/57600, factory 9600
  (RC-D710 p. 24). 8 data bits, no parity, 1 stop bit, no handshaking
  (hamlib `tmv71_caps`: RIG_HANDSHAKE_NONE - where its TM-D710 entry says
  hardware; CHIRP's live-mode base class asks for RTS/CTS, which a
  three-wire cable simply ignores).
- **Coverage** (receive): hamlib's rx_range_list1 for the TM-V71(A): 118-470
  MHz on band A only (AM below 136), 136-174 and 300-524 MHz on both, and
  800-1300 MHz on band B only. Transmit: Region 1 2 m and 70 cm (this
  library's convention, model.py), 5-50 W on 2 m and 5-35 W on 70 cm
  (hamlib's tx_range_list1; the medium step is not given there).
- **A band is VHF or UHF, chosen on the radio.** "It is possible to set
  frequency only inside the selected frequency band of the current VFO,
  since there is no command to change the frequency band of a VFO"
  (hamlib tmd710.c, its header); LA3QMA FO.md: "must be within selected
  band". So the rig picks whichever band is already on the right one and
  says so when neither is (`segment`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..i18n import _
from ..model import SEVENTY_CM, TWO, RigModel

#: The receive segments a band can be on, by what tuning across them needs:
#: within a segment FO moves the frequency; between them the operator
#: presses the band's BAND key (or the rig uses the other band).
SEGMENTS = {
    "vhf": (118_000_000, 174_000_000),
    "uhf": (300_000_000, 524_000_000),
    "1200": (800_000_000, 1_300_000_000),
}


def segment(hz: int | None) -> str | None:
    """Which segment `hz` is in ("vhf", "uhf", "1200"), or None."""
    if hz is None:
        return None
    for name, (lo, hi) in SEGMENTS.items():
        if lo <= hz <= hi:
            return name
    return None


@dataclass(frozen=True, kw_only=True)
class KenwoodModel(RigModel):
    """A RigModel with the PC-port columns."""

    maker: str = "kenwood"
    #: What `ID` answers after "ID " (CHIRP kenwood_live: "TM-V71").
    pc_id: str = ""
    #: The PC port's speed as the radio ships, and its menu's choices.
    pc_baud: int = 9600
    pc_bauds: tuple = (9600, 19200, 38400, 57600)
    #: Segments each band can be on: band A has no 1.2 GHz, band B no airband
    #: AM (hamlib rx_range_list1's VFO columns).
    band_segments: dict = field(default_factory=dict, hash=False, compare=False)
    #: The DATA jack's speeds, as EXT. DATA SPEED offers them.
    data_speeds: tuple = (1200, 9600)
    verified: bool = False

    def public(self) -> dict:
        got = super().public()
        got.update({"pc_baud": self.pc_baud, "pc_id": self.pc_id, "verified": self.verified,
                    "data_speeds": list(self.data_speeds)})
        return got


MODELS: dict[str, KenwoodModel] = {}


def _add(m: KenwoodModel) -> None:
    MODELS[m.key] = m


_add(KenwoodModel(
    key="tm-v71", name="TM-V71", pc_id="TM-V71", pc_baud=9600,
    modes=["FM", "AM"],
    rx_ranges=[(118_000_000, 174_000_000), (300_000_000, 524_000_000), (800_000_000, 1_300_000_000)],
    tx_ranges=[TWO, SEVENTY_CM],
    tx_power={TWO[0]: (5_000, 50_000), SEVENTY_CM[0]: (5_000, 35_000)},
    band_segments={0: ("vhf", "uhf"), 1: ("vhf", "uhf", "1200")},
    vfos=2, verified=False,
    note="From LA3QMA's TM-V71/TM-D710 command list, hamlib's TM-D710 backend and CHIRP's TM-V71 driver; not "
         "yet checked against a radio. PC port at 9600 8N1 (menu 519). Packet goes through the DATA jack: "
         "audio and PTT from a sound-card interface, never a CAT transmit. EXT. DATA BAND (517) and EXT. "
         "DATA SPEED (518) are set over CAT and put back.",
))

DEFAULT_MODEL = "tm-v71"


def model(key: str) -> KenwoodModel:
    got = MODELS.get((key or "").strip().lower())
    if got is None:
        raise ValueError(_("unknown radio {key}; this module knows {known}").format(
            key=repr(key), known=", ".join(sorted(MODELS))))
    return got
