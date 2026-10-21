"""A TM-V71's data set-up: the settings shack_rig.datasetup reads, sets and
puts back, built on cat.py and KenwoodRig.

    setup = DataSetup(settings(rig, frequency=144_800_000, speed=1200), Journal(path))

In order (restored in reverse, so the band is back in memory mode last):

| key | what | wanted |
|---|---|---|
| vfo_mode | VM on the rig's band | VFO (FO writes the VFO; a memory channel is left untouched) |
| frequency | FO P2 | the caller's |
| mode | FO P13 | FM (wide: 9600 G3RUH needs it, and 1200 AFSK is sent at FM's deviation) |
| shift, reverse | FO P4, P5 | simplex, normal |
| tones | FO P6-P8 (tone, CTCSS, DCS) | all off |
| data_band | MU P38, menu 517 EXT. DATA BAND | the rig's band, A or B (not crossband) |
| data_speed | MU P39, menu 518 EXT. DATA SPEED | 1200 or 9600, the modem's |
| sqc | MU P40, menu 520 SQC SOURCE | read and reported only: the app reads busy over the PC port |
| power | PC | read and reported only; the control strip changes it |
| squelch | SQ | read and reported only |

**EXT. DATA SPEED is not only a name.** It sets the PKD input's
sensitivity - 40 mVp-p at 1200 and 2 Vp-p at 9600 (cat.py) - so a station
on 1200 with the menu at 9600 transmits at a fiftieth of the deviation it
was set up for, and one on 9600 with the menu at 1200 overdrives into the
limiter. That is why it is set, not reported.

**The menu is written whole** (cat.Menu): the radio's own MU answer with
one field changed, believed only when the echo matches it field for
field. A radio whose menu answer is not the 42 fields hamlib reads is not
written at all; the line is "manual", with the menu number to set by hand.

**The band** is chosen before any of this (`choose_band`): the rig's own if
its segment covers the frequency, else the other band if it does; else
nothing is tuned and the report says to press BAND - the TM-V71 cannot be
moved between VHF and UHF over its PC port (models.py).
"""

from __future__ import annotations

from ..datasetup import UNAVAILABLE, Setting
from ..i18n import _
from . import cat

SPEED_CODES = {1200: 0, 9600: 1}


def choose_band(rig, frequency: int | None) -> int | None:
    """The band to run packet on for `frequency` (module docstring); the
    rig's current band when no frequency is given; None if neither band
    can be tuned there."""
    if not frequency:
        return rig.band
    return rig.band_for(int(frequency))


def settings(rig, *, frequency: int | None = None, speed: int = 1200) -> list[Setting]:
    """The data set-up for `rig` (a KenwoodRig already on its chosen band),
    in order. `speed` is the modem's: 1200 or 9600."""
    if speed not in SPEED_CODES:
        raise ValueError(f"the TM-V71's DATA jack runs at 1200 or 9600, not {speed!r}")
    band = rig.band
    name = "AB"[band]
    out: list[Setting] = []

    def read_vm():
        got = rig.ask(cat.read_band_mode(band))
        if not got or not got.startswith("VM "):
            return None
        return cat.parse_band_mode(got)[1]

    def write_vm(mode) -> bool:
        got = rig.ask(cat.set_band_mode(band, int(mode)))
        return bool(got) and got.startswith("VM ")

    out.append(Setting("vfo_mode", 0, read_vm, write_vm, show=lambda m: cat.BAND_MODES.get(m, str(m))))

    def fo_field(key: str, field: str, want, show):
        def read():
            vfo = rig.read_vfo(band)
            return None if vfo is None else vfo.number(field)

        def write(v) -> bool:
            vfo = rig.read_vfo(band)
            return vfo is not None and rig.write_vfo(vfo.replace(**{field: int(v)})) is not None
        return Setting(key, want, read, write, show=show)

    if frequency:
        out.append(fo_field("frequency", "freq", int(frequency), lambda hz: f"{hz / 1e6:.4f} MHz"))
    out.append(fo_field("mode", "mode", 0, lambda m: cat.MODES.get(m, str(m))))
    out.append(fo_field("shift", "shift", 0, lambda v: {0: "simplex", 1: "+", 2: "-"}.get(v, str(v))))
    out.append(fo_field("reverse", "reverse", 0, lambda v: "on" if v else "off"))

    def read_tones():
        vfo = rig.read_vfo(band)
        return None if vfo is None else vfo.tones

    def write_tones(v) -> bool:
        vfo = rig.read_vfo(band)
        if vfo is None:
            return False
        tone, ctcss, dcs = (int(x) for x in v)
        return rig.write_vfo(vfo.replace(tone=tone, ctcss=ctcss, dcs=dcs)) is not None

    def show_tones(v) -> str:
        on = [n for n, x in zip(("tone", "CTCSS", "DCS"), v) if x]
        return ", ".join(on) if on else "off"
    out.append(Setting("tones", [0, 0, 0], read_tones, write_tones, show=show_tones))

    def menu_field(key: str, index: int, want, show, menu_no: int, by_hand: str):
        def read():
            menu = rig.read_menu()
            if menu is None:
                return None
            if not menu.complete:
                return UNAVAILABLE
            return menu.value(index)

        def write(v) -> bool:
            menu = rig.read_menu()
            if menu is None or not menu.complete:
                return False
            return rig.write_menu(menu.replace(index, int(v))) is not None
        probe = rig.read_menu()
        if probe is not None and not probe.complete:
            # Not the menu hamlib knows: nothing is written, the operator is told.
            return Setting(key, note=_("menu {n}: {what}").format(n=menu_no, what=by_hand))
        return Setting(key, want, read, write, show=show)

    out.append(menu_field("data_band", cat.MU_DATA_BAND, band, lambda v: cat.DATA_BANDS.get(v, str(v)), 517,
                          _("set EXT. DATA BAND to {band} on the radio").format(band=name)))
    out.append(menu_field("data_speed", cat.MU_DATA_SPEED, SPEED_CODES[speed],
                          lambda v: f"{cat.DATA_SPEEDS.get(v, v)} bps", 518,
                          _("set EXT. DATA SPEED to {speed} bps on the radio").format(speed=speed)))
    out.append(menu_field("sqc", cat.MU_SQC, None, lambda v: cat.SQC_SOURCES.get(v, str(v)), 520,
                          _("SQC SOURCE is not used: the app reads the squelch over the PC port")))

    def read_power():
        level = rig.read_power()
        return level

    out.append(Setting("power", None, read_power, None, show=lambda v: cat.POWER_LEVELS.get(v, str(v))))
    out.append(Setting("squelch", None, rig.read_squelch, None, show=lambda v: f"{v:02X}"))
    return out


def power_setting(rig) -> Setting:
    """RF power as the control strip turns it: 0 H, 1 M, 2 L, through
    DataSetup.adjust (saved before the first change, put back at close)."""
    return Setting("power_level", None, rig.read_power, rig.set_power,
                   show=lambda v: cat.POWER_LEVELS.get(v, str(v)))
