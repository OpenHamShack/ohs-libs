"""What a rig is, in words every maker's backend shares.

Moved out of OpenHamShack Icom's `radios.py`, which already said the right
thing about itself: what changes from one radio to the next is "small and
entirely tabular" - which bands it has, how much power it makes on each, and
which modes exist. That sentence is not about Icom. A Yaesu or a Kenwood
backend needs the same table with a different addressing column, so the
table's shape lives here and each maker adds only its own column (the Icom
CI-V address is `shack_rig.icom.models.IcomModel`).

**Mode names are the vocabulary, not a maker's wire format.** LSB, USB, AM,
CW, RTTY, FM, WFM, CWR, RTTYR, DV, DD, PSK, PSKR: the names Icom's radios put
on their own screens, which are also - near enough - what hamlib calls them
(the rigctl server does the few translations). A backend maps these to its
bytes; nothing above a backend ever sees a mode byte.

**DATA is a flag, not a mode.** "USB-D" is USB with the data flag set, which
routes transmit audio from the computer instead of the microphone. The note on
command 0x26 in `icom/civ.py` is the long argument; the short one is that a
mode table with a USB-D entry would be inventing wire format no radio sends.
Pages show `mode_label()`, because USB and USB-D are different things to an
operator and only one of them works FT8.

**Transmit ranges are licence ranges.** Region 1 amateur allocations rather
than what the hardware can reach, so a hamlib client reading dump_state is not
offered somewhere a licence does not cover. This is a floor, not the band-plan
check: whether *this operator* may transmit *here* is `shack_bandplan`'s
question and the app's lease asks it before a rig is ever keyed.

**Receive ranges are the hardware's.** What the radio can be tuned to, from
its maker's specification, and nothing narrower. They were once the same
Region 1 tuples as the transmit ranges, so an IC-9700 refused to *listen* on
146.5 MHz - a frequency the radio tunes, the US version transmits on, and a
UK NoV licenses - because a table that "read as bands" had become a tuning
limit. Tuning outside a licence is not transmitting; the lease is what stands
between a frequency and a keyed rig, and a radio asked for somewhere its own
version cannot go answers NG and stays where it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Every mode name a backend may report. A rig has a subset (`RigModel.modes`);
#: one it has not got is refused by name rather than sent and ignored.
MODES = ("LSB", "USB", "AM", "CW", "RTTY", "FM", "WFM", "CWR", "RTTYR",
         "DV", "DD", "PSK", "PSKR")

#: The modes that have a data variant. The flag's job is to take transmit audio
#: from the computer instead of the microphone, so it means something only for
#: the modes that carry audio: the two sidebands, AM and FM. CW has no audio
#: path to redirect, RTTY and PSK are already fed from the data input, and DV
#: and DD carry their own digital stream.
#:
#: Whether a particular radio honours the flag is the radio's business. A
#: backend reads the flag back from the radio, so a rig that ignores the
#: request shows as not in data rather than a page claiming USB-D at a radio
#: sitting in USB.
DATA_CAPABLE = ("LSB", "USB", "AM", "FM")

#: The suffix a radio's own display puts on a data mode, and what this library
#: calls it everywhere above a backend: USB-D, LSB-D, FM-D.
DATA_SUFFIX = "-D"


def mode_label(mode: str | None, data: bool = False) -> str | None:
    """What to call a mode on a page: USB, or USB-D when the flag is set."""
    if not mode:
        return None
    return mode + DATA_SUFFIX if data else mode


def split_mode_label(label: str) -> tuple[str, bool]:
    """The inverse: "USB-D" -> ("USB", True), "USB" -> ("USB", False).

    Accepted on the way in so a caller holding the name that is on the screen
    can hand it straight back rather than taking it apart itself. "-D" counts
    as the suffix only when what is left is a mode this library knows, so a
    future mode whose own name ends in -D is not quietly cut in half.
    """
    label = (label or "").strip().upper()
    if label.endswith(DATA_SUFFIX) and label[: -len(DATA_SUFFIX)] in MODES:
        return label[: -len(DATA_SUFFIX)], True
    return label, False


#: Region 1 amateur allocations, so the model tables read as bands. These are
#: for `tx_ranges` and band names only - never a radio's `rx_ranges`, which is
#: what the hardware tunes (see "Receive ranges" above).
HF = [
    (1_810_000, 2_000_000), (3_500_000, 3_800_000), (5_351_500, 5_366_500),
    (7_000_000, 7_200_000), (10_100_000, 10_150_000), (14_000_000, 14_350_000),
    (18_068_000, 18_168_000), (21_000_000, 21_450_000), (24_890_000, 24_990_000),
    (28_000_000, 29_700_000),
]
SIX = (50_000_000, 52_000_000)
FOUR = (70_000_000, 70_500_000)
TWO = (144_000_000, 146_000_000)
SEVENTY_CM = (430_000_000, 440_000_000)
TWENTYTHREE_CM = (1_240_000_000, 1_300_000_000)


def _ranges_text(ranges) -> str:
    return ", ".join(f"{lo / 1e6:g}-{hi / 1e6:g} MHz" for lo, hi in ranges) or "no band at all"


@dataclass(frozen=True, kw_only=True)
class RigModel:
    """One kind of radio: its modes, its coverage and its power per band.

    Keyword-only so a maker's subclass can add a required column (the Icom
    CI-V address) after the defaulted ones here without reordering anything.
    """

    key: str
    name: str
    modes: list
    rx_ranges: list           # (start_hz, end_hz)
    tx_ranges: list           # (start_hz, end_hz)
    tx_power: dict            # band start hz -> (min milliwatts, max milliwatts)
    vfos: int = 2
    note: str = ""
    maker: str = field(default="", compare=False)
    #: The receive front end, as the radio's own panel names it. `preamps` is
    #: the levels above OFF, so level 0 is always off and level n is
    #: preamps[n - 1]: ("P.AMP",) on an IC-9700, ("P.AMP1", "P.AMP2") on an
    #: IC-7610. `attenuators` is the steps in dB above 0, so 0 is always off.
    #: Empty means the radio has none, or nobody has checked - and a caller
    #: offers a control only for what is listed here, never by radio name.
    preamps: tuple = ()
    attenuators: tuple = ()
    #: Every setting the preamp control can be in, named, where the list is not
    #: simply OFF followed by `preamps`. Index n is state["preamp_mode"] n.
    #: Only an IC-9700 needs it today: its one byte also switches an external
    #: (mast-head) preamp, so its four settings are OFF, P.AMP, EXT and both -
    #: combinations a level list cannot express. Empty means "derive it", which
    #: keeps every other radio's table as it was. See `preamp_mode_names`.
    preamp_modes: tuple = ()

    def has_mode(self, mode: str) -> bool:
        return mode in self.modes

    def data_modes(self) -> list:
        """The modes this radio offers with the data flag set.

        Derived rather than given a column of its own: it is what the radio has
        crossed with what the flag means anything for, and every radio so far
        agrees on that. A model that differs gets an override the day one
        turns up - identical lists kept in step by hand is how the IC-9700's
        CI-V address once leaked into the other Icom entries.
        """
        return [m for m in self.modes if m in DATA_CAPABLE]

    def has_data_mode(self, mode: str) -> bool:
        return mode in DATA_CAPABLE and self.has_mode(mode)

    def has_preamp(self, level: int) -> bool:
        return 0 <= level <= len(self.preamps)

    def preamp_mode_names(self) -> list:
        """The preamp control's settings in order, as the panel names them.

        Derived as ["OFF", *preamps] unless the table gives `preamp_modes`, so
        on every radio but the IC-9700 mode n and level n are the same thing
        and a client may use either. A radio with no preamp has none at all,
        not a lone "OFF" - a page draws no control for an empty list.
        """
        if self.preamp_modes:
            return list(self.preamp_modes)
        return ["OFF", *self.preamps] if self.preamps else []

    def has_att(self, db: int) -> bool:
        return db == 0 or db in self.attenuators

    def covers(self, hz: int) -> bool:
        return any(lo <= hz <= hi for lo, hi in self.rx_ranges)

    def can_transmit(self, hz: int) -> bool:
        return any(lo <= hz <= hi for lo, hi in self.tx_ranges)

    def transmit_bands_text(self) -> str:
        """The licensed transmit ranges, for a refusal a person can read.

        `tx_ranges` is not one span, so naming "the range" would either lie
        about the gaps between bands or list every one anyway - which is what
        this does.
        """
        return _ranges_text(self.tx_ranges)

    def receive_bands_text(self) -> str:
        """What the radio tunes, the same way, for a refused tune.

        A VHF/UHF rig's coverage has gaps a lowest-to-highest span hides:
        "tunes 144 to 1300 MHz" was what the IC-9700 refusal used to say, and
        it reads as though 146.5 MHz should have worked.
        """
        return _ranges_text(self.rx_ranges)

    def public(self) -> dict:
        return {
            "key": self.key, "name": self.name, "maker": self.maker,
            "modes": list(self.modes),
            "data_modes": self.data_modes(),
            "bands": [
                {"from": lo, "to": hi,
                 "watts": round(self.tx_power.get(lo, (0, 0))[1] / 1000)}
                for lo, hi in self.tx_ranges
            ],
            # Level n is preamps[n - 1]; 0 is off. A page draws one control per
            # entry and nothing at all when the list is empty.
            "preamps": list(self.preamps),
            # The same control as a cycle of whole settings, index n being
            # state["preamp_mode"] n. Where it differs from OFF + preamps
            # (an IC-9700's external preamp) a client should cycle this one,
            # because a level alone cannot say "EXT" or "P.AMP+EXT".
            "preamp_modes": self.preamp_mode_names(),
            "attenuators": list(self.attenuators),
            "note": self.note,
        }


#: A spot inside each band to jump to. The centre of a band is rarely where
#: anybody wants to be, so these are the calling and activity frequencies where
#: there is a conventional one, and a sensible corner of the band otherwise.
BAND_SPOTS = {
    1_810_000: ("160m", 1_840_000),
    3_500_000: ("80m", 3_600_000),
    5_351_500: ("60m", 5_357_000),
    7_000_000: ("40m", 7_090_000),
    10_100_000: ("30m", 10_136_000),
    14_000_000: ("20m", 14_074_000),
    18_068_000: ("17m", 18_100_000),
    21_000_000: ("15m", 21_074_000),
    24_890_000: ("12m", 24_915_000),
    28_000_000: ("10m", 28_074_000),
    50_000_000: ("6m", 50_313_000),
    70_000_000: ("4m", 70_200_000),
    144_000_000: ("2m", 144_300_000),
    430_000_000: ("70cm", 432_200_000),
    1_240_000_000: ("23cm", 1_296_200_000),
}


def band_buttons(model: RigModel) -> list:
    """The bands this radio transmits on, and where to land in each."""
    out = []
    for lo, hi in model.tx_ranges:
        name, spot = BAND_SPOTS.get(lo, (f"{lo // 1_000_000} MHz", lo + (hi - lo) // 2))
        out.append({"name": name, "hz": spot})
    return out
