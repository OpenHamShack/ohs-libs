"""A Yaesu's data set-up: the settings shack_rig.datasetup reads, sets and
puts back, from cat.py's narrow builders and the model's rows.

    setup = DataSetup(settings(rig, frequency=144_800_000), Journal(path))

YaesuRig answers on its own reader thread and `ask` blocks until it has,
so these readers and writers are plain calls (the Icom's go through its
loop). In order, FTX-1 CAT manual 2508-C pages as printed:

| key | what | wanted |
|---|---|---|
| frequency | FA (p. 16) | the caller's |
| mode | MD0 (p. 19) | DATA-FM (MD0A), wide: 1200 baud AFSK is sent at about 3 kHz deviation |
| shift | OS0 (p. 21; FM modes only, so after the mode) | simplex |
| tones | CT0 (p. 9) | off |
| an, nr, nb | BC0, RL0, NL0 (pp. 7, 23, 21) | off (level 00/000) |
| vox, monitor, comp | VX, ML0, PR0 (pp. 26, 19, 22) | off |
| mod_source_data, mod_source_fm | EX 01 04 13, EX 01 03 12 (pp. 11, 10) | USB |
| mod_gain_data, mod_gain_fm | EX 01 04 14, EX 01 03 13 (pp. 11, 10) | the caller's level, else read only |

Both MOD SOURCE and both USB MOD GAIN rows, because the manual does not say
which block governs DATA-FM (models.py): whichever one the radio reads,
it reads the same. Unlike the Icom's 0F, the FTX-1 reports its shift, so
simplex is put back too.
"""

from __future__ import annotations

from ..datasetup import Setting
from ..i18n import _
from . import cat


def _onoff(on) -> str:
    return "on" if on else "off"


def _level(v) -> str:
    return "off" if not v else f"on ({v})"


def settings(rig, *, frequency: int | None = None, mod_gain: int | None = None) -> list[Setting]:
    """The data set-up for `rig` (a YaesuRig), in order. `mod_gain`
    (0-100) sets USB MOD GAIN; without it the gain is only read."""
    model = rig.model

    def ask_value(command: str, prefix: str):
        got = rig.ask(command, prefix)
        return None if got is None else cat.parse_value(got, prefix)

    out: list[Setting] = []
    if frequency:
        def read_freq():
            got = rig.ask(cat.read_freq(), "FA")
            return None if got is None else cat.parse_freq(got)
        out.append(Setting("frequency", int(frequency), read_freq, lambda hz: rig.send(cat.set_freq(int(hz))),
                           show=lambda hz: f"{hz / 1e6:.4f} MHz"))

    def read_mode():
        got = rig.ask(cat.read_mode(), "MD0")
        return None if got is None else cat.parse_mode(got)[1]

    out.append(Setting("mode", cat.code_for("FM", True, False), read_mode,
                       lambda code: rig.send(cat.set_mode(str(code))),
                       show=lambda code: cat.MODE_CODES.get(code, code)))
    out.append(Setting("shift", 0, lambda: ask_value(cat.read_shift(), "OS0"),
                       lambda v: rig.send(cat.set_shift(int(v))),
                       show=lambda v: {0: "simplex", 1: "+", 2: "-", 3: "ARS"}.get(v, str(v))))
    out.append(Setting("tones", 0, lambda: ask_value(cat.read_tones(), "CT0"),
                       lambda v: rig.send(cat.set_tones(int(v))),
                       show=lambda v: {0: "off", 1: "CTCSS encode", 2: "CTCSS", 3: "DCS"}.get(v, str(v))))

    if model.data_switches:
        out += [
            Setting("an", 0, lambda: ask_value(cat.read_auto_notch(), "BC0"),
                    lambda v: rig.send(cat.set_auto_notch(bool(v))), show=_onoff),
            Setting("nr", 0, lambda: ask_value(cat.read_noise_reduction(), "RL0"),
                    lambda v: rig.send(cat.set_noise_reduction(int(v))), show=_level),
            Setting("nb", 0, lambda: ask_value(cat.read_noise_blanker(), "NL0"),
                    lambda v: rig.send(cat.set_noise_blanker(int(v))), show=_level),
            Setting("vox", 0, lambda: ask_value(cat.read_vox(), "VX"),
                    lambda v: rig.send(cat.set_vox(bool(v))), show=_onoff),
            Setting("monitor", 0, lambda: ask_value(cat.read_monitor(), "ML0"),
                    lambda v: rig.send(cat.set_monitor(bool(v))), show=_onoff),
            # PR0's values are 1 OFF and 2 ON (p. 22), kept raw.
            Setting("comp", 1, lambda: ask_value(cat.read_processor(), "PR0"),
                    lambda v: rig.send(cat.set_processor(int(v) == 2)), show=lambda v: _onoff(v == 2)),
        ]
    else:
        out.append(Setting("switches", note=_("switch the {radio}'s noise reduction, noise blanker, notch, "
                                              "VOX and monitor off on the radio").format(radio=model.name)))

    def menu(key: str, item, want, show):
        prefix = f"EX{item.p1:02d}{item.p2:02d}{item.p3:02d}"

        def read():
            got = rig.ask(cat.read_menu(item.p1, item.p2, item.p3), prefix)
            if got is None:
                return None
            value = cat.parse_menu(got)[1]
            return int(value) if value.isdigit() else None

        def write(v) -> bool:
            return rig.send(cat.set_menu(item.p1, item.p2, item.p3, int(v), item.digits))
        return Setting(key, want, read, write, show=show)

    if model.mod_source:
        for block, item in model.mod_source.items():
            if item.confirmed:
                out.append(menu(f"mod_source_{block}", item, item.value("usb"),
                                lambda v, item=item: (item.name(v) or str(v)).upper()))
    else:
        out.append(Setting("mod_source", note=_("set the {radio}'s DATA modulation source to USB on the "
                                                "radio").format(radio=model.name)))
    for block, item in model.mod_gain.items():
        if item.confirmed:
            out.append(menu(f"mod_gain_{block}", item, mod_gain, lambda v: f"{v}"))
    return out
