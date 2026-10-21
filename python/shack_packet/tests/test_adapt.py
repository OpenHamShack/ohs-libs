"""Link adaptation (adapt.py, 18/10/2026): the ladder, the receiver's
evidence rules, the in-band request in XID, I frames at the data mode and
everything else at the floor, an adaptive Pluto link sending both in one
over and hearing both through its per-mode receivers, and the simulated
channel's comparison against the fixed top and bottom modes."""

from __future__ import annotations

import numpy as np
import pytest
from radiohelp import until
from sdrhelp import FakePluto
from test_datalink import Script

from shack_packet import Station, adapt, ax25, fsk_bench, modem, xid
from shack_packet.ax25 import Control, Frame, FrameType
from shack_packet.channel import ChannelConfig, RadioLink
from shack_packet.datalink import LinkConfig
from shack_packet.engine import Engine
from shack_packet.errors import PacketError
from shack_packet.radios import LinkRefused, PlutoLink
from shack_packet.radios.pluto import FSK_RATE_IQ, WIDE_IFO_HZ
from shack_packet.radios.sdrrx import ModeBundle, ModePart

LADDER = adapt.ladder()
ARMED = Station.of("GM4BRI", tx_enabled=True)


def test_the_ladder_is_ordered_by_what_each_mode_needs():
    assert LADDER == ("4fsk76800", "gfsk76800", "g3ruh38400", "g3ruh19200", "g3ruh9600")
    needs = [adapt.threshold_dbhz(m) for m in LADDER]
    assert needs == sorted(needs, reverse=True)
    # 4fsk38400 needs more than gfsk76800 for the same bit rate: left out.
    assert adapt.threshold_dbhz("4fsk38400") > adapt.threshold_dbhz("gfsk76800")
    # 13 cm: gfsk153600 at the top; 4fsk76800 is less robust, so not below it.
    assert adapt.ladder("gfsk153600")[:2] == ("gfsk153600", "gfsk76800")
    assert adapt.ladder("gfsk76800", "g3ruh38400") == ("gfsk76800", "g3ruh38400")
    with pytest.raises(PacketError):
        adapt.ladder("g3ruh9600", "4fsk76800")
    with pytest.raises(PacketError):
        adapt.ladder("4fsk76800", "afsk1200")


def test_the_mode_request_rides_in_xid_and_others_skip_it():
    info = xid.Parameters(modulo=128, window_rx=63, ohs_modes=(7, 4)).encode()
    got = xid.decode(info)
    assert got.ohs_modes == (7, 4) and got.window_rx == 63
    assert modem.BY_CODE[7] == "4fsk76800" and modem.BY_CODE[4] == "gfsk76800"
    # Without it, nothing: and its PI is one 2.2 does not define.
    assert xid.decode(xid.Parameters(modulo=128).encode()).ohs_modes is None
    assert xid.PI_OHS_MODES not in (xid.PI_CLASSES, xid.PI_HDLC_OPTIONS, xid.PI_N1_RX, xid.PI_WINDOW_RX,
                                    xid.PI_ACK_TIMER, xid.PI_RETRIES)


def test_the_receiver_steps_down_on_level_loss_and_polls():
    a = adapt.RxAdapter(LADDER, 0.0)
    a.heard("g3ruh9600", 75.0, 1.0)                        # loud: stays at the top
    assert a.request == "4fsk76800"
    a.heard("g3ruh9600", 62.0, 2.0)                        # a fade: straight down, by level
    assert a.request == "gfsk76800" and a.stats["down_level"] == 1 and a.announce_pending
    b = adapt.RxAdapter(LADDER, 0.0)
    b.heard("4fsk76800", None, 1.0, data=True)
    b.burst(10, 0, 1.5)
    b.poll(3.0)                                            # nothing new since we acknowledged
    assert b.request == "gfsk76800" and b.stats["down_poll"] == 1
    b.heard("gfsk76800", None, 4.0, data=True)
    b.burst(6, 4, 4.5)                                     # 40 % missing
    assert b.request == "g3ruh38400" and b.stats["down_loss"] == 1
    # A burst still at a mode we have left says nothing about this one.
    b.heard("gfsk76800", None, 5.0, data=True)
    b.burst(1, 9, 5.5)
    assert b.request == "g3ruh38400"


def test_up_needs_a_clean_burst_and_the_level_and_a_failed_probe_waits_longer():
    a = adapt.RxAdapter(LADDER, 0.0)
    a.heard("g3ruh9600", 58.5, 0.0)                         # g3ruh38400 by level
    assert a.request == "g3ruh38400"
    a.heard("g3ruh38400", None, 0.5, data=True)
    for t in range(1, 8):                                  # data flowing: the level alone does not lift it
        a.heard("g3ruh38400", 74.0, 0.5 + t / 2)
    assert a.request == "g3ruh38400"
    a.heard("g3ruh38400", None, 4.5, data=True)
    a.burst(10, 0, 4.5)                                    # clean, and 74 dB-Hz clears the top by 3 dB
    assert a.request == "4fsk76800" and a.stats["up_clean"] == 1
    a.heard("4fsk76800", None, 5.0, data=True)
    a.burst(5, 5, 5.0)                                     # the probe failed by loss
    assert a.request == "gfsk76800" and a.up_level_needed == 2
    a.heard("gfsk76800", None, 5.5, data=True)
    a.burst(10, 0, 5.5)
    assert a.request == "gfsk76800"                        # one clean burst is no longer enough
    a.heard("gfsk76800", None, 6.0, data=True)
    a.burst(10, 0, 6.0)
    assert a.request == "4fsk76800"


def test_listening_covers_the_floor_the_request_the_next_down_and_the_last():
    a = adapt.RxAdapter(LADDER, 0.0)
    assert a.listening(0.0) == {"4fsk76800", "gfsk76800", "g3ruh9600"}
    a.heard("g3ruh9600", 58.5, 1.0)
    assert a.listening(1.0) == {"g3ruh38400", "g3ruh19200", "g3ruh9600", "4fsk76800"}
    assert "4fsk76800" not in a.listening(1.0 + adapt.LISTEN_GRACE_S + 1)
    a.heard("g3ruh38400", None, 2.0, data=True)
    assert "4fsk76800" not in a.listening(2.0)


def _adaptive_script():
    t = Script(modulo=128, k=32, srej=True, adapt=LADDER)
    conn = t.connect_from_peer()
    return t, conn


def test_i_frames_go_at_the_mode_asked_for_and_s_frames_at_the_floor():
    t, conn = _adaptive_script()
    link = conn.link
    assert link.tx_mode == "4fsk76800" and link.adapter.request == "4fsk76800"
    # The other station asks for gfsk76800 and offers 4fsk76800 at most.
    t.peer(Control(FrameType.XID, True), xid.Parameters(modulo=128, window_rx=32, srej=True,
                                                        ohs_modes=(7, 4)).encode())
    [answer] = t.out(128)
    assert answer.type is FrameType.XID and xid.decode(answer.info).ohs_modes == (7, 7)
    assert link.tx_mode == "gfsk76800" and link.peer_adapts
    conn.send(b"x" * 600)
    outs = t.engine._link_frames(t.clock.now, 50)
    assert {o.mode for o in outs} == {"gfsk76800"}
    t.peer(Control(FrameType.I, False, 0, 0, 128), b"data", pid=0xF0)
    outs = t.engine._link_frames(t.clock.now, 50)
    assert [(ax25.parse(o.frame, 128).type, o.mode) for o in outs] == [(FrameType.RR, None)]


def test_a_new_request_is_sent_until_answered():
    t, conn = _adaptive_script()
    link = conn.link
    link.adapter.heard("g3ruh9600", 60.0, t.clock.now)       # a fade: our request drops
    assert link.wants_to_send
    outs = t.engine._link_frames(t.clock.now, 50)
    [req] = [ax25.parse(o.frame, 128) for o in outs]
    assert req.type is FrameType.XID and req.command and xid.decode(req.info).ohs_modes[1] == modem.CODES[
        "g3ruh38400"]
    t.clock.now += adapt.ANNOUNCE_RETRY_S + 0.1
    link.poll(t.clock.now)                                 # the link's own timer, not the radio-less channel
    again = [f for f in t.out(128) if f.type is FrameType.XID]
    assert again and link.stats["sent_mode_request"] == 2
    t.peer(Control(FrameType.XID, True), xid.Parameters(modulo=128, ohs_modes=(7, 7)).encode(), command=False)
    assert link._announce_due is None


def test_slowing_down_resends_what_is_outstanding_at_once():
    t, conn = _adaptive_script()
    link = conn.link
    conn.send(b"y" * 2000)
    first = t.engine._link_frames(t.clock.now, 50)
    assert len(first) == 8 and link.vs == 8
    t.peer(Control(FrameType.XID, True), xid.Parameters(modulo=128, ohs_modes=(7, 3)).encode(), command=False)
    assert link.tx_mode == "g3ruh38400" and link.vs == link.va == 0
    again = t.engine._link_frames(t.clock.now, 50)
    assert again and {o.mode for o in again} == {"g3ruh38400"}


# -- the simulated channel ---------------------------------------------------------------


def transfer(modes, quality, kb=40, seed=1):
    rate, a, b = fsk_bench.adaptive_transfer(modes, quality, kb, seed)
    assert rate > 0, "the transfer did not finish intact"
    return rate, a, b


def test_on_an_asymmetric_channel_adaptation_beats_both_fixed_modes():
    """18/10's on-air case: the far end's direction cannot carry 4fsk76800
    (62 dB-Hz, ~1 in 5 short frames), the near end's can. Fixed at the top,
    the acknowledgements are lost and the sender sits in T1; fixed at the
    bottom, everything crawls; adaptive sends data at the top and
    acknowledgements at the floor."""
    q = fsk_bench.fading(74.0, 62.0)
    adaptive, a, b = transfer(LADDER, q)
    top, *_ = transfer(("4fsk76800",), q)
    bottom, *_ = transfer(("g3ruh9600",), q)
    assert adaptive > 1.3 * top and adaptive > 8 * bottom, (adaptive, top, bottom)
    assert a.link.tx_mode == "4fsk76800" and b.link.adapter.request == "4fsk76800"
    # The weak direction asked for less (it never carried data, so it
    # followed the level), and its acknowledgements went at the floor.
    assert a.link.adapter.request != "4fsk76800"


def test_through_fades_it_steps_down_and_back_up():
    rate, a, b = transfer(LADDER, fsk_bench.fading(74.0, 68.0, 8.0), kb=60)
    steps = b.link.adapter.stats
    assert steps["down"] >= 1 and steps["up"] >= 1, dict(steps)
    assert rate > 20_000


# -- an adaptive Pluto link ---------------------------------------------------------------


def _link(radio=None, realtime=True, **kw):
    radio = radio or FakePluto()
    radio.rx_realtime = realtime
    link = PlutoLink("ip:192.168.2.1", 1_299_525_000, iio_module=radio.module(), modes=LADDER,
                     allow_unguarded=True, **kw)
    return radio, link


def test_an_adaptive_pluto_link_is_set_up_for_its_ladder():
    _radio, link = _link()
    assert link.modes == LADDER and link.mode == "4fsk76800"
    assert link.rate == 614_400 and link.ifo_hz == WIDE_IFO_HZ and link.deviation_hz == 48_000
    eng = Engine(ARMED, link)
    assert eng.channel.adaptive and eng.link_config.adapt == LADDER and eng.receiver is None
    # A ladder whose top is not the fastest symbol rate cannot be sent at
    # one rate: gfsk76800 at 4fsk38400's 307.2 kHz is four samples a symbol.
    with pytest.raises(LinkRefused):
        PlutoLink("ip:192.168.2.1", 1_299_525_000, iio_module=FakePluto().module(),
                  modes=("4fsk38400", "gfsk76800"), allow_unguarded=True)


def test_one_over_carries_the_floor_then_the_data_mode_and_a_second_link_hears_both_in_order():
    radio, link = _link()
    eng = Engine(ARMED, link, channel=ChannelConfig(persist=255, txdelay_s=0.05))
    rr = Frame.build("GM4BRI", "G0ABC", Control(FrameType.RR, False, 5, None, 128), command=False).encode()
    data = [Frame.from_monitor(f"GM4BRI>G0ABC:>adaptive {i} " + "z" * 200).encode() for i in range(4)]
    with link:
        eng.channel.queue(rr)
        for f in data:
            eng.channel.queue(f, mode="4fsk76800")
        assert until(lambda: eng.poll() is not None and eng.channel.stats.transmissions >= 1, 5)
        assert until(lambda: (eng.poll(), not link.keyed and not eng.channel.transmitting)[1], 10)
        assert radio.tx_quiet()
    live = [i for i, (_d, on) in enumerate(radio.pushed) if on]
    keep = FakePluto()
    keep.pushed = radio.pushed[live[0]:live[-1] + 1]
    iq = keep.tx_iq()
    rng = np.random.default_rng(3)
    lead = FSK_RATE_IQ // 2
    noise = lambda k: rng.normal(0, 0.01, k) + 1j * rng.normal(0, 0.01, k)  # noqa: E731
    air = np.concatenate([noise(lead), iq + noise(len(iq)), noise(lead)])
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([air.real, air.imag]).ravel() * 2047).astype(np.int16)
    _r2, link2 = _link(rx, realtime=False)
    eng2 = Engine(Station.of("G0ABC", tx_enabled=False), link2)
    heard = []
    eng2.monitors.append(lambda f: heard.append(f.encode()))
    with link2:
        assert until(lambda: len(heard) >= 5, 30), heard
    assert heard == [rr] + data
    assert eng2.stats["heard_g3ruh9600"] == 1 and eng2.stats["heard_4fsk76800"] == 4


class _AdaptiveLink(RadioLink):
    """Just enough of an adaptive link for the engine: its ladder, and the
    modes it is told to demodulate. Its bundles are handed in by the test."""
    modes = ("gfsk76800", "g3ruh9600", "g3ruh19200")
    rate = 614_400
    deviation_hz = 48_000

    def __init__(self):
        self.listened = None

    def key(self):
        raise AssertionError("never keyed here")

    def unkey(self):
        pass

    def listen_modes(self, modes):
        self.listened = set(modes)


def _bundle(link, parts):
    seconds = len(parts[0][1]) / parts[0][2]
    return ModeBundle([ModePart(m, rate, x, 20.0, 1e5) for m, x, rate in parts], seconds, link.rate)


def test_a_mode_no_longer_listened_to_does_not_hold_the_channel_busy():
    # Half a frame at g3ruh19200 (a middle rung, listened to while a
    # connection wants it): its receiver's DCD is up when the next bundles
    # stop carrying that mode. The channel must read clear from the floor
    # and the top alone - it read busy for as long as 19200 stayed dropped.
    link = _AdaptiveLink()
    eng = Engine(ARMED, link)
    frame = Frame.from_monitor("G0ABC>GM4BRI:>" + "x" * 200).encode()
    m = modem.mode("g3ruh19200")
    rate = int(m.baud * 8)
    half = modem.modulate([frame], rate, mode=m, amplitude=0.5, txdelay_ms=50)
    half = half[:len(half) // 2]
    rng = np.random.default_rng(0)
    quiet = {name: (name, rng.normal(0, 0.3, int(modem.mode(name).baud * 8 * len(half) / rate)),
                    int(modem.mode(name).baud * 8)) for name in ("gfsk76800", "g3ruh9600")}
    eng.mode_in(_bundle(link, [quiet["gfsk76800"], quiet["g3ruh9600"], ("g3ruh19200", half, rate)]))
    assert eng.channel.dcd                          # a frame on the air: busy, rightly
    for _ in range(3):
        eng.mode_in(_bundle(link, [quiet["gfsk76800"], quiet["g3ruh9600"]]))
    assert not eng.channel.dcd and not eng.channel.busy
