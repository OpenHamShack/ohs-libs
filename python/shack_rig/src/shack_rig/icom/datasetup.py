"""An Icom's data set-up: the settings shack_rig.datasetup reads, sets and
puts back, from the model's DataSetup table (models.py) and civ.py's
narrow builders.

    from shack_rig.datasetup import DataSetup, Journal
    from shack_rig.icom.datasetup import settings
    setup = DataSetup(settings(rig, call, source="lan", frequency=144_800_000), Journal(path))
    setup.recover(); setup.apply(); ...; setup.restore()

`call(fn, *args)` runs `fn` on the rig's event loop and returns its result
(awaiting it if it is a coroutine): IcomRig and its streams live on that
loop and are not thread-safe. The packet link's `_call` is exactly this.

**In order**, because the order is the safety (datasetup.py rule 4):

| key | what | wanted | source |
|---|---|---|---|
| frequency | 03 / 05 | the caller's | CI-V guide p. 4 |
| mode | 26 00: mode, data flag, filter | FM, data on, the model's FM-D filter (or the radio's) | p. 12; filter Basic Manual p. 4-4 |
| simplex | 0F (read), 0F 10 / 11 / 12 | simplex, only from a DUP- or DUP+ that 0F reported; left alone while split is on | p. 4; hamlib S_DUP_OFF |
| tone, tsql, dtcs | 16 42 / 43 / 4B | off | p. 5 |
| nr, nb, an | 16 40 / 22 / 41 | off | p. 5 |
| comp, vox, monitor, afc | 16 44 / 46 / 45 / 4A | off | p. 5 |
| data_mod | 1A 05 0116 | the transport's input (LAN, USB) | p. 7 |
| data_off_mod | 1A 05 0115 | MIC | p. 7 |
| mod_level | 1A 05 0112/0113/0114 for the transport | the caller's level, else the model's measured one, else read only | p. 7 |
| af_sql | 1A 05 0107 (USB) / 0111 (LAN) | off | p. 7 |
| squelch | 14 03 | read only, with a warning when fully open | p. 4 |

Page numbers are the IC-9700 CI-V Reference Guide's. A radio whose table
leaves an item out gets a "set it on the radio" line instead (the IC-705's
menu items), and a 16 xx switch it does not have is answered NG and
reported as not on this radio.
"""

from __future__ import annotations

from typing import Any, Callable

from ..datasetup import UNAVAILABLE, Setting
from ..i18n import _
from . import civ

#: How long a set-up read waits for its answer. A LAN round trip is a few
#: milliseconds; a USB one waits its turn behind the poll's reads.
ASK_S = 1.0

#: How long a second asking waits, after an NG, before the setting is taken
#: as one the radio does not have.
NG_CONFIRM_S = 0.5

#: civ's FILTERS, as numbers.
FILTER_NUMBERS = {"FIL1": 1, "FIL2": 2, "FIL3": 3}


def _percent(level: int) -> str:
    return f"{round(level * 100 / 255)}%"


def _onoff(on: bool) -> str:
    return "on" if on else "off"


def mod_level_from_percent(percent: float) -> int:
    """A MOD level as the radio stores it (0-255) from a percentage."""
    return max(0, min(255, round(float(percent) * 255 / 100)))


def settings(rig, call: Callable[..., Any], *, source: str = "lan", frequency: int | None = None,
             mod_level: int | None = None, simplex: bool = True) -> list[Setting]:
    """The data set-up for `rig` (an IcomRig), in order. `source` is where
    transmit audio comes from: "lan" or "usb". `mod_level` (0-255) sets
    that input's MOD Level; without it the model's measured level is used,
    or the level is only read."""
    model = rig.model
    table = getattr(model, "data_setup", None)
    name = model.name if model is not None else _("this radio")

    def ask(command: bytes, key: bytes, match=None):
        got = call(lambda: rig.ask(command, key, ASK_S, match=match, refused=True), timeout=ASK_S + 2)
        if got != bytes([civ.NG]):
            return got
        # An NG names no command, and the poll's own questions get NGs too
        # (a radio in standby, a meter it does not have): on a USB cable one
        # of those arrives often enough while this waits (seen in the
        # tests). So an NG is only a hint; asked again, a radio that has the
        # setting answers it, and one that does not stays silent.
        got = call(lambda: rig.ask(command, key, NG_CONFIRM_S, match=match), timeout=NG_CONFIRM_S + 2)
        return UNAVAILABLE if got is None else got

    def send(command: bytes) -> bool:
        return bool(call(rig.send, command))

    def menu_reader(item: int, level: bool = False):
        def read():
            got = ask(civ.read_menu(item), b"\x1a\x05", match=lambda p: civ.decode_menu(p)[0] == item)
            if got is None or got == UNAVAILABLE:
                return got
            return civ.decode_menu_level(got)[1] if level else civ.decode_menu(got)[1]
        return read

    out: list[Setting] = []

    if frequency:
        def read_freq():
            got = ask(civ.read_freq(), b"\x03")
            return got if got in (None, UNAVAILABLE) else civ.decode_freq(got[1:6])
        out.append(Setting("frequency", int(frequency), read_freq,
                           lambda hz: bool(call(rig.set_freq, int(hz))),
                           show=lambda hz: f"{hz / 1e6:.4f} MHz"))

    def read_mode():
        got = ask(civ.read_mode_data(), b"\x26")
        if got in (None, UNAVAILABLE):
            return got
        m = civ.parse_mode_data(got)
        return [got[2], 1 if m["data"] else 0, m["filter"]]

    def want_mode(before):
        filt = table.fm_filter if table is not None and table.fm_filter else (before[2] if before else 1)
        return [civ.MODE_CODES["FM"], 1, filt]

    def write_mode(raw) -> bool:
        mode = civ.MODES.get(raw[0])
        if mode is None:
            return False
        return bool(call(lambda: rig.set_mode(mode, int(raw[2]), bool(raw[1]))))

    def show_mode(raw) -> str:
        return f"{civ.mode_label(civ.MODES.get(raw[0], '?'), bool(raw[1]))} FIL{raw[2]}"

    out.append(Setting("mode", want_mode, read_mode, write_mode, show=show_mode))

    if simplex:
        # Duplex is changed only when it can be put back (Brian, 18/10/2026,
        # after the 0.2.0 radio session). This used to send 0F 10 blind as
        # "set-unrestorable", on the belief that 0F reads only the split
        # state; the guide says a bare 0F answers the duplex direction too
        # (civ.DUPLEX_READS), so a DUP- or DUP+ is read, saved, set to
        # simplex and restored like any other setting. While split is on
        # the answer says nothing about the duplex: then nothing is
        # written, and the report asks the operator to check.
        def read_duplex():
            got = ask(civ.read_split(), b"\x0f")
            return got if got in (None, UNAVAILABLE) else civ.decode_duplex(got)

        def want_duplex(before):
            return 0x00 if before == 0x00 or before in civ.DUPLEX_SHIFTS else None

        def warn_duplex(before):
            if before == 0x00 or before in civ.DUPLEX_SHIFTS:
                return None
            return _("split is on, so the {radio} does not say whether it is on a repeater shift; make sure "
                     "it is on simplex").format(radio=name)

        out.append(Setting("simplex", want_duplex, read_duplex,
                           lambda code: send(civ.set_duplex(int(code))),
                           show=lambda code: civ.DUPLEX_READS.get(code, f"0x{code:02x}"), warn=warn_duplex))

    functions = table.functions if table is not None else tuple(civ.TONE_FUNCTIONS)
    order = ("tone", "tsql", "dtcs", "nr", "nb", "an", "comp", "vox", "monitor", "afc")
    for fn in [f for f in order if f in functions]:
        sub = civ.FUNCTIONS[fn]

        def read_fn(sub=sub, fn=fn):
            got = ask(civ.read_function(fn), bytes([0x16, sub]))
            return got if got in (None, UNAVAILABLE) else civ.decode_function(got)[1]

        def write_fn(on, fn=fn) -> bool:
            return send(civ.FUNCTION_WRITERS[fn](bool(on)))

        out.append(Setting(fn, False, read_fn, write_fn, show=_onoff))

    data_mod = getattr(model, "data_mod", None)
    if data_mod is not None and data_mod.confirmed and data_mod.value(source) is not None:
        out.append(Setting("data_mod", data_mod.value(source), menu_reader(data_mod.item),
                           lambda v: send(civ.set_data_mod(data_mod.item, int(v))),
                           show=lambda v: (data_mod.name(v) or f"0x{v:02x}").upper()))
    else:
        out.append(Setting("data_mod", note=_("set DATA MOD to {source} on the {radio} (SET > Connectors > "
                                              "MOD Input)").format(source=source.upper(), radio=name)))

    off_mod = table.data_off_mod if table is not None else None
    if off_mod is not None and off_mod.confirmed:
        out.append(Setting("data_off_mod", off_mod.value("mic"), menu_reader(off_mod.item),
                           lambda v: send(civ.set_data_off_mod(off_mod.item, int(v))),
                           show=lambda v: (off_mod.name(v) or f"0x{v:02x}").upper()))
    else:
        out.append(Setting("data_off_mod", note=_("set DATA OFF MOD to MIC on the {radio} (SET > Connectors > "
                                                  "MOD Input)").format(radio=name)))

    level_item = table.mod_level.get(source) if table is not None else None
    if level_item is not None:
        target = mod_level if mod_level is not None else table.mod_level_target.get(source)
        out.append(Setting("mod_level", target, menu_reader(level_item, level=True),
                           lambda v: send(civ.set_mod_level(level_item, int(v))), show=_percent))
    else:
        out.append(Setting("mod_level", note=_("check the {source} MOD Level on the {radio} (SET > Connectors > "
                                               "MOD Input): peaks of about 3 kHz deviation, below its "
                                               "limiter").format(source=source.upper(), radio=name)))

    sql_item = table.af_sql.get(source) if table is not None else None
    if sql_item is not None:
        out.append(Setting("af_sql", False, menu_reader(sql_item),
                           lambda v: send(civ.set_af_sql(sql_item, bool(v))), show=_onoff))

    if table is not None:
        def read_squelch():
            got = ask(civ.read_squelch_level(), b"\x14\x03")
            return got if got in (None, UNAVAILABLE) else civ.decode_squelch_level(got)

        def warn_squelch(level):
            if level == 0:
                return _("the squelch is fully open on this band, so the radio reports the channel busy all "
                         "the time and nothing will be sent; close it until the noise stops")
            return None

        out.append(Setting("squelch", None, read_squelch, None, show=_percent, warn=warn_squelch))
    return out


def warnings(model, transport: str, transmit: bool) -> list[tuple[str, str]]:
    """What to tell the operator about this radio on this transport,
    beyond the settings: [(key, sentence)]."""
    table = getattr(model, "data_setup", None)
    got = []
    if transmit and transport == "usb" and table is not None and table.usb_tx_slips:
        got.append(("usb-tx-slips", _(
            "the {radio}'s own USB sound device drops and repeats samples when it transmits (about 1,500 "
            "parts per million, measured on 18/10/2026): harmless to FT8 but enough to stop 1200 baud packet "
            "decoding on some radios. Use the network (LAN) connection to transmit packet if you "
            "can").format(radio=model.name)))
    return got
