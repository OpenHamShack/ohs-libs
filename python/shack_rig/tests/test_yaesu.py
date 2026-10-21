"""The Yaesu backend: the CAT codec against the FTX-1 manual's tables, the
model table, and YaesuRig against FakeFtx1 on a pty - connecting, tuning,
FM with DATA, the squelch as busy, simplex, MOD SOURCE read, set and put
back, keying under a lease by CAT or by a modem line, and audio both ways
through a FakeBackend. No radio is touched."""

from __future__ import annotations

import array
import time

import pytest

from shack_rig import Lease, PttRefused, Rig, STATE_KEYS, sound
from shack_rig.yaesu import cat, models
from shack_rig.yaesu.fake import FakeFtx1
from shack_rig.yaesu.rig import YaesuRig

FTX1 = models.model("ftx-1")


def until(cond, timeout=5.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


# -- the codec ---------------------------------------------------------------------


def test_commands_are_the_manuals_to_the_digit():
    assert cat.set_freq(14_250_000) == "FA014250000;"            # the manual's own example (p. 4)
    assert cat.set_freq(144_800_000) == "FA144800000;"
    with pytest.raises(cat.CatError):
        cat.set_freq(1_000_000_000)
    assert cat.code_for("FM", data=True) == "A" and cat.set_mode("A") == "MD0A;"
    assert cat.code_for("FM", data=True, narrow=True) == "F"
    assert cat.code_for("CW", data=True) is None
    assert cat.set_tx(True) == "TX1;" and cat.set_tx(False) == "TX0;"
    assert cat.read_radio_info() == "RI0;" and cat.tones_off() == "CT00;" and cat.simplex() == "OS00;"
    assert cat.read_menu(1, 4, 13) == "EX010413;" and cat.set_menu(1, 4, 13, 1, 1) == "EX0104131;"
    with pytest.raises(cat.CatError):
        cat.set_menu(1, 4, 13, 10, 1)


def test_answers_are_read_and_a_wrong_shape_is_refused():
    assert cat.parse_freq("FA145500000;") == 145_500_000
    assert cat.parse_mode("MD0A;") == (0, "A") and cat.vocabulary("A") == ("FM", True, False)
    assert cat.vocabulary("H") == (None, False, False), "C4FM has no name here"
    assert cat.parse_tx("TX2;") == 2
    ri = cat.parse_radio_info("RI00010001;")
    assert ri.tx == 1 and ri.busy and not ri.hi_swr
    assert cat.parse_smeter("SM0123;") == 123
    assert cat.parse_menu("EX0104131;") == ((1, 4, 13), "1")
    for bad, parse in (("FA1455;", cat.parse_freq), ("TX3;", cat.parse_tx), ("RI0;", cat.parse_radio_info)):
        with pytest.raises(cat.CatError):
            parse(bad)
    answers, rest = cat.split_answers("FA145500000;MD0A;TX")
    assert answers == ["FA145500000;", "MD0A;"] and rest == "TX"


def test_the_ftx1_if_answer_field_by_field():
    # Laid out from the manual's p. 17 diagram: channel, frequency, clarifier,
    # RX/TX clarifier, mode, VFO/memory, tone, the fixed 00, shift.
    a = "IF00000144800000-012010A0100" + "2;"
    assert len(a) == cat.IF_LENGTH
    info = cat.parse_if(a)
    assert info.channel == "00000" and info.freq == 144_800_000
    assert info.clarifier_hz == -120 and info.rx_clarifier and not info.tx_clarifier
    assert info.mode_code == "A" and info.mode == "FM" and info.data
    assert info.vfo_memory == 0 and info.tone == 1 and info.shift == 2
    with pytest.raises(cat.CatError):
        cat.parse_if(a[:-2] + ";")
    with pytest.raises(cat.CatError):
        cat.parse_if(a.replace("-0120", "x0120"))


def test_the_model_table():
    assert FTX1.cat_id == "0840" and FTX1.cat_baud == 38400 and FTX1.cat_stop_bits == 1
    assert {b: (i.p1, i.p2, i.p3) for b, i in FTX1.mod_source.items()} == {"data": (1, 4, 13), "fm": (1, 3, 12)}
    assert FTX1.can_transmit(144_800_000) and not FTX1.can_transmit(146_500_000)
    nine91 = models.model("ft-991a")
    assert not nine91.verified and not nine91.mod_source and not nine91.has_radio_info
    assert nine91.cat_stop_bits == 2


# -- YaesuRig on the fake -------------------------------------------------------------


@pytest.fixture
def radio():
    r = FakeFtx1().start()
    yield r
    r.stop()


@pytest.fixture
def rigs():
    made = []

    def make(*a, **kw):
        kw.setdefault("poll_interval", 0.05)
        r = YaesuRig(*a, **kw)
        made.append(r)
        r.open()
        return r

    yield make
    for r in made:
        r.close()


def test_it_is_a_rig_that_connects_and_follows_the_radio(radio, rigs):
    rig = rigs(radio.device, "ftx-1")
    assert isinstance(rig, Rig) and set(STATE_KEYS) <= set(rig.state)
    assert until(lambda: rig.state["connected"] and rig.state["freq"] == 145_500_000), rig.state
    assert rig.state["mode"] == "FM" and rig.state["mode_label"] == "FM"
    assert rig.set_freq(144_800_000) and until(lambda: radio.freq == 144_800_000)
    assert rig.set_mode("FM", data=True) and until(lambda: radio.mode == "A")
    assert until(lambda: rig.state["mode_label"] == "FM-D")
    assert rig.set_freq(1_000_000_000) is False, "outside the radio's coverage"
    radio.freq = 145_000_000                      # the dial turned
    assert until(lambda: rig.state["freq"] == 145_000_000)


def test_a_radio_that_is_not_an_ftx1_is_refused(rigs):
    class Other(FakeFtx1):
        def _handle(self, cmd):
            if cmd == "ID":
                self._say("ID0670;")
            else:
                super()._handle(cmd)
    other = Other().start()
    try:
        rig = rigs(other.device, "ftx-1")
        assert until(lambda: rig.state["error"])
        assert "0670" in rig.state["error"] and not rig.state["connected"]
    finally:
        rig.close()
        other.stop()


def test_the_squelch_is_busy_and_the_channel_is_made_simplex(radio, rigs):
    rig = rigs(radio.device)
    assert until(lambda: rig.state["connected"])
    assert rig.state["busy"] is None
    rig.watch_squelch(True)
    assert until(lambda: rig.state["busy"] is False)
    radio.squelch_open = True
    assert until(lambda: rig.state["busy"] is True)
    assert rig.set_simplex()
    assert until(lambda: radio.ctcss == 0 and radio.shift == 0)


def test_mod_source_is_read_set_to_usb_and_put_back(radio, rigs):
    rig = rigs(radio.device)
    assert until(lambda: rig.state["connected"])
    assert rig.set_mod_source("usb") is False, "not read yet: nothing to put back, so nothing written"
    assert radio.menu_writes == []
    assert rig.read_mod_source() == {"data": "mic", "fm": "mic"}
    assert rig.set_mod_source("usb")
    assert until(lambda: radio.menu[(1, 4, 13)][0] == 1 and radio.menu[(1, 3, 12)][0] == 1)
    assert rig.restore_mod_source()
    assert until(lambda: radio.menu[(1, 4, 13)][0] == 0 and radio.menu[(1, 3, 12)][0] == 0)
    assert {w for w, _v in radio.menu_writes} == {(1, 4, 13), (1, 3, 12)}, "no other menu item"


def test_keying_by_cat_takes_a_lease_and_ends_when_the_lease_does(radio, rigs):
    rig = rigs(radio.device, allow_ptt=True)
    assert until(lambda: rig.state["freq"])
    rig.set_freq(144_800_000)
    with pytest.raises(PttRefused):
        rig.set_ptt(True)
    assert radio.tx_state == 0
    lease = Lease("packet", seconds=30)
    assert rig.set_ptt(True, lease)
    assert until(lambda: radio.tx_state == 1)
    lease.revoke("the operator disarmed")
    assert until(lambda: radio.tx_state == 0, 2.0), "the poll unkeys on the first refusal"
    assert rig.lease_holder is None
    assert rig.set_ptt(True, Lease("packet", seconds=30)) and until(lambda: radio.tx_state == 1)
    assert rig.set_ptt(False) and until(lambda: radio.tx_state == 0)


def test_keying_refused_off_band_and_with_transmit_switched_off(radio, rigs):
    rig = rigs(radio.device, allow_ptt=False)
    assert until(lambda: rig.state["freq"])
    with pytest.raises(PttRefused, match="switched off"):
        rig.set_ptt(True, Lease("packet"))
    rig.allow_ptt = True
    rig.set_freq(146_500_000)
    with pytest.raises(PttRefused, match="outside"):
        rig.set_ptt(True, Lease("packet"))
    assert radio.tx_state == 0


def test_closing_a_keyed_rig_unkeys_it(radio, rigs):
    rig = rigs(radio.device, allow_ptt=True)
    assert until(lambda: rig.state["freq"])
    rig.set_freq(144_800_000)
    assert rig.set_ptt(True, Lease("packet", seconds=30)) and until(lambda: radio.tx_state == 1)
    rig.close()
    assert radio.tx_state == 0


def test_a_line_ptt_keys_only_through_set_ptt_and_drops_on_close(radio, rigs):
    """RPTT SELECT = RTS on the radio, and RTS on the Standard port as the
    PTT: the line is never raised by opening, only by set_ptt."""
    radio.menu[(1, 4, 15)][0] = 1          # RPTT SELECT = RTS, DATA block
    radio.mode = "A"
    line = radio.line_port()

    def open_port(device, baud, bits):
        if device == "standard":
            return line
        from shack_rig.serialio import SerialPort
        return SerialPort(device, baud, stop_bits=bits)

    rig = rigs(radio.device, ptt="rts", ptt_device="standard", allow_ptt=True, open_port=open_port)
    assert until(lambda: rig.state["freq"])
    assert line.opened and radio.line_history == [], "opened with the lines low, and nothing raised"
    rig.set_freq(144_800_000)
    assert rig.set_ptt(True, Lease("packet", seconds=30))
    assert radio.tx_state == 2 and radio.lines["rts"]
    assert not any(c == "TX1" for c in radio.commands), "a line PTT sends no CAT key"
    assert rig.set_ptt(False)
    assert radio.tx_state == 0 and not radio.lines["rts"]
    assert rig.set_ptt(True, Lease("packet", seconds=30)) and radio.tx_state == 2
    rig.close()
    assert radio.tx_state == 0 and line.closed
    assert [(n, on) for _t, n, on in radio.line_history] == [("rts", True), ("rts", False), ("rts", True),
                                                             ("rts", False)]


def ramp(n, start=0):
    return array.array("h", [(start + i) % 20000 for i in range(n)]).tobytes()


def test_audio_both_ways_at_the_codecs_own_rate(radio, rigs):
    be = sound.FakeBackend([("FTX-1 USB Audio", 1, 1, (48000, 44100))], realtime=False)
    rig = rigs(radio.device, allow_ptt=True, audio_in="FTX-1 USB Audio", audio_out="FTX-1 USB Audio",
               sound_backend=be)
    heard = []
    rig.listen_audio(heard.append)
    assert until(lambda: rig.state["audio_rate"] == 48000)
    assert rig.tx_rate == 48000, "the same rate both ways, no resampling"
    be.feed("FTX-1 USB Audio", ramp(960))
    be.pump(0.02)
    assert until(lambda: b"".join(heard)[:1920] == ramp(960))
    rig.set_freq(144_800_000)
    assert rig.send_audio(ramp(480)) is False, "not keyed: nothing reaches the codec"
    assert rig.set_ptt(True, Lease("packet", seconds=30))
    assert rig.send_audio(ramp(480, 5))
    be.pump(0.01)
    out = be.stream("FTX-1 USB Audio", output=True)
    assert bytes(out.played[:960]) == ramp(480, 5)
    rig.tx_end()
    rig.set_ptt(False)
    assert out.closed, "unkeying lets go of the output"


def test_the_po_meter_rises_after_the_key_as_the_fake_is_told(radio, rigs):
    assert cat.read_meter("po") == "RM5;" and cat.parse_meter("RM5128000;") == (5, 128)
    with pytest.raises(cat.CatError):
        cat.parse_meter("RM5128;")
    radio.rf_rise_s = 0.2
    rig = rigs(radio.device, allow_ptt=True)
    assert until(lambda: rig.state["freq"])
    rig.set_freq(144_800_000)
    assert cat.parse_meter(rig.ask("RM5;", "RM5"))[1] == 0
    rig.set_ptt(True, Lease("packet", seconds=30))
    assert until(lambda: radio.tx_state == 1)
    assert cat.parse_meter(rig.ask("RM5;", "RM5"))[1] == 0, "not up to power yet"
    time.sleep(0.25)
    assert cat.parse_meter(rig.ask("RM5;", "RM5"))[1] == radio.rf_power
