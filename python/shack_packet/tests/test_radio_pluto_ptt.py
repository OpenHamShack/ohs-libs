"""PlutoLink's PTT output and amplifier cap (radios/pluto.py, 18/10/2026),
against the fake libiio's register file: the GPO comes on before any RF
and goes off after it, by the lead and the tail; every quiet drops it; the
gate touches no other bit; the firmware mode keys the LO at the quiet gain
first; and no gain above an amplifier's cap is ever written."""

from __future__ import annotations

import time

import numpy as np
import pytest
from radiohelp import until
from sdrhelp import FakePluto
from test_fsk_iq import FloatSink

from shack_packet import Station, fsk
from shack_packet.afsk import pcm16
from shack_packet.errors import PacketError
from shack_packet.radios import LinkRefused, PlutoLink
from shack_packet.sdr.pluto import TX_ATTEN_MAX

ARMED = Station.of("GM4BRI", tx_enabled=True)


def make(radio=None, **kw):
    radio = radio or FakePluto()
    radio.rx_realtime = True
    link = PlutoLink("ip:192.168.2.1", 432_650_000, iio_module=radio.module(), baud=9600, allow_unguarded=True,
                     **kw)
    link.attach(FloatSink(ARMED))
    return radio, link


def over(link):
    bb = fsk.modulate([b"\x82\xa0\xa4\xa6@@`\x8e\x9ah\x84\xa4\x92a\x03\xf0ptt"], link.rate, baud=9600,
                      txdelay_ms=30, tail_ms=5)
    link.key()
    link.send_pcm(pcm16(bb))
    link.unkey()
    assert until(lambda: not link.keyed, 10)


def first(timeline, pred):
    return next(t for t, *rest in timeline if pred(*rest))


def test_the_gpo_brackets_the_rf_by_the_lead_and_the_tail():
    radio, link = make(ptt_output="gpo1", relay_lead_ms=40, relay_tail_ms=30)
    with link:
        assert radio.regs[0x26] == 0x90                  # manual mode added to what was there
        state = link.gpo_state()
        assert state["manual"] and not state["gpo1"]
        radio.timeline.clear()
        over(link)
        assert radio.tx_quiet() and radio.regs[0x27] == 0x00
    tl = radio.timeline
    gpo_on = first(tl, lambda what, reg, v: what == "reg" and reg == "0x27" and int(v, 16) & 0x20)
    lo_up = first(tl, lambda what, name, v: what == "tx_lo" and name == "powerdown" and v == "0")
    lo_down = first(tl, lambda what, name, v: what == "tx_lo" and name == "powerdown" and v == "1")
    gpo_off = first(tl, lambda what, reg, v: what == "reg" and reg == "0x27" and t_after(v, 0x20))
    assert lo_up - gpo_on >= 0.04 and gpo_off - lo_down >= 0.03, (lo_up - gpo_on, gpo_off - lo_down)
    # Nothing but our bit was ever written to 0x27, and 0x26 went back as found.
    assert all(int(v, 16) & ~0x20 == 0 for _t, what, reg, v in tl if what == "reg" and reg == "0x27")
    assert radio.regs[0x26] == 0x80


def t_after(v, bit):
    return not int(v, 16) & bit


def test_every_quiet_drops_the_gpo_and_the_gate_touches_no_other_bit():
    radio, link = make(ptt_output="gpo2")
    with link:
        radio.regs[0x27] = 0x10                          # a firmware's GPO0 is up: not ours to touch
        link.key()
        assert radio.regs[0x27] == 0x50
        link._emergency_unkey()                          # the watchdog's path: no tail, at once
        assert radio.regs[0x27] == 0x10 and radio.tx_quiet()
        with pytest.raises(PacketError):
            link._reg_write(0x27, 0x00)                  # would clear GPO0
        with pytest.raises(PacketError):
            link._reg_write(0x24, 0x00)                  # not a PTT register at all
        link.unkey()


def test_no_debugfs_no_gpo_output_and_a_guard_must_know_about_it():
    radio = FakePluto()
    radio.no_debugfs = True
    _r, link = make(radio, ptt_output="gpo0")
    with pytest.raises(LinkRefused):
        link.open()

    class Guard:                                         # a guard not told about the GPO
        confirmed = True
        gpo = False

        def start(self):
            return self

        def stop(self):
            pass
    radio, link = make(ptt_output="gpo0", guard=Guard())
    with link:
        assert not link.tx_guarded and "guard-no-gpo" in link.unguarded_why
    with pytest.raises(LinkRefused):
        make(ptt_output="gpio7")


def test_firmware_ptt_keys_the_lo_at_the_quiet_gain_and_raises_it_after_the_lead():
    radio, link = make(ptt_output="firmware", relay_lead_ms=40, relay_tail_ms=30, tx_gain_db=-20)
    assert link.turnaround_s == pytest.approx(PlutoLink.DEFAULT_TURNAROUND_S + 0.04)
    with link:
        radio.timeline.clear()
        over(link)
        assert radio.tx_quiet()
        tl = radio.timeline
        lo_up = first(tl, lambda what, name, v: what == "tx_lo" and name == "powerdown" and v == "0")
        gain_up = first(tl, lambda what, name, v: what == "tx" and name == "hardwaregain" and v == "-20.0")
        lo_down = first(tl, lambda what, name, v: what == "tx_lo" and name == "powerdown" and v == "1")
        assert gain_up - lo_up >= 0.04
        downs = [t for t, what, name, v in tl if what == "tx" and name == "hardwaregain" and float(v) == TX_ATTEN_MAX
                 and t > gain_up]
        assert downs and lo_down - downs[0] >= 0.03
        # A quiet during the lead: the gain never goes up.
        radio.timeline.clear()
        link.key()
        link._quiet()
        time.sleep(0.08)
        assert not [v for _t, what, name, v in radio.timeline if what == "tx" and v == "-20.0"]
        link.unkey()


def test_an_amplifiers_drive_cap_is_never_exceeded():
    with pytest.raises(LinkRefused):
        make(max_tx_gain_db=-20.0, tx_gain_db=-10.0)
    radio, link = make(max_tx_gain_db=-20.0, tx_gain_db=-25.0)
    with link:
        over(link)
        gains = [float(v) for _t, what, name, v in radio.timeline if what == "tx" and name == "hardwaregain"]
        assert max(gains) <= -20.0
        link.tx_allowed[("tx", "hardwaregain")].add("-10.0")   # even if the allowed set were wrong
        with pytest.raises(PacketError):
            link._write("tx", "hardwaregain", -10.0)

    class Guard:                                         # a guard without the cap
        confirmed = True
        gpo = False
        max_gain_db = None

        def start(self):
            return self

        def stop(self):
            pass
    radio, link = make(max_tx_gain_db=-20.0, tx_gain_db=-25.0, guard=Guard())
    with link:
        assert not link.tx_guarded and "guard-no-cap" in link.unguarded_why


def test_a_pluto_shared_by_the_controller_is_reached_on_its_own_ports():
    _r, link = make()
    assert (link.uri, link.ssh_port) == ("ip:192.168.2.1", None)
    radio = FakePluto()
    link = PlutoLink("pluto://ohs-box?iiod=30431&ssh=30422", 432_650_000, iio_module=radio.module(),
                     allow_unguarded=True)
    assert (link.uri, link.ssh_port) == ("ip:ohs-box", 30422)
    link = PlutoLink("pluto://box?iiod=1234", 432_650_000, iio_module=radio.module(), allow_unguarded=True)
    assert link.uri == "ip:box:1234"
    assert np.isfinite(link.frequency)


def test_a_firmware_script_moving_the_register_pointer_is_not_believed():
    radio = FakePluto()
    radio.regs[0x27] = 0x81
    radio.pointer_stolen = 1                             # the first read of 0x26 answers 0x27's value
    radio, link = make(radio, ptt_output="gpo1")
    with link:
        assert radio.regs[0x26] == 0x90                  # 0x80 | manual, not 0x81 | manual
        assert link.gpo_state()["manual"]


def test_tezuka_owns_its_gpos():
    radio = FakePluto()
    radio.fw_version = "tezuka-v0.3.21"
    _r, link = make(radio, ptt_output="gpo1")
    with pytest.raises(LinkRefused):
        link.open()
    _r, link = make(radio, ptt_output="firmware")
    with link:
        assert link.tx_quiet
