"""The Kenwood backend: the TM-V71 command codec against its sources
(LA3QMA's list, hamlib's tmd710.c, CHIRP's kenwood_live), KenwoodRig
against FakeTmV71 on a pty - connecting, the band, tuning inside a
segment, busy from BY, power H/M/L, the data set-up read, set and put back
(after a crash too), keying by the interface's RTS, a CM108 pin or its VOX
and never by CAT - and audio through a FakeBackend. No radio is touched."""

from __future__ import annotations

import array
import json
import time

import pytest

from shack_rig import Lease, PttRefused, Rig, STATE_KEYS, cm108, sound
from shack_rig.datasetup import DataSetup, Journal
from shack_rig.kenwood import cat, datasetup, models
from shack_rig.kenwood.fake import FakeTmV71
from shack_rig.kenwood.rig import KenwoodRig
from shack_rig.serialio import SerialPort

V71 = models.model("tm-v71")


def until(cond, timeout=5.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


# -- the codec ---------------------------------------------------------------------


def test_commands_are_the_sources_to_the_character():
    assert cat.read_id() == "ID\r"
    assert cat.read_vfo(1) == "FO 1\r"
    assert cat.set_power(0, 2) == "PC 0,2\r"
    assert cat.set_band_mode(0, 0) == "VM 0,0\r"
    assert cat.read_busy(1) == "BY 1\r"
    with pytest.raises(cat.CatError):
        cat.set_power(0, 3)
    with pytest.raises(cat.CatError):
        cat.read_vfo(2)


def test_answers_are_read_and_a_wrong_shape_is_refused():
    assert cat.parse_id("ID TM-V71") == "TM-V71"
    assert cat.parse_busy("BY 1,1") == (1, True)
    assert cat.parse_power("PC 0,1") == (0, 1) and cat.POWER_LEVELS[1] == "M"
    assert cat.parse_band_mode("VM 0,1") == (0, 1)
    # LA3QMA and hamlib disagree on SQ's read; both shapes give the level.
    assert cat.parse_squelch("SQ 0,1F") == 31 and cat.parse_squelch("SQ 0A") == 10
    for bad, parse in (("BY 0", cat.parse_busy), ("PC 0,7", cat.parse_power), ("FO 0,1,2", cat.parse_vfo),
                       ("?", cat.parse_id)):
        with pytest.raises(cat.CatError):
            parse(bad)
    assert cat.refused("?") and cat.refused("N") and not cat.refused("FO 0")
    answers, rest = cat.split_answers("ID TM-V71\rBY 0,0\r\nPC")
    assert answers == ["ID TM-V71", "BY 0,0"] and rest.lstrip("\n") == "PC"


def test_an_fo_write_changes_only_the_named_fields_in_hamlibs_widths():
    vfo = cat.parse_vfo("FO 0,0145500000,A,2,0,1,0,0,08,08,000,00600000,0")
    assert vfo.freq == 145_500_000 and vfo.number("step") == 10 and vfo.tones == [1, 0, 0]
    new = vfo.replace(freq=144_800_000, shift=0, tone=0)
    assert new.command() == "FO 0,0144800000,A,0,0,0,0,0,08,08,000,00600000,0\r", "the hex step goes back as it came"
    with pytest.raises(cat.CatError):
        vfo.replace(freq=12_345_678_901)


def test_a_menu_write_is_the_radios_own_answer_with_one_field_changed():
    from shack_rig.kenwood.fake import FACTORY_MENU
    menu = cat.parse_menu("MU " + ",".join(FACTORY_MENU))
    assert menu.complete and menu.data_band == 1 and menu.data_speed == 0
    nine = menu.replace(cat.MU_DATA_SPEED, 1)
    assert nine.data_speed == 1
    assert [i for i, (a, b) in enumerate(zip(menu.raw, nine.raw)) if a != b] == [cat.MU_DATA_SPEED]
    short = cat.parse_menu("MU " + ",".join(FACTORY_MENU[:41]))
    assert not short.complete
    with pytest.raises(cat.CatError):
        short.replace(cat.MU_DATA_SPEED, 1)


def test_the_model_table():
    assert V71.pc_id == "TM-V71" and V71.pc_baud == 9600 and V71.pc_bauds == (9600, 19200, 38400, 57600)
    assert V71.can_transmit(144_800_000) and V71.can_transmit(433_500_000)
    assert not V71.can_transmit(146_500_000), "Region 1 licence ranges, as every model here"
    assert V71.covers(1_296_000_000) and not V71.covers(200_000_000)
    assert models.segment(145_000_000) == "vhf" and models.segment(1_240_000_000) == "1200"


# -- KenwoodRig on the fake -----------------------------------------------------------


@pytest.fixture
def radio():
    r = FakeTmV71().start()
    yield r
    r.stop()


@pytest.fixture
def rigs():
    made = []

    def make(*a, **kw):
        kw.setdefault("poll_interval", 0.05)
        r = KenwoodRig(*a, **kw)
        made.append(r)
        r.open()
        return r

    yield make
    for r in made:
        r.close()


def interface(radio, line="rts"):
    """open_port for a rig whose PTT is the interface's serial port."""
    port = radio.interface_port(line)

    def open_port(device, baud, bits):
        if device == "interface":
            return port
        return SerialPort(device, baud, stop_bits=bits)
    return port, open_port


def test_it_is_a_rig_that_connects_on_the_data_bands_receive_side(radio, rigs):
    rig = rigs(radio.device, ptt="none")
    assert isinstance(rig, Rig) and set(STATE_KEYS) <= set(rig.state)
    assert until(lambda: rig.state["connected"] and rig.state["freq"]), rig.state
    # EXT. DATA BAND ships on B, so B is the band the rig drives.
    assert rig.band == 1 and rig.state["freq"] == 433_500_000 and rig.state["vfo"] == "VFOB"
    assert rig.state["version"] == "E" and rig.state["radio"] == "TM-V71"
    assert rig.state["data_band"] == 1 and rig.state["data_speed"] == 1200
    radio.vfo[1][1] = f"{433_400_000:010d}"       # the dial turned
    assert until(lambda: rig.state["freq"] == 433_400_000)


def test_a_radio_that_is_not_a_tmv71_is_refused(rigs):
    class Other(FakeTmV71):
        def _handle(self, line):
            if line == "ID":
                self._say("ID TM-D710")
            else:
                super()._handle(line)
    other = Other().start()
    try:
        rig = rigs(other.device, ptt="none")
        assert until(lambda: rig.state["error"])
        assert "TM-D710" in rig.state["error"] and not rig.state["connected"]
    finally:
        rig.close()
        other.stop()


def test_tuning_stays_inside_the_bands_segment_and_the_other_band_is_found(radio, rigs):
    rig = rigs(radio.device, ptt="none")
    assert until(lambda: rig.state["connected"])
    assert rig.set_freq(432_500_000) and radio.freq(1) == 432_500_000
    assert rig.set_freq(144_800_000) is False, "band B is on UHF; no PC command moves it to VHF"
    assert radio.freq(1) == 432_500_000
    assert rig.band_for(144_800_000) == 0, "band A is on VHF"
    assert rig.band_for(1_296_000_000) is None, "neither band is on 23 cm"
    rig.use_band(0)
    assert rig.set_freq(144_800_000) and radio.freq(0) == 144_800_000


def test_busy_power_and_squelch(radio, rigs):
    rig = rigs(radio.device, ptt="none", band=0)
    assert until(lambda: rig.state["connected"])
    assert rig.state["busy"] is None
    rig.watch_squelch(True)
    assert until(lambda: rig.state["busy"] is False)
    radio.set_busy(0, True)
    assert until(lambda: rig.state["busy"] is True)
    radio.set_busy(1, False)
    assert rig.state["busy"] is True, "band B's squelch is not this rig's"
    assert rig.read_power() == 0 and rig.state["power_level"] == "H"
    assert rig.set_power(2) and radio.power[0] == 2 and rig.state["power_level"] == "L"
    assert rig.read_squelch() == 5


def test_the_data_set_up_is_read_set_and_put_back(radio, rigs, tmp_path):
    rig = rigs(radio.device, ptt="none")
    assert until(lambda: rig.state["connected"])
    band = datasetup.choose_band(rig, 144_800_000)
    assert band == 0
    rig.use_band(band)
    before = {"vfo": list(radio.vfo[0]), "mode": radio.band_mode[0], "menu": list(radio.menu)}
    journal = Journal(tmp_path / "tmv71.json")
    setup = DataSetup(datasetup.settings(rig, frequency=144_800_000, speed=9600), journal)
    report = {ln.key: ln for ln in setup.apply()}
    assert report["vfo_mode"].status == "set" and radio.band_mode[0] == 0
    assert report["frequency"].status == "set" and radio.freq(0) == 144_800_000
    assert report["mode"].status == "already"
    assert report["shift"].status == "set" and radio.vfo[0][3] == "0"
    assert report["tones"].status == "set" and radio.vfo[0][5:8] == ["0", "0", "0"]
    assert report["data_band"].status == "set" and radio.data_band() == 0
    assert report["data_speed"].status == "set" and radio.data_speed() == 9600
    assert report["sqc"].status == "report" and report["sqc"].before == "BUSY"
    assert report["power"].status == "report" and report["power"].before == "H"
    changed = [i for i, (a, b) in enumerate(zip(before["menu"], radio.menu)) if a != b]
    assert changed == [cat.MU_DATA_BAND, cat.MU_DATA_SPEED], "no other menu item moved"
    assert json.loads((tmp_path / "tmv71.json").read_text())["saved"], "on disk before the radio is touched"
    restored = setup.restore()
    assert all(ln.status == "restored" for ln in restored), [ln.text() for ln in restored]
    assert radio.vfo[0] == before["vfo"] and radio.band_mode[0] == before["mode"] and radio.menu == before["menu"]
    assert [ln.key for ln in restored][-1] == "vfo_mode", "back on its memory channel last"
    assert not (tmp_path / "tmv71.json").exists()
    assert radio.cat_keys == [], "never TX or RX on the PC port"


def test_a_session_that_died_is_put_back_by_the_next(radio, rigs, tmp_path):
    rig = rigs(radio.device, ptt="none", band=0)
    assert until(lambda: rig.state["connected"])
    menu_before = list(radio.menu)
    DataSetup(datasetup.settings(rig, frequency=144_800_000, speed=9600), Journal(tmp_path / "j.json")).apply()
    assert radio.data_speed() == 9600
    # No restore: the process "crashed". The next start recovers first.
    again = DataSetup(datasetup.settings(rig, frequency=144_800_000, speed=1200), Journal(tmp_path / "j.json"))
    lines = again.recover()
    assert lines and all(ln.status == "recovered" for ln in lines)
    assert radio.menu == menu_before and radio.band_mode[0] == 1


def test_a_menu_the_library_does_not_know_is_never_written(rigs):
    class Short(FakeTmV71):
        def _handle(self, line):
            if line == "MU":
                self._say("MU " + ",".join(self.menu[:41]))
            else:
                super()._handle(line)
    radio = Short().start()
    try:
        rig = rigs(radio.device, ptt="none", band=0)
        assert until(lambda: rig.state["connected"])
        report = {ln.key: ln for ln in DataSetup(datasetup.settings(rig, speed=9600)).apply()}
        assert report["data_speed"].status == "manual" and "518" in report["data_speed"].note
        assert radio.menu_writes == []
    finally:
        radio.stop()


def test_keying_by_the_interfaces_rts_takes_a_lease_and_ends_with_it(radio, rigs):
    port, open_port = interface(radio, "rts")
    rig = rigs(radio.device, ptt="rts", ptt_device="interface", allow_ptt=True, open_port=open_port, band=0)
    assert until(lambda: rig.state["freq"])
    assert port.opened and port.history == [], "opened with the lines low, and nothing raised"
    rig.set_freq(144_800_000)
    radio.menu[37] = "0"                          # the DATA jack on band A
    with pytest.raises(PttRefused):
        rig.set_ptt(True)
    lease = Lease("packet", seconds=30)
    assert rig.set_ptt(True, lease) and radio.tx_band == 0 and rig.ptt_line_up()
    lease.revoke("the operator disarmed")
    assert until(lambda: not radio.transmitting, 2.0), "the poll unkeys on the first refusal"
    assert rig.set_ptt(True, Lease("packet", seconds=30)) and radio.transmitting
    rig.close()
    assert not radio.transmitting and port.closed
    assert radio.cat_keys == [], "no TX on the PC port, ever"


def test_keying_refused_off_band_switched_off_or_without_a_ptt(radio, rigs):
    rig = rigs(radio.device, ptt="none", band=0, allow_ptt=True)
    assert until(lambda: rig.state["freq"])
    with pytest.raises(PttRefused, match="receives only"):
        rig.set_ptt(True, Lease("packet"))
    rig.close()                                   # the PC port is opened exclusively
    port, open_port = interface(radio)
    rig2 = rigs(radio.device, ptt="rts", ptt_device="interface", open_port=open_port, band=0)
    assert until(lambda: rig2.state["freq"])
    with pytest.raises(PttRefused, match="switched off"):
        rig2.set_ptt(True, Lease("packet"))
    rig2.allow_ptt = True
    rig2.set_freq(146_500_000)
    with pytest.raises(PttRefused, match="outside"):
        rig2.set_ptt(True, Lease("packet"))
    assert not radio.transmitting and port.history == []


def test_a_cm108_pin_keys_the_radio_and_is_low_from_the_moment_it_opens(radio, rigs):
    chip = radio.cm108_writer(gpio=3)
    rig = rigs(radio.device, ptt="cm108", ptt_device="/dev/hidraw-fake", open_cm108=chip, band=0,
               allow_ptt=True)
    assert until(lambda: rig.state["freq"])
    assert chip.reports == [bytes([0, 0, 0, 4, 0])], "GPIO 3 an output, low, before anything else"
    radio.menu[37] = "0"
    rig.set_freq(144_800_000)
    assert rig.set_ptt(True, Lease("packet", seconds=30)) and radio.tx_band == 0
    assert chip.reports[-1] == bytes([0, 0, 4, 4, 0])
    assert rig.set_ptt(False) and not radio.transmitting
    rig.close()
    assert chip.closed and not radio.transmitting


def test_cm108_reports_and_device_search():
    assert cm108.report(3, True) == bytes([0, 0, 4, 4, 0])
    assert cm108.report(1, False) == bytes([0, 0, 0, 1, 0])
    with pytest.raises(ValueError):
        cm108.report(9, True)
    assert cm108.gpio_capable(0x0D8C, 0x0012) and cm108.gpio_capable(0x1209, 0x7388)
    assert not cm108.gpio_capable(0x1209, 0x0001), "pid.codes' shared vendor ID alone is not a CM108"


def test_cm108_finds_its_hidraw_by_vendor(tmp_path):
    for n, hid_id in ((0, "0003:00000D8C:00000012"), (1, "0003:0000046D:0000C52B")):
        d = tmp_path / f"hidraw{n}" / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(f"HID_ID={hid_id}\nHID_NAME=dev{n}\n")
    found = cm108.find_hidraw(str(tmp_path))
    assert [f["device"] for f in found] == ["/dev/hidraw0"] and found[0]["name"] == "dev0"


def test_vox_keys_nothing_but_lets_the_audio_through_under_the_lease(radio, rigs):
    be = sound.FakeBackend([("Digirig", 1, 1, (48000,))], realtime=False)
    rig = rigs(radio.device, ptt="vox", band=0, allow_ptt=True, audio_in="Digirig", audio_out="Digirig",
               sound_backend=be)
    heard = []
    rig.listen_audio(heard.append)
    assert until(lambda: rig.state["audio_rate"] == 48000)
    assert rig.tx_rate == 48000
    rig.set_freq(144_800_000)
    pcm = array.array("h", range(480)).tobytes()
    assert rig.send_audio(pcm) is False, "not keyed: nothing reaches the interface"
    assert rig.set_ptt(True, Lease("packet", seconds=30))
    assert rig.send_audio(pcm)
    be.pump(0.01)
    out = be.stream("Digirig", output=True)
    assert bytes(out.played[:960]) == pcm
    rig.set_ptt(False)
    assert out.closed, "unkeying lets go of the output, so a VOX interface drops"
    assert radio.cat_keys == []
