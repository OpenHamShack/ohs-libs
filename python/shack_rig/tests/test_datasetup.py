"""A radio set up for data and put back: the engine's order of things
(shack_rig.datasetup) on a radio made of a dict, then an Icom's settings
(shack_rig.icom.datasetup) on FakeIcom - the whole LAN path, the radio
keeping what it is told. Nothing leaves loopback; nothing is keyed.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from helpers import OnFake
from shack_rig.datasetup import UNAVAILABLE, DataSetup, Journal, Setting, journal_name
from shack_rig.icom import civ, models
from shack_rig.icom.datasetup import mod_level_from_percent, settings, warnings


# -- the engine, on a radio made of a dict ----------------------------------


class DictRadio:
    """Values by key; `deaf` keys never answer, `missing` ones say so, and
    `stubborn` ones acknowledge a write and keep their value."""

    def __init__(self, **values):
        self.values = dict(values)
        self.deaf: set[str] = set()
        self.missing: set[str] = set()
        self.stubborn: set[str] = set()
        self.writes: list[tuple[str, object]] = []
        self.on_write = None

    def setting(self, key, want, **kw) -> Setting:
        def read():
            if key in self.missing:
                return UNAVAILABLE
            return None if key in self.deaf else self.values.get(key)

        def write(v):
            if self.on_write:
                self.on_write(key, v)
            self.writes.append((key, v))
            if key not in self.stubborn:
                self.values[key] = v
            return True
        return Setting(key, want, read, write, **kw)


def test_set_in_order_and_put_back_last_first(tmp_path):
    radio = DictRadio(freq=145_500_000, nr=True, vox=False, level=128)
    order = []
    radio.on_write = lambda k, v: order.append(k)
    setup = DataSetup([radio.setting("freq", 144_800_000), radio.setting("nr", False),
                       radio.setting("vox", False), radio.setting("level", None)],
                      Journal(tmp_path / "r.json"))
    report = {line.key: line.status for line in setup.apply()}
    assert report == {"freq": "set", "nr": "set", "vox": "already", "level": "report"}
    assert radio.values == {"freq": 144_800_000, "nr": False, "vox": False, "level": 128}
    assert order == ["freq", "nr"], "only what needed changing, in order"
    order.clear()
    lines = setup.restore()
    assert [(line.key, line.status) for line in lines] == [("nr", "restored"), ("freq", "restored")]
    assert order == ["nr", "freq"], "the frequency last, so a per-band setting goes back on its own band"
    assert radio.values == {"freq": 145_500_000, "nr": True, "vox": False, "level": 128}
    assert not (tmp_path / "r.json").exists(), "nothing left to put back, no journal"


def test_each_value_is_on_disk_before_its_setting_is_touched(tmp_path):
    radio = DictRadio(a=1, b=2)
    journal = Journal(tmp_path / "r.json")
    seen = {}
    radio.on_write = lambda k, v: seen.setdefault(k, dict(journal.load()))
    DataSetup([radio.setting("a", 10), radio.setting("b", 20)], journal).apply()
    assert seen["a"] == {"a": 1}
    assert seen["b"] == {"a": 1, "b": 2}


def test_what_cannot_be_read_is_never_written(tmp_path):
    radio = DictRadio(a=1, b=2, c=3)
    radio.deaf.add("a")
    radio.missing.add("b")
    setup = DataSetup([radio.setting("a", 0), radio.setting("b", 0), radio.setting("c", 0)])
    assert [line.status for line in setup.apply()] == ["unread", "unavailable", "set"]
    assert [k for k, _v in radio.writes] == ["c"]


def test_a_write_the_radio_does_not_show_is_failed_and_still_put_back(tmp_path):
    radio = DictRadio(a=1)
    radio.stubborn.add("a")
    setup = DataSetup([radio.setting("a", 5)], Journal(tmp_path / "r.json"))
    assert setup.apply()[0].status == "failed"
    radio.stubborn.clear()
    radio.values["a"] = 5                                 # say it took after all
    assert setup.restore()[0].status == "restored"
    assert radio.values["a"] == 1


def test_a_session_that_died_is_put_back_by_the_next(tmp_path):
    path = tmp_path / "r.json"
    radio = DictRadio(freq=145_500_000, nr=True)
    first = DataSetup([radio.setting("freq", 144_800_000), radio.setting("nr", False)], Journal(path))
    first.apply()
    # The process dies here: no restore. The journal is what survives.
    assert json.loads(path.read_text())["saved"] == [["freq", 145_500_000], ["nr", True]]
    radio.writes.clear()
    second = DataSetup([radio.setting("freq", 144_800_000), radio.setting("nr", False)], Journal(path))
    lines = second.recover()
    assert [(line.key, line.status) for line in lines] == [("nr", "recovered"), ("freq", "recovered")]
    assert radio.values == {"freq": 145_500_000, "nr": True}
    assert not path.exists()
    second.apply()
    second.restore()
    assert radio.values == {"freq": 145_500_000, "nr": True}, "and the new session's own restore agrees"


def test_what_could_not_be_put_back_is_kept_for_next_time(tmp_path):
    path = tmp_path / "r.json"
    radio = DictRadio(a=1, b=2)
    setup = DataSetup([radio.setting("a", 10), radio.setting("b", 20)], Journal(path))
    setup.apply()
    radio.stubborn.add("b")                              # the radio went away, say
    assert [line.status for line in setup.restore()] == ["restore-failed", "restored"]
    assert Journal(path).load() == [("b", 2)]
    radio.stubborn.clear()
    assert [line.status for line in DataSetup([radio.setting("b", 20)], Journal(path)).recover()] == ["recovered"]
    assert radio.values["b"] == 2 and not path.exists()


def test_what_the_operator_changed_during_the_session_is_left_as_they_set_it(tmp_path):
    """Rule 6 (0.3.2): the operator turns the VFO while the app runs. At
    close the frequency is theirs and stays; what they did not touch is
    still put back."""
    radio = DictRadio(freq=145_500_000, mode="USB", nr=True)
    setup = DataSetup([radio.setting("freq", 144_925_000), radio.setting("mode", "FM-D"),
                       radio.setting("nr", False)], Journal(tmp_path / "r.json"))
    setup.apply()
    radio.values["freq"] = 144_800_000                   # the operator's knob
    radio.writes.clear()
    lines = {line.key: line for line in setup.restore()}
    assert lines["freq"].status == "kept" and lines["freq"].before == "144800000"
    assert (lines["mode"].status, lines["nr"].status) == ("restored", "restored")
    assert radio.values == {"freq": 144_800_000, "mode": "USB", "nr": True}
    assert ("freq", 145_500_000) not in radio.writes
    assert not (tmp_path / "r.json").exists(), "a kept setting is not left to put back later"


def test_a_setting_turned_back_to_the_apps_value_is_put_back_as_usual():
    radio = DictRadio(freq=145_500_000)
    setup = DataSetup([radio.setting("freq", 144_925_000)])
    setup.apply()
    radio.values["freq"] = 144_800_000
    radio.values["freq"] = 144_925_000                   # and back again before closing
    assert setup.restore()[0].status == "restored" and radio.values["freq"] == 145_500_000


def test_a_change_the_app_made_with_adjust_is_still_the_apps(tmp_path):
    """The control strip's squelch is the app's change: put back at close.
    The operator's own knob after it is theirs."""
    radio = DictRadio(sql=10)
    setup = DataSetup([], Journal(tmp_path / "r.json"))
    s = radio.setting("sql", None)
    assert setup.adjust(s, 40).status == "set"
    assert setup.restore()[0].status == "restored" and radio.values["sql"] == 10
    setup.adjust(s, 60)
    radio.values["sql"] = 25
    assert setup.restore()[0].status == "kept" and radio.values["sql"] == 25


def test_after_a_crash_the_operators_change_is_still_theirs(tmp_path):
    path = tmp_path / "r.json"
    radio = DictRadio(freq=145_500_000, nr=True)
    DataSetup([radio.setting("freq", 144_925_000), radio.setting("nr", False)], Journal(path)).apply()
    radio.values["freq"] = 144_800_000                   # then the app dies
    lines = DataSetup([radio.setting("freq", 144_925_000), radio.setting("nr", False)], Journal(path)).recover()
    assert [(line.key, line.status) for line in lines] == [("nr", "recovered"), ("freq", "kept")]
    assert radio.values == {"freq": 144_800_000, "nr": True} and not path.exists()


def test_a_journal_from_before_the_rule_is_put_back_whole(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"radio": "x", "saved": [["freq", 145_500_000]]}))
    radio = DictRadio(freq=144_800_000)
    lines = DataSetup([radio.setting("freq", 144_925_000)], Journal(path)).recover()
    assert lines[0].status == "recovered" and radio.values["freq"] == 145_500_000


def test_a_setting_with_no_reader_is_set_and_said_to_stay():
    radio = DictRadio()
    sent = []
    setup = DataSetup([Setting("simplex", "simplex", None, lambda v: sent.append(v) or True),
                       Setting("data_mod", note="set DATA MOD to LAN on the radio")])
    lines = setup.apply()
    assert [line.status for line in lines] == ["set-unrestorable", "manual"]
    assert lines[1].note.startswith("set DATA MOD")
    assert sent == ["simplex"] and setup.restore() == []
    assert radio.writes == []


def test_a_journal_name_is_a_file_name():
    assert journal_name("icom", "ic-9700", "198.51.100.150") == "icom-ic-9700-192-168-1-150.json"
    assert journal_name("yaesu", "ftx-1", "/dev/serial/by-id/usb-Silicon_Labs") == \
        "yaesu-ftx-1-dev-serial-by-id-usb-silicon-labs.json"


# -- an IC-9700's set-up, on FakeIcom ------------------------------------------


def _caller(loop):
    """What a packet link's `_call` is: run on the rig's loop, from a thread."""
    def call(fn, *args, timeout=2.0):
        async def run():
            got = fn(*args)
            if asyncio.iscoroutine(got):
                got = await got
            return got
        return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout)
    return call


def test_an_ic9700_is_set_up_for_packet_on_the_lan_and_put_back(tmp_path):
    async def run():
        async with OnFake() as (rig, radio):
            radio.freq, radio.mode, radio.data, radio.filter = 433_500_000, 0x05, 0x00, 0x02     # FM voice, FIL2
            radio.squelch_level_by_band[144_000_000] = 0                                        # 2 m wide open
            radio.functions[0x46] = 0x01                                                       # VOX on
            radio.data_mod = 0x00
            await asyncio.sleep(0.3)
            setup = DataSetup(settings(rig, _caller(asyncio.get_running_loop()), source="lan",
                                       frequency=144_800_000),
                              Journal(tmp_path / "r.json"))
            report = {line.key: line for line in await asyncio.to_thread(setup.apply)}
            status = {k: line.status for k, line in report.items()}
            assert status == {"frequency": "set", "mode": "set", "simplex": "set", "tone": "set",
                              "tsql": "already", "dtcs": "already", "nr": "set", "nb": "already",
                              "an": "already", "comp": "already", "vox": "set", "monitor": "already",
                              "afc": "already", "data_mod": "set", "data_off_mod": "set",
                              "mod_level": "report", "af_sql": "already", "squelch": "report"}
            assert radio.freq == 144_800_000
            assert (radio.mode, radio.data, radio.filter) == (0x05, 0x01, 0x01), "FM-D on FIL1, wide"
            assert radio.functions[0x40] == 0 and radio.functions[0x46] == 0, "NR and VOX off"
            assert radio.data_mod == 0x05 and radio.setup_menu[115] == 0x00, "DATA MOD LAN, DATA OFF MOD MIC"
            assert report["mode"].before == "FM FIL2" and report["mode"].after == "FM-D FIL1"
            assert radio.duplex == 0x10 and (report["simplex"].before, report["simplex"].after) == ("DUP+", "simplex")
            assert report["mod_level"].before == "50%", "LAN MOD Level read and left: unmeasured"
            assert "fully open" in report["squelch"].note, "read on the packet band, after the retune"

            back = await asyncio.to_thread(setup.restore)
            assert all(line.status == "restored" for line in back)
            assert [line.key for line in back][-2:] == ["mode", "frequency"]
            assert radio.freq == 433_500_000 and (radio.mode, radio.data, radio.filter) == (0x05, 0x00, 0x02)
            assert radio.functions[0x40] == 1 and radio.functions[0x46] == 1
            assert radio.tone_functions[0x42] == 1
            assert radio.duplex == 0x12, "the repeater shift it was found on"
            assert radio.data_mod == 0x00 and radio.setup_menu[115] == 0x02
            assert radio.menu_writes == [], "no menu item outside the set-up's own was written"
            assert not (tmp_path / "r.json").exists()
    asyncio.run(run())


def test_a_level_given_is_set_and_the_usb_items_are_the_usb_ones(tmp_path):
    async def run():
        async with OnFake() as (rig, radio):
            radio.setup_menu[107] = 0x01                         # USB AF SQL on
            level = mod_level_from_percent(30)
            setup = DataSetup(settings(rig, _caller(asyncio.get_running_loop()), source="usb",
                                       mod_level=level))
            report = {line.key: line for line in await asyncio.to_thread(setup.apply)}
            assert report["mod_level"].status == "set" and report["mod_level"].after == "30%"
            assert radio.setup_menu[113] == level and radio.setup_menu[114] == 128, "USB MOD Level, not LAN's"
            assert radio.setup_menu[107] == 0 and radio.data_mod == 0x03
            await asyncio.to_thread(setup.restore)
            assert radio.setup_menu[113] == 128 and radio.setup_menu[107] == 1 and radio.data_mod == 0x00
    asyncio.run(run())


def test_a_switch_the_radio_does_not_have_is_left_alone(tmp_path):
    async def run():
        async with OnFake() as (rig, radio):
            radio.missing_functions.add(0x4A)                   # no AFC
            setup = DataSetup(settings(rig, _caller(asyncio.get_running_loop())))
            report = {line.key: line.status for line in await asyncio.to_thread(setup.apply)}
            assert report["afc"] == "unavailable"
            assert 0x4A not in {sub for sub, _v in radio.function_writes}
            await asyncio.to_thread(setup.restore)
    asyncio.run(run())


def test_a_crashed_session_is_put_back_on_the_next_open(tmp_path):
    path = tmp_path / "r.json"

    async def run():
        async with OnFake() as (rig, radio):
            call = _caller(asyncio.get_running_loop())
            await asyncio.to_thread(DataSetup(settings(rig, call, frequency=144_800_000), Journal(path)).apply)
            assert radio.data_mod == 0x05 and radio.functions[0x40] == 0
            # The app dies: no restore. A new session on the same radio:
            fresh = DataSetup(settings(rig, call, frequency=144_800_000), Journal(path))
            lines = await asyncio.to_thread(fresh.recover)
            assert {line.key for line in lines} >= {"frequency", "mode", "nr", "data_mod", "data_off_mod"}
            assert all(line.status == "recovered" for line in lines)
            assert radio.freq == 144_174_000 and radio.data_mod == 0x00 and radio.functions[0x40] == 1
            assert "simplex" in {line.key for line in lines} and radio.duplex == 0x12
            assert not path.exists()
    asyncio.run(run())


def test_simplex_already_is_not_written(tmp_path):
    async def run():
        async with OnFake() as (rig, radio):
            radio.duplex = 0x10
            setup = DataSetup(settings(rig, _caller(asyncio.get_running_loop())), Journal(tmp_path / "r.json"))
            line = {ln.key: ln for ln in await asyncio.to_thread(setup.apply)}["simplex"]
            assert (line.status, line.before) == ("already", "simplex")
            await asyncio.to_thread(setup.restore)
            assert radio.duplex_writes == []
    asyncio.run(run())


def test_with_split_on_the_duplex_is_left_alone_and_the_operator_asked(tmp_path):
    # 0F answers 01 while split is on, whatever the duplex: unreadable, so
    # never written - not even 0F 10, which could not be put back.
    async def run():
        async with OnFake() as (rig, radio):
            radio.duplex, radio.split = 0x10, True
            setup = DataSetup(settings(rig, _caller(asyncio.get_running_loop())), Journal(tmp_path / "r.json"))
            line = {ln.key: ln for ln in await asyncio.to_thread(setup.apply)}["simplex"]
            assert (line.status, line.before) == ("report", "split")
            assert "make sure it is on simplex" in line.note
            await asyncio.to_thread(setup.restore)
            assert radio.duplex_writes == [] and radio.split is True
    asyncio.run(run())


def test_the_duplex_answers_are_the_guides():
    assert civ.decode_duplex(b"\x0f\x12") == 0x12 and civ.DUPLEX_READS[0x11] == "DUP-"
    assert civ.set_duplex(0x00) == b"\x0f\x10" and civ.set_duplex(0x11) == b"\x0f\x11"
    for bad in (0x01, 0x13, 0x10):
        with pytest.raises(civ.CivError):
            civ.set_duplex(bad)
    with pytest.raises(civ.CivError):
        civ.decode_duplex(b"\x16\x42\x00")


def test_an_ic705_gets_its_switches_and_is_told_the_rest():
    async def run():
        async with OnFake({"model": "ic-705", "civ_address": "0xA2"}) as (rig, radio):
            setup = DataSetup(settings(rig, _caller(asyncio.get_running_loop()), source="lan"))
            report = {line.key: line for line in await asyncio.to_thread(setup.apply)}
            assert report["nr"].status == "set" and "afc" not in report
            for key in ("data_mod", "data_off_mod", "mod_level"):
                assert report[key].status == "manual" and "IC-705" in report[key].note
            assert report["squelch"].status == "report" and "af_sql" not in report
            assert radio.data_mod_writes == [] and radio.setup_menu_writes == [], "no unsourced menu item written"
            await asyncio.to_thread(setup.restore)
            assert radio.functions[0x40] == 1
    asyncio.run(run())


def test_the_usb_slip_warning_is_for_a_9700_transmitting_over_usb():
    nine = models.model("ic-9700")
    assert [k for k, _t in warnings(nine, "usb", True)] == ["usb-tx-slips"]
    assert "LAN" in warnings(nine, "usb", True)[0][1]
    assert warnings(nine, "lan", True) == [] and warnings(nine, "usb", False) == []
    assert warnings(models.model("ic-705"), "usb", True) == []


def test_the_menu_writers_reach_only_their_own_items():
    assert civ.set_mod_level(114, 77) == b"\x1a\x05\x01\x14\x00\x77"
    assert civ.set_data_off_mod(115, 0) == b"\x1a\x05\x01\x15\x00"
    assert civ.set_af_sql(111, False) == b"\x1a\x05\x01\x11\x00"
    for bad in (lambda: civ.set_mod_level(93, 1), lambda: civ.set_data_off_mod(94, 0),
                lambda: civ.set_af_sql(95, False), lambda: civ.set_mod_level(114, 256)):
        with pytest.raises(civ.CivError):
            bad()
    assert civ.set_noise_reduction(False) == b"\x16\x40\x00"
    assert civ.set_afc(True) == b"\x16\x4a\x01"
    assert civ.decode_function(b"\x16\x22\x01") == ("nb", True)
    assert civ.decode_menu_level(b"\x1a\x05\x01\x14\x01\x28") == (114, 128)
    assert civ.decode_squelch_level(b"\x14\x03\x00\x00") == 0


# -- an FTX-1's set-up, on FakeFtx1 ----------------------------------------------


@pytest.fixture
def ftx1():
    from shack_rig.yaesu.fake import FakeFtx1
    from shack_rig.yaesu.rig import YaesuRig
    radio = FakeFtx1().start()
    rig = YaesuRig(radio.device, "ftx-1", poll_interval=0.05)
    rig.open()
    deadline = time.monotonic() + 5
    while not rig.state.get("connected") and time.monotonic() < deadline:
        time.sleep(0.02)
    yield rig, radio
    rig.close()
    radio.stop()


def test_an_ftx1_is_set_up_for_packet_and_put_back(ftx1, tmp_path):
    from shack_rig.yaesu.datasetup import settings as yaesu_settings
    rig, radio = ftx1
    radio.switches.update(BC=1, NL=3, PR=2)                    # DNF, NB and the processor on as well
    path = tmp_path / "ftx1.json"
    setup = DataSetup(yaesu_settings(rig, frequency=144_800_000), Journal(path))
    report = {line.key: line for line in setup.apply()}
    status = {k: line.status for k, line in report.items()}
    assert status == {"frequency": "set", "mode": "set", "shift": "set", "tones": "set", "an": "set",
                      "nr": "set", "nb": "set", "vox": "already", "monitor": "already", "comp": "set",
                      "mod_source_data": "set", "mod_source_fm": "set", "mod_gain_data": "report",
                      "mod_gain_fm": "report"}
    assert radio.freq == 144_800_000 and radio.mode == "A", "DATA-FM, wide"
    assert (radio.shift, radio.ctcss) == (0, 0)
    assert radio.switches == {"BC": 0, "RL": 0, "NL": 0, "VX": 0, "ML": 0, "PR": 1}
    assert radio.menu[(1, 4, 13)][0] == 1 and radio.menu[(1, 3, 12)][0] == 1, "MOD SOURCE USB, both blocks"
    assert report["nr"].before == "on (5)" and report["comp"].after == "off"
    assert path.exists()
    back = setup.restore()
    assert all(line.status == "restored" for line in back), [line.text() for line in back]
    assert radio.freq == 145_500_000 and radio.mode == "4"
    assert (radio.shift, radio.ctcss) == (1, 2), "simplex and tones put back: the FTX-1 reports both"
    assert radio.switches == {"BC": 1, "RL": 5, "NL": 3, "VX": 0, "ML": 0, "PR": 2}
    assert radio.menu[(1, 4, 13)][0] == 0 and radio.menu[(1, 3, 12)][0] == 0
    assert {w for w, _v in radio.menu_writes} == {(1, 4, 13), (1, 3, 12)}, "no other menu item"
    assert radio.refused == [], "every command in the manual's own shape"
    assert not path.exists()


def test_an_ftx1_usb_mod_gain_given_is_set_in_both_blocks(ftx1):
    from shack_rig.yaesu.datasetup import settings as yaesu_settings
    rig, radio = ftx1
    setup = DataSetup(yaesu_settings(rig, mod_gain=35))
    report = {line.key: line.status for line in setup.apply()}
    assert report["mod_gain_data"] == "set" and report["mod_gain_fm"] == "set"
    assert radio.menu[(1, 4, 14)][0] == 35 and radio.menu[(1, 3, 13)][0] == 35
    setup.restore()
    assert radio.menu[(1, 4, 14)][0] == 50 and radio.menu[(1, 3, 13)][0] == 50


def test_the_ftx1_switch_commands_are_the_manuals():
    from shack_rig.yaesu import cat
    assert cat.set_auto_notch(False) == "BC00;" and cat.set_noise_reduction(0) == "RL000;"
    assert cat.set_noise_blanker(0) == "NL0000;" and cat.set_vox(False) == "VX0;"
    assert cat.set_monitor(False) == "ML0000;" and cat.set_processor(False) == "PR01;"
    assert cat.set_shift(0) == "OS00;" and cat.set_tones(2) == "CT02;"
    assert cat.parse_value("RL005;", "RL0") == 5
    with pytest.raises(cat.CatError):
        cat.set_noise_reduction(11)
