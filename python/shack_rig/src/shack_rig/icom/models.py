"""The Icom radios this backend knows, and what differs between them.

The maker-neutral half of the table (modes, ranges, power, what DATA means)
is `shack_rig.model.RigModel`; this file adds the one column only Icom has,
the CI-V address, and the wire table of mode bytes. Moved from OpenHamShack
Icom's `app/radios.py` unchanged in substance.

The LAN protocol does not change from one networked Icom to the next - the
handshake, the token, the passcode and the CI-V stream are the same on an
IC-9700, an IC-7610 and an IC-705. What changes is small and entirely
tabular: the CI-V address the radio answers to, which bands it has, how much
power it makes on each, and which modes exist.

So this is a table rather than a class hierarchy. Adding a radio is an entry
here; it should never require touching the protocol.

**The CI-V addresses are factory defaults and every one of them is settable**
in the radio's own menu, which is the first thing to check when a radio is
plainly reachable and answers nothing: a frame addressed to the wrong CI-V
address is not rejected, it is ignored, and the symptom is silence rather than
an error. The address in use is on the radio at Menu > Set > Connectors >
CI-V > CI-V Address.

Ranges are what the radio will be *told* to tune. Transmit ranges are the
Region 1 amateur allocations rather than what the hardware can physically
reach, so a client that reads dump_state does not offer to transmit somewhere
a licence does not cover. Receive ranges are the radio's own coverage, which
on the IC-705 and IC-7610 is far wider than the amateur bands and on the
IC-9700 is a little wider (see `IC9700_COVERAGE`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..i18n import _

from ..model import (DATA_CAPABLE, FOUR, HF, SEVENTY_CM, SIX, TWENTYTHREE_CM,  # noqa: F401
                     TWO, RigModel)

#: Every mode byte this module can name, across all the radios. An individual
#: radio has a subset; asking for one it does not have is refused by name
#: rather than sent and ignored.
ALL_MODES = {
    0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
    0x05: "FM", 0x06: "WFM", 0x07: "CWR", 0x08: "RTTYR",
    0x17: "DV", 0x22: "DD", 0x12: "PSK", 0x13: "PSKR",
}


@dataclass(frozen=True)
class DataMod:
    """Where a radio's DATA MOD menu item is, and what its values mean.

    DATA MOD is the source of transmit audio in a data mode (USB-D, FM-D):
    the radio's USB codec, its LAN stream, the microphone. A packet station
    over USB needs it on USB; over the LAN, on LAN - otherwise the radio is
    keyed and modulated by whatever is on the microphone. `confirmed` says
    whether the item and values come from Icom's own guide for that radio;
    an unconfirmed row is never written (IcomRig.set_data_mod refuses it),
    only shown as what to set by hand.
    """

    item: int
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


#: IC-9700: SET > Connectors > MOD Input > DATA MOD, 1A 05 0116, 00 MIC,
#: 01 ACC, 02 MIC+ACC, 03 USB, 04 MIC+USB, 05 LAN (CI-V Reference Guide p. 7,
#: as openhamshack-nb docs/ic9700-research.md quotes it; NB reads and, under its
#: setup assistant's permit, writes the same item on Brian's and Ray's 9700s).
IC9700_DATA_MOD = DataMod(116, {"mic": 0x00, "acc": 0x01, "mic,acc": 0x02, "usb": 0x03,
                                "mic,usb": 0x04, "lan": 0x05}, True,
                          "IC-9700 CI-V Reference Guide p. 7")


@dataclass(frozen=True)
class DataSetup:
    """What a radio needs set for a data station, and where each setting is.

    The table half of shack_rig.icom.datasetup, which reads, sets and puts
    back each one. A setting with no source for a radio is absent here, and
    the set-up then tells the operator rather than writing anything.

    - `functions`: the 16 xx switches (civ.FUNCTIONS names) this radio is
      known to have; each is switched off for data and put back after.
    - `data_off_mod`: DATA OFF MOD, set to MIC.
    - `mod_level`: {source: 1A 05 item} for the MOD Level of each input.
    - `mod_level_target`: {source: 0-255} to set it to, or nothing for a
      source whose level has not been measured on the air: then it is read
      and reported, not changed (see the IC-9700's row).
    - `af_sql`: {source: 1A 05 item} for the AF SQL of each audio output.
    - `fm_filter`: the filter for FM-D (1-3), or None to keep the radio's.
    - `usb_tx_slips`: whether this radio's USB transmit audio is known to
      slip (the set-up warns and steers to the LAN).
    """

    functions: tuple = ()
    functions_source: str = ""
    data_off_mod: DataMod | None = None
    mod_level: dict = field(default_factory=dict)
    mod_level_target: dict = field(default_factory=dict)
    af_sql: dict = field(default_factory=dict)
    menu_source: str = ""
    fm_filter: int | None = None
    fm_filter_source: str = ""
    usb_tx_slips: bool = False


#: IC-9700, all from Icom's documents:
#:
#: - 16 22/40/41/44/45/46/4A: CI-V Reference Guide p. 5. COMP (16 44) made
#:   no difference to packet on 18/10, and FM has no speech processor, but
#:   it is a switch on the transmit audio path and is put back as found.
#: - DATA OFF MOD 1A 05 0115, ACC/USB/LAN MOD Level 0112/0113/0114, USB and
#:   LAN AF SQL 0107/0111: CI-V Reference Guide p. 7. All confirmed on
#:   Brian's radio answering 1A 05 01 13-16 (18/10/2026).
#: - FM-D on FIL1: Basic Manual p. 4-4, "When you set the IF filter to FIL2
#:   or FIL3 in the FM mode, the transceiver will transmit in the FM narrow
#:   mode". FIL1 is 15 kHz and wide deviation, which is what 1200 baud AFSK
#:   (about 3 kHz peak deviation) is sent with.
#: - MOD level: none to set yet. The radio's deviation limiter sits at about
#:   4.3 kHz (18/10); the level that puts peaks near 3 kHz is to be measured
#:   on a Pluto and an RSPdx into a dummy load (PLAN-0.2 §7, session R2) and
#:   written here. Until then the set-up reads and reports it, and a level
#:   given in the profile is used.
#: - USB transmit audio slips inside the radio, about 1,500 ppm between its
#:   USB codec and its own clock (shack-packet HANDOVER 18/10 ~11:48;
#:   shack_packet.slips): harmless to FT8, fatal to 1200 baud on Brian's
#:   radio. Ray's 9700 sends clean AFSK over USB, so it is a warning.
IC9700_DATA_SETUP = DataSetup(
    functions=("nr", "nb", "an", "comp", "vox", "monitor", "afc", "tone", "tsql", "dtcs"),
    functions_source="IC-9700 CI-V Reference Guide p. 5",
    data_off_mod=DataMod(115, IC9700_DATA_MOD.values, True, "IC-9700 CI-V Reference Guide p. 7"),
    mod_level={"acc": 112, "usb": 113, "lan": 114},
    af_sql={"usb": 107, "lan": 111},
    menu_source="IC-9700 CI-V Reference Guide p. 7",
    fm_filter=1, fm_filter_source="IC-9700 Basic Manual p. 4-4",
    usb_tx_slips=True,
)

#: IC-705: the 16 xx switches by hamlib 4.6's numbers, which are the same on
#: every Icom it drives. No AFC: neither of the 705's manuals on this
#: machine (Basic 9, Advanced 5) mentions one. Its 1A 05 menu items are not sourced here (its CI-V
#: guide is not on this machine), so DATA MOD, DATA OFF MOD, the MOD levels
#: and AF SQL are left to the operator, who is told which.
IC705_DATA_SETUP = DataSetup(
    functions=("nr", "nb", "an", "comp", "vox", "monitor", "tone", "tsql", "dtcs"),
    functions_source="hamlib 4.6 icom_defs.h (S_FUNC_*), not Icom's IC-705 guide",
)


@dataclass(frozen=True, kw_only=True)
class IcomModel(RigModel):
    """A RigModel with the address CI-V frames are sent to.

    A factory default, and settable in the radio's own menu - see the note at
    the top of this file. A frame to the wrong address is ignored, not refused.
    """

    civ_address: int
    maker: str = "icom"
    #: Whether the preamp byte (16 02) also carries the external preamp. On an
    #: IC-9700 it does: 00 none, 01 P.AMP, 02 EXT-P.AMP, 03 both (CI-V
    #: Reference Guide p. 5). On the HF radios the same byte is just the
    #: level - 00, 01 PRE1, 02 PRE2 - and an IC-7610 told 02 meaning
    #: "external" would switch to PRE2.
    preamp_ext: bool = False
    #: Where the radio's own menu says, per band, whether an external preamp
    #: is fitted: (band key, from Hz, to Hz, 1A 05 menu item). Read, never
    #: written - see `IC9700_EXT_PREAMP_MENU`. Empty on a radio without one.
    ext_preamp_menu: tuple = ()
    #: What the spectrum scope's settings may be set to (CI-V 27 14-1D and the
    #: averaging menu item; see civ.py), or None where this module offers no
    #: scope settings for the radio. Spans are full widths in Hz.
    scope: dict | None = field(default=None, hash=False, compare=False)
    #: Where DATA MOD lives in the radio's menu (DataMod), or None where
    #: this library has no source for it: the IC-705's and the HF radios'
    #: item numbers differ from the 9700's and have not been read from a
    #: guide here, so the operator sets them on the radio and the packet
    #: link says which setting to check.
    data_mod: DataMod | None = field(default=None, hash=False, compare=False)
    #: What a data station sets and puts back on this radio (DataSetup),
    #: or None where nothing is sourced.
    data_setup: DataSetup | None = field(default=None, hash=False, compare=False)

    def preamp_mode_is_ext(self, mode: int) -> bool:
        """Whether a 16 02 setting switches the external preamp on."""
        return self.preamp_ext and bool(mode & 0x02)

    def ext_menu_band(self, hz) -> tuple | None:
        """The ext_preamp_menu row for the band `hz` is in, or None."""
        if hz is None:
            return None
        for row in self.ext_preamp_menu:
            if row[1] <= hz <= row[2]:
                return row
        return None

    def preamp_byte(self, level: int, ext: bool = False) -> int:
        """The 16 02 data byte for an internal preamp level (and, on a radio
        that has one in the same byte, the external preamp's state)."""
        return level | (0x02 if self.preamp_ext and ext else 0)

    def preamp_from_byte(self, b: int) -> tuple[int, bool | None]:
        """(level, external preamp on) from a 16 02 answer; None where this
        radio's byte does not say."""
        if self.preamp_ext:
            return b & 0x01, bool(b & 0x02)
        return b, None

    def public(self) -> dict:
        got = super().public()
        got["civ_address"] = f"0x{self.civ_address:02X}"
        got["scope"] = None if self.scope is None else {
            k: (dict(v) if isinstance(v, dict) else v if isinstance(v, int) else list(v))
            for k, v in self.scope.items()}
        return got


#: The name this table used in the Icom app, kept so code written against it
#: (and the adoption in docs/RIG.md) reads unchanged.
Model = IcomModel


VHF_UHF_MODES = ["LSB", "USB", "AM", "CW", "RTTY", "FM", "CWR", "RTTYR"]

#: What an IC-9700 tunes: the widest of its versions, from the "Frequency
#: range" table in Icom's IC-9700 service manual, section 1 (specifications),
#: which gives one RX/TX figure per version:
#:
#:   [USA], [EXP]  144-148 MHz, 430-450 MHz, 1240-1300 MHz
#:   [EUR]         144-146 MHz, 430-440 MHz, 1240-1300 MHz
#:   [ITR], [TPE], [KOR]  narrower again on 70 cm and 23 cm
#:
#: The widest, not the European one, because which version is on the bench is
#: not something CI-V reports, and the two ways of being wrong are not equal.
#: Too narrow refuses a frequency the radio would have tuned - Brian's 9700
#: sat on 146.100 FM, put there from its own front panel, while this module
#: refused to step it up (10/10/2026). Too wide sends a frequency a stricter
#: version answers NG to, and the radio stays where it was, which the next
#: frequency poll shows. Transmit is a separate question with a separate
#: answer: `tx_ranges` below and the licence check behind the lease.
#: The receive front end, per radio: preamp levels and attenuator steps.
#:
#: Both are the same two commands on every Icom here, and only the values
#: differ - which is why they are columns rather than code:
#:
#:   16 02 <n>   send/read the preamp
#:   11 <dB>     send/read the attenuator, the step as BCD dB (10 dB is 0x10)
#:
#: IC-9700: Icom's IC-9700 CI-V Reference Guide, command table - "11 00
#: Send/read attenuator OFF setting, 10 Send/read 10 dB attenuator setting"
#: (p. 4) and "16 02 00 to 03 Send/read the Preamp/External Preamp" (p. 5),
#: both marked * (send and read). One preamp and one 10 dB step, each
#: remembered per band by the radio itself ("Each band memorizes the
#: Preamplifier setting" / "...the Attenuator setting", IC-9700 Basic Manual
#: p. 4-1). Neither document limits either one to some bands, so 23 cm gets
#: both like 2 m and 70 cm; a band where the radio refused would answer NG and
#: the next read would show it unchanged.
#:
#: IC-7610, IC-705, IC-7600: from wfview's rig files (rigs/IC-7610.rig etc.,
#: which carry the same 11 and 16 02 strings and the value lists below), not
#: from Icom's own guides, which are not on this machine. Treat them as
#: documented-by-prior-art rather than checked, like the scope commands in
#: civ.py. The IC-705's preamps are PRE1/PRE2 on HF and 6 m; on 2 m and 70 cm
#: it has one, and a PRE2 asked for there is the radio's to refuse.
IC9700_PREAMPS = ("P.AMP",)
IC9700_ATTENUATORS = (10,)

#: The IC-9700's preamp control as whole settings, index = the 16 02 byte
#: (CI-V guide p. 5: 00 both off, 01 P.AMP, 02 EXT-P.AMP, 03 both). Brian's
#: report that "preamp only works with the internal pre amp" (10/10/2026) was
#: the level list above, which has no way to say EXT at all.
IC9700_PREAMP_MODES = ("OFF", "P.AMP", "EXT", "P.AMP+EXT")

#: SET > Connectors > External P.AMP > 144M / 430M / 1200M, 1A 05 0093-0095,
#: 00 OFF / 01 ON (CI-V guide, 1A 05 table). The radio uses it to decide
#: whether a mast-head preamp is fitted on that band, which is why it is the
#: gate for offering EXT. It is READ ONLY here, and deliberately there is no
#: builder for writing it in civ.py: on the real radio that setting decides
#: whether DC goes up the coax to a mast-head preamp, and a stray write could
#: feed 13.8 V into an antenna or a preamp that is not expecting it. The
#: ranges are the radio's coverage bands (IC9700_COVERAGE below).
IC9700_EXT_PREAMP_MENU = (
    ("144", 144_000_000, 148_000_000, 93),
    ("430", 430_000_000, 450_000_000, 94),
    ("1200", 1_240_000_000, 1_300_000_000, 95),
)

#: The IC-9700's scope settings, from its CI-V Reference Guide p. 24-25:
#: centre-mode spans +-2.5, 5, 10, 25, 50, 100, 250 and 500 kHz (given here
#: as full widths, which is what a client draws), three fixed-mode edge sets,
#: a reference level of -20.0 to +20.0 dB in 0.5 dB steps, three sweep
#: speeds, two VBWs, and SCOPE > Averaging off/2/3/4.
IC9700_SCOPE = {
    "spans_hz": (5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000, 1_000_000),
    "modes": ("center", "fixed"),
    "edges": 3,
    "ref_db": {"min": -20.0, "max": 20.0, "step": 0.5},
    "speeds": ("fast", "mid", "slow"),
    "vbw": ("narrow", "wide"),
    "averaging": (0, 2, 3, 4),
}

#: The IC-7610 and IC-705 have the same 27 commands and spans by wfview's
#: rig files, and four fixed edge sets; not checked against a guide or a
#: radio. A setting one of them refuses is answered NG, and the next read
#: shows it unchanged.
HF_SCOPE = {**IC9700_SCOPE, "edges": 4}

IC9700_COVERAGE = [
    (144_000_000, 148_000_000),
    (430_000_000, 450_000_000),
    (1_240_000_000, 1_300_000_000),
]

MODELS: dict[str, IcomModel] = {}


def _add(model: IcomModel) -> None:
    MODELS[model.key] = model


_add(Model(
    key="ic-9700", name="IC-9700", civ_address=0xA2,
    modes=VHF_UHF_MODES + ["DV", "DD"],
    rx_ranges=IC9700_COVERAGE,
    tx_ranges=[TWO, SEVENTY_CM, TWENTYTHREE_CM],
    tx_power={
        TWO[0]: (2_000, 100_000),
        SEVENTY_CM[0]: (2_000, 75_000),
        TWENTYTHREE_CM[0]: (1_000, 10_000),
    },
    preamps=IC9700_PREAMPS, attenuators=IC9700_ATTENUATORS, preamp_ext=True,
    preamp_modes=IC9700_PREAMP_MODES, ext_preamp_menu=IC9700_EXT_PREAMP_MENU,
    scope=IC9700_SCOPE, data_mod=IC9700_DATA_MOD, data_setup=IC9700_DATA_SETUP,
    note="VHF/UHF only. The one radio here with DD, which hamlib has no name "
         "for - it is passed through as \"DD\" and a hamlib client will not "
         "recognise it.",
))

_add(Model(
    key="ic-7600", name="IC-7600", civ_address=0x7A,
    modes=VHF_UHF_MODES,
    rx_ranges=[(30_000, 60_000_000)],
    tx_ranges=HF + [SIX],
    tx_power={lo: (5_000, 100_000) for lo, _ in HF + [SIX]},
    preamps=("P.AMP1", "P.AMP2"), attenuators=(6, 12, 18),
    note="HF and 6 m, 100 W. **Not checked against hardware.** Added for a "
         "radio at another location, from its manual - so the CI-V address, "
         "the mode list and the power figures are as documented rather than "
         "as measured, unlike the three above. It is here because everything "
         "else about it is the same CI-V as the rest, which is exactly what "
         "this file being a table rather than a class hierarchy is for.",
))

_add(Model(
    key="ic-7610", name="IC-7610", civ_address=0x98,
    modes=VHF_UHF_MODES + ["PSK", "PSKR"],
    # The receiver is continuous from 30 kHz; the amateur bands are what it
    # will be told to tune.
    rx_ranges=[(30_000, 60_000_000)],
    tx_ranges=HF + [SIX],
    tx_power={lo: (1_000, 100_000) for lo, _ in HF + [SIX]},
    preamps=("P.AMP1", "P.AMP2"), attenuators=tuple(range(3, 46, 3)),
    scope=HF_SCOPE,
    note="HF and 6 m, 100 W. Two independent receivers; this module drives "
         "the main one, because CI-V addresses the sub-receiver as a separate "
         "selection rather than a separate VFO.",
))

_add(Model(
    key="ic-705", name="IC-705", civ_address=0xA4,
    modes=VHF_UHF_MODES + ["WFM", "DV"],
    rx_ranges=[(30_000, 199_999_999), (400_000_000, 470_000_000)],
    tx_ranges=HF + [SIX, FOUR, TWO, SEVENTY_CM],
    # 10 W on external supply, 5 W on the internal battery - and the radio
    # does not say which it is on, so the figure here is the external one.
    tx_power={lo: (100, 10_000) for lo, _ in HF + [SIX, FOUR, TWO, SEVENTY_CM]},
    preamps=("P.AMP1", "P.AMP2"), attenuators=(20,), scope=HF_SCOPE, data_setup=IC705_DATA_SETUP,
    note="HF through 70 cm at 10 W, or 5 W on its battery - the radio does "
         "not report which supply it is on, so 10 W is assumed. Connects over "
         "WiFi rather than Ethernet, so it is worth a fixed DHCP reservation.",
))

DEFAULT_MODEL = "ic-9700"



def model(key: str) -> IcomModel:
    got = MODELS.get((key or "").strip().lower())
    if got is None:
        raise ValueError(_("unknown radio {key}; this module knows {known}").format(
            key=repr(key), known=", ".join(sorted(MODELS))))
    return got


def listing() -> list[dict]:
    return [m.public() for m in MODELS.values()]
