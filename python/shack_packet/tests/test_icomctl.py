"""IcomControl through an open IcomLink on FakeIcom: the scope switched on
by a listener and off again after the last one leaves, RF POWER written and
read back, and a satellite pass set up, stepped and put back before the
link's own restore. Nothing leaves the loopback; nothing keys."""

from __future__ import annotations

import time

import pytest

from radiohelp import until
from test_icom_link import fake  # noqa: F401 - the fixture
from shack_packet import Engine, Station
from shack_packet.radios import IcomLink
from shack_packet.radios.icomctl import IcomControl, IcomControlError

pytest.importorskip("shack_rig")


def _open(fake, tmp_path, **kw):
    link = IcomLink(fake.profile(), password="secret", frequency=145_825_000, setup_dir=tmp_path, **kw)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    return link


def test_nothing_is_asked_of_a_link_that_is_not_open(fake, tmp_path):
    ctl = IcomControl(_open(fake, tmp_path))
    assert not ctl.available()
    with pytest.raises(IcomControlError):
        ctl.rf_power()


def test_the_scope_runs_while_somebody_listens_and_stops_after(fake, tmp_path, monkeypatch):
    from shack_rig.icom.rig import IcomRig
    monkeypatch.setattr(IcomRig, "SCOPE_RELEASE_S", 0.3)
    radio = fake.radio
    link = _open(fake, tmp_path)
    with link:
        ctl = IcomControl(link)
        assert ctl.scope_refusal() is None
        time.sleep(0.5)
        assert not radio.scope_output, "the link's rig leaves the scope alone until asked"
        got = []
        stop = ctl.listen_scope(got.append)
        assert until(lambda: len(got) >= 3, 5)
        assert got[-1]["points"] == 475 and got[-1]["start"] < 145_825_000 < got[-1]["end"]
        assert ctl.set_scope(span_hz=100_000)
        assert until(lambda: radio.scope_half_span == 50_000, 2)
        stop()
        assert until(lambda: not radio.scope_output, 3), "off again once nobody watches"


def test_rf_power_is_written_and_read_back(fake, tmp_path):
    radio = fake.radio
    link = _open(fake, tmp_path)
    with link:
        ctl = IcomControl(link)
        assert ctl.rf_power() == 255
        assert ctl.set_rf_power(64) and radio.rf_power_level == 64
        radio.ignore_rf_power_writes = True
        assert not ctl.set_rf_power(30), "a write the radio did not act on is not believed"
        assert ctl.po() is not None


def test_a_pass_is_put_back_before_the_link_closes(fake, tmp_path):
    radio = fake.radio
    link = _open(fake, tmp_path)
    with link:
        ctl = IcomControl(link)
        report = ctl.begin_pass("split", rx_hz=145_828_000, tx_hz=145_822_000)
        assert {ln.key: ln.status for ln in report} == {"split": "set", "rx_vfo": "set", "tx_vfo": "set"}
        assert ctl.pass_active and radio.split
        assert ctl.retune(rx_hz=145_827_000, tx_hz=145_823_000)
        assert until(lambda: (radio.freq, radio.freq_unselected) == (145_827_000, 145_823_000), 2)
        back = ctl.end_pass()
        assert {ln.status for ln in back} == {"restored"} and not radio.split
        assert not ctl.pass_active and not ctl.retune(rx_hz=1)
    assert radio.freq == 144_174_000 and not radio.split


def test_a_pass_left_by_a_crash_is_put_back_at_the_next(fake, tmp_path):
    radio = fake.radio
    link = _open(fake, tmp_path)
    with link:
        ctl = IcomControl(link)
        ctl.begin_pass("split", rx_hz=145_828_000, tx_hz=145_822_000)
        ctl._pass = None                    # the process died: nothing restored
        assert radio.split and radio.freq_unselected == 145_822_000
        lines = IcomControl(link).recover()
        assert {ln.status for ln in lines} == {"recovered"}
        assert not radio.split and radio.freq_unselected == 145_825_000
