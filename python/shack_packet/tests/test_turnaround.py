"""The turnaround measurement (radios/turnaround.py), against fakes with a
known key-up and fall: FakeIcom's Po meter over the LAN, FakeFtx1's PO
meter over CAT, and a sound-card "radio" heard by a monitor receiver. Only
fakes are keyed; the refusal without the operator's confirmation is
tested first."""

from __future__ import annotations

import time

import pytest

from radiohelp import until
from shack_packet import Engine, Station
from shack_packet.radios import IcomLink, LinkRefused, SoundcardLink, YaesuLink
from shack_packet.radios.turnaround import CONFIRM, LevelProbe, Probe, RigPowerProbe, measure

pytest.importorskip("shack_rig")
from shack_rig import sound  # noqa: E402
from shack_rig.yaesu.fake import FakeFtx1  # noqa: E402
from test_icom_link import LoopRadio  # noqa: E402

ARMED = Station.of("GM4BRI", tx_enabled=True)


class Never(Probe):
    def on_air(self):
        return False


def test_nothing_is_keyed_without_the_operators_confirmation():
    be = sound.FakeBackend([("Card", 1, 1, (48000,))], realtime=False)
    link = SoundcardLink("Card", ptt="vox", sound_backend=be, station=ARMED)
    with link:
        with pytest.raises(LinkRefused, match="dummy load"):
            measure(link, Never())
        with pytest.raises(LinkRefused):
            measure(link, Never(), confirm=CONFIRM, overs=50)
        assert link.stats["keyed"] == 0
    be.close()


def test_an_icom_over_the_lan_by_its_po_meter():
    fake = LoopRadio()
    fake.radio.rf_rise_s, fake.radio.rf_fall_s = 0.12, 0.06
    try:
        link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
        eng = Engine(ARMED, link)
        with link:
            got = measure(link, RigPowerProbe(link), confirm=CONFIRM, overs=3, hold_s=0.4, apply=True)
            assert got.ok, got.public()
            assert 0.12 <= got.key_up_s <= 0.12 + 0.1, got.public()
            assert 0.06 <= got.unkey_s <= 0.06 + 0.1, got.public()
            assert got.turnaround_s >= got.key_up_s and got.txdelay_s == pytest.approx(got.turnaround_s + 0.08)
            assert link.turnaround_s == got.turnaround_s and link.turnaround_measured
            assert eng.channel.txdelay_s >= got.turnaround_s + 0.08 - 1e-9
            assert not fake.radio.ptt
            assert fake.radio.ptt_commands.count(True) == 3
    finally:
        fake.close()


def test_an_icom_at_minimum_power_is_still_measured():
    """18/10 real-radio finding: an IC-9700 into a dummy load at RF POWER
    0% put only 2-4 of 255 on the Po meter keyed, 0 unkeyed - below the old
    fixed threshold of 10, so `measure()` never saw a key-up at all. The
    default `RigPowerProbe` must now read the baseline first and still
    catch it."""
    fake = LoopRadio()
    fake.radio.rf_power_level = 0                        # the fixed floor: rf_power is 2
    fake.radio.rf_rise_s, fake.radio.rf_fall_s = 0.05, 0.03
    try:
        link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
        Engine(ARMED, link)
        with link:
            got = measure(link, RigPowerProbe(link), confirm=CONFIRM, overs=3, hold_s=0.3)
            assert got.ok, got.public()
            assert 0.05 <= got.key_up_s <= 0.05 + 0.1, got.public()
            assert 0.03 <= got.unkey_s <= 0.03 + 0.1, got.public()
    finally:
        fake.close()


def test_a_noisy_unkeyed_baseline_is_not_read_as_keyed():
    """The probe must not mistake the radio's own idle noise for a carrier:
    `start()`'s baseline has to be the worst of several readings, not the
    first, or a reading above an unlucky first sample would already count
    as "on" before anything was ever keyed."""
    fake = LoopRadio()
    # Cycles 0,1,2,1,0,2,... - never settles, and never clears 2.
    fake.radio.unkeyed_noise = [0, 1, 2, 1, 0, 2]
    try:
        link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
        with link:
            probe = RigPowerProbe(link)
            probe.start()
            assert probe.threshold >= 3, "the baseline must clear the noisiest reading, plus margin"
            for _ in range(12):
                assert probe.on_air() is False
    finally:
        fake.close()


def test_an_ftx1_by_its_po_meter():
    radio = FakeFtx1().start()
    radio.rf_rise_s, radio.rf_fall_s = 0.2, 0.03
    be = sound.FakeBackend([("FTX-1 USB Audio", 1, 1, (48000,))], realtime=True)
    try:
        link = YaesuLink(radio.device, audio="FTX-1 USB Audio", frequency=144_800_000, sound_backend=be,
                         station=ARMED)
        with link:
            got = measure(link, RigPowerProbe(link), confirm=CONFIRM, overs=2, hold_s=0.5)
            assert got.ok, got.public()
            assert 0.2 <= got.key_up_s <= 0.2 + 0.08, got.public()
            assert 0.03 <= got.unkey_s <= 0.03 + 0.08, got.public()
            assert got.turnaround_s == pytest.approx(round(got.key_up_s + 0.0049, 2), abs=0.011)
            assert radio.tx_state == 0
    finally:
        be.close()
        radio.stop()


def test_a_sound_card_radio_heard_by_a_monitor_receiver():
    """A radio on an interface keyed by RTS, which comes up 100 ms after the
    line; a second receiver hears its tone."""
    be = sound.FakeBackend([("Interface", 1, 1, (48000,)), ("Monitor", 1, 0, (48000,))], realtime=True)
    raised = {}

    class Port:
        line_state = {"rts": False}

        def open(self):
            pass

        def set_line(self, name, on):
            self.line_state[name] = on
            raised[name] = time.monotonic() if on else None
            return True

        def close(self):
            self.line_state["rts"] = False

    def on_air():
        t = raised.get("rts")
        return t is not None and time.monotonic() - t >= 0.1

    be.route("Interface", "Monitor", gate=on_air)
    probe = LevelProbe()
    monitor = sound.Input("Monitor", probe.feed, backend=be)
    link = SoundcardLink("Interface", ptt="rts", ptt_device="COM3", sound_backend=be, open_port=lambda d: Port(),
                         station=ARMED)
    try:
        with link:
            assert until(probe.ready, 2)
            got = measure(link, probe, confirm=CONFIRM, overs=3, hold_s=0.5)
            assert got.ok, got.public()
            # The route and the monitor work in 20 ms blocks, the pacing 60 ms ahead.
            assert 0.1 <= got.key_up_s <= 0.1 + 0.1, got.public()
            assert got.unkey_s <= 0.1, got.public()
    finally:
        monitor.close()
        be.close()


def test_a_radio_that_never_comes_up_is_reported_and_left_unkeyed():
    be = sound.FakeBackend([("Card", 1, 1, (48000,))], realtime=True)
    link = SoundcardLink("Card", ptt="vox", sound_backend=be, station=ARMED)
    try:
        with link:
            t = time.monotonic()
            got = measure(link, Never(), confirm=CONFIRM, overs=3, hold_s=0.3)
            assert not got.ok and got.overs[0].key_up_s is None and len(got.overs) == 1
            assert got.turnaround_s is None and got.txdelay_s is None
            assert not link.keyed and time.monotonic() - t < 5
    finally:
        be.close()


def test_a_disarmed_station_is_not_keyed_to_measure():
    be = sound.FakeBackend([("Card", 1, 1, (48000,))], realtime=False)
    link = SoundcardLink("Card", ptt="vox", sound_backend=be, station=Station.of("GM4BRI"))
    with link:
        with pytest.raises(LinkRefused) as e:
            measure(link, Never(), confirm=CONFIRM)
        assert e.value.reason == "transmit-disabled" and link.stats["keyed"] == 0
    be.close()
