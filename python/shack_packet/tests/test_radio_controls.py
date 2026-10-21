"""The control strip's library half (0.3.0): RF power, RF gain and squelch
turned while an IcomLink runs, through the data set-up's journal so they
are put back at close (shack_rig.icom.controls, DataSetup.adjust); the
IC-9700's meters on Icom's published scales; the SWR-jump warning; and a
Pluto's TX and RX gain changed live within its drive cap."""

from __future__ import annotations

import json

import pytest

from radiohelp import until
from sdrhelp import FakePluto
from shack_packet import Engine, Station
from shack_packet.errors import PacketError
from shack_packet.radios import IcomLink, PlutoLink
from test_icom_link import LoopRadio

from shack_rig.icom import controls


@pytest.fixture
def fake():
    f = LoopRadio()
    yield f
    f.close()


def test_the_meters_scale_as_icoms_guide_says():
    assert [controls.po_percent(r) for r in (0, 143, 213)] == [0.0, 50.0, 100.0]
    assert controls.po_percent(178) == pytest.approx(75.0, abs=0.1)        # between the points, not linear overall
    assert controls.po_percent(255) == 120.0                                # capped, not invented
    assert [controls.alc_percent(r) for r in (0, 60, 120)] == [0.0, 50.0, 100.0]
    assert controls.percent(255) == 100 and controls.level(50) == 128


def test_rf_power_gain_and_squelch_are_turned_live_and_put_back_at_close(fake, tmp_path):
    radio = fake.radio
    radio.rf_power_level, radio.rf_gain, radio.squelch_level = 200, 255, 0x40
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000, setup_dir=tmp_path)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert set(link.control_keys()) == {"rf_power", "rf_gain", "squelch"}
        assert link.read_control("rf_power") == 200
        line = link.set_control("rf_power", controls.level(25))
        assert line.status == "set" and radio.rf_power_level == 64
        assert link.set_control("rf_power", controls.level(10)).status == "set" and radio.rf_power_level == 26
        assert link.set_control("rf_gain", 128).status == "set" and radio.rf_gain == 128
        assert link.set_control("squelch", 0).status == "set" and radio.squelch_level == 0
        # Journalled before the first change, as found.
        saved = dict(json.loads(next(tmp_path.glob("*.json")).read_text())["saved"])
        assert saved["rf_power"] == 200 and saved["rf_gain"] == 255 and saved["squelch"] == 0x40
        assert link.read_meters() == {"s_db": pytest.approx(-25.2, abs=0.1)}
    assert (radio.rf_power_level, radio.rf_gain, radio.squelch_level) == (200, 255, 0x40)


def test_transmit_meters_while_keyed(fake):
    radio = fake.radio
    radio.swr_raw, radio.alc_raw = 48, 60
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        link.key()
        assert until(lambda: radio.ptt, 2)
        m = link.read_meters()
        link.unkey()
    assert set(m) == {"po_pct", "swr", "alc_pct"} and m["swr"] == 1.5 and m["alc_pct"] == 50.0


def test_an_swr_jump_between_transmissions_is_warned_of():
    w = controls.SwrWatch()
    for swr in (1.2, 1.3):
        w.seen(swr)
    assert w.over_ended() is None
    w.seen(1.4)
    assert w.over_ended() is None
    w.seen(2.6)
    assert "up from 1.4" in w.over_ended()
    w.seen(2.7)
    assert w.over_ended() is None                     # still high, not a jump


def test_a_pluto_takes_a_new_tx_gain_within_its_cap_and_rx_gain_now():
    pluto = FakePluto()
    link = PlutoLink(uri="ip:fake", frequency=144_800_000, tx_gain_db=-30, max_tx_gain_db=-20,
                     iio_module=pluto.module())
    with pytest.raises(PacketError):
        link.set_tx_gain(-10)                         # above the amplifier's cap
    with pytest.raises(PacketError):
        link.set_tx_gain(-95)
    link.set_tx_gain(-24)
    assert link.tx_gain_db == -24 and str(-24.0) in link.tx_allowed[("tx", "hardwaregain")]
    link.set_rx_gain(40)
    assert link.rx_gain == 40.0
