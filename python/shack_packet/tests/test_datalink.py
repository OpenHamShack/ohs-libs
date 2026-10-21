"""The data link's less common paths, against a scripted peer: frames are
put straight into an engine and what it queues is read back."""

from __future__ import annotations

from shack_packet import ax25, xid
from shack_packet.ax25 import Control, Frame, FrameType
from shack_packet.datalink import LinkConfig, State
from shack_packet.engine import Engine
from shack_packet.identity import Station
from shack_packet.sim import SimClock


class Script:
    """An engine with no radio: frames in by hand, frames out read from the
    channel's queue."""

    def __init__(self, modulo=8, **link):
        self.clock = SimClock()
        self.engine = Engine(Station.of("GM4BRI", tx_enabled=True), None, clock=self.clock,
                             link=LinkConfig(modulo=modulo, **link))
        self.modulo = modulo
        self.events = []
        self.engine.on_event = self.events.append
        self.accepted = []
        self.engine.listen("GM4BRI", lambda call: self.accepted.append)

    def peer(self, control: Control, info=b"", command=True, pid=None):
        frame = Frame.build("G0ABC", "GM4BRI", control, info, pid=pid, command=command)
        self.engine.frame_in(frame.encode())

    def out(self, modulo=None):
        """Everything queued since last asked, links' frames included."""
        ch = self.engine.channel
        frames = [o.frame for o in list(ch._urgent) + list(ch._normal)]
        ch._urgent.clear()
        ch._normal.clear()
        frames += [o.frame for o in self.engine._link_frames(self.clock.now, 50)]
        return [ax25.parse(f, modulo or self.modulo) for f in frames]

    def connect_from_peer(self):
        self.peer(Control(FrameType.SABME if self.modulo == 128 else FrameType.SABM, True))
        [ua] = self.out()
        assert ua.type is FrameType.UA and ua.control.pf and ua.command is False
        return self.accepted[0]


def i(ns, nr, modulo=8, pf=False):
    return Control(FrameType.I, pf, nr, ns, modulo)


def s(kind, nr, modulo=8, pf=False):
    return Control(kind, pf, nr, None, modulo)


def test_out_of_sequence_brings_one_rej_then_silence_until_in_sequence():
    t = Script()
    conn = t.connect_from_peer()
    got = bytearray()
    conn.on_data = got.extend
    t.peer(i(0, 0), b"zero", pid=0xF0)
    t.peer(i(2, 0), b"two", pid=0xF0)              # 1 was lost
    t.peer(i(3, 0), b"three", pid=0xF0)
    out = t.out()
    assert [(f.type, f.control.nr) for f in out] == [(FrameType.REJ, 1)]
    t.peer(i(1, 0), b"one", pid=0xF0)
    t.peer(i(2, 0), b"two", pid=0xF0)
    assert bytes(got) == b"zeroonetwo"
    assert [(f.type, f.control.nr) for f in t.out()] == [(FrameType.RR, 3)]


def test_a_poll_is_answered_at_once_with_the_final_bit():
    t = Script()
    t.connect_from_peer()
    t.peer(s(FrameType.RR, 0, pf=True))
    [rr] = t.out()
    assert rr.type is FrameType.RR and rr.control.pf and rr.command is False


def test_frmr_re_establishes_the_link():
    t = Script()
    conn = t.connect_from_peer()
    t.peer(Control(FrameType.FRMR, False), b"\x00\x00\x00", command=False)
    [sabm] = t.out()
    assert sabm.type is FrameType.SABM and conn.state is State.AWAITING_CONNECTION
    assert any(e.reason == "reset-frmr" for e in t.events)
    t.peer(Control(FrameType.UA, True), command=False)
    assert conn.connected


def test_srej_resends_just_the_frame_it_names():
    t = Script()
    conn = t.connect_from_peer()
    conn.send(b"a" * 10)
    conn.send(b"b" * 10)
    conn.send(b"c" * 10)
    sent = t.out()
    assert [f.control.ns for f in sent] == [0, 1, 2]
    t.peer(s(FrameType.SREJ, 1), command=False)
    [again] = t.out()
    assert again.type is FrameType.I and again.control.ns == 1 and again.info == b"b" * 10


def test_a_bad_nr_resets_the_link():
    t = Script()
    conn = t.connect_from_peer()
    t.peer(s(FrameType.RR, 5), command=False)       # acknowledges frames never sent
    assert t.out()[0].type is FrameType.SABM
    assert any(e.reason == "reset-nr" for e in t.events) and conn.state is State.AWAITING_CONNECTION


def test_dm_disc_and_a_reset_from_the_peer():
    t = Script()
    conn = t.connect_from_peer()
    t.peer(Control(FrameType.SABM, True))           # the peer starts again
    assert t.out()[0].type is FrameType.UA and conn.connected
    assert any(e.reason == "reset-by-peer" for e in t.events)
    t.peer(Control(FrameType.DM, False), command=False)
    assert conn.state is State.DISCONNECTED and conn.reason == "dm-received"
    t2 = Script()
    conn2 = t2.connect_from_peer()
    t2.peer(Control(FrameType.DISC, True))
    [ua] = t2.out()
    assert ua.type is FrameType.UA and ua.control.pf and conn2.reason == "closed-by-peer"
    t2.peer(Control(FrameType.DISC, True))           # no link now: DM
    assert t2.out()[0].type is FrameType.DM


def test_xid_and_test_without_a_connection():
    t = Script()
    theirs = xid.Parameters(window_rx=2, n1_rx=64, modulo=128).encode()
    t.peer(Control(FrameType.XID, True), theirs)
    [answer] = t.out()
    assert answer.type is FrameType.XID and answer.command is False and answer.control.pf
    ours = xid.decode(answer.info)
    assert ours.rej and not ours.srej and ours.half_duplex
    t.peer(Control(FrameType.TEST, True), b"echo me")
    [echo] = t.out()
    assert echo.type is FrameType.TEST and echo.info == b"echo me"


def test_modulo_128_sequence_numbers_wrap():
    t = Script(modulo=128)
    conn = t.connect_from_peer()
    assert conn.modulo == 128
    got = bytearray()
    conn.on_data = got.extend
    for n in range(130):
        t.peer(i(n % 128, 0, 128), bytes([n % 256]), pid=0xF0)
        if n % 7 == 6:
            assert t.out()[-1].control.nr == (n + 1) % 128
    assert bytes(got) == bytes(n % 256 for n in range(130))


def test_rnr_holds_our_i_frames_until_rr():
    t = Script()
    conn = t.connect_from_peer()
    t.peer(s(FrameType.RNR, 0), command=False)
    conn.send(b"held")
    assert t.out() == []
    assert any(e.reason == "peer-busy" for e in t.events)
    t.peer(s(FrameType.RR, 0), command=False)
    [frame] = t.out()
    assert frame.type is FrameType.I and frame.info == b"held"


def _srej_link(srej_ours=True, srej_theirs=True, k=16):
    t = Script(modulo=128, k=k, srej=srej_ours)
    conn = t.connect_from_peer()
    t.peer(Control(FrameType.XID, True), xid.Parameters(modulo=128, window_rx=k, srej=srej_theirs).encode())
    [answer] = t.out(128)
    return t, conn, xid.decode(answer.info)


def test_with_srej_frames_out_of_sequence_are_held_and_the_missing_ones_named():
    t, conn, ours = _srej_link()
    assert ours.srej and conn.link.srej
    got = bytearray()
    conn.on_data = got.extend
    for ns in (0, 2, 3, 5):                         # 1 and 4 lost
        t.peer(i(ns, 0, 128), b"%d" % ns, pid=0xF0)
    out = t.out(128)
    # The RR acknowledges what arrived in order; an SREJ each for the gaps.
    assert [(f.type, f.control.nr) for f in out] == [(FrameType.RR, 1), (FrameType.SREJ, 1), (FrameType.SREJ, 4)]
    assert bytes(got) == b"0"
    t.peer(i(1, 0, 128), b"1", pid=0xF0)
    assert bytes(got) == b"0123"                   # 2 and 3 were held
    t.peer(i(4, 0, 128), b"4", pid=0xF0)
    t.peer(i(3, 0, 128), b"3", pid=0xF0)            # a copy: dropped
    assert bytes(got) == b"012345"
    assert [(f.type, f.control.nr) for f in t.out(128)] == [(FrameType.RR, 6)]


def test_srej_is_used_only_when_both_offer_it_and_the_window_allows():
    for ours, theirs, k, used in ((True, False, 16, False), (False, True, 16, False), (True, True, 64, True),
                                  (True, True, 65, False)):
        t = Script(modulo=128, k=k, srej=ours)
        conn = t.connect_from_peer()
        t.peer(Control(FrameType.XID, True), xid.Parameters(modulo=128, window_rx=127, srej=theirs).encode())
        [answer] = t.out(128)
        assert xid.decode(answer.info).srej is (ours and k <= 64)
        assert conn.link.srej is used, (ours, theirs, k)
    # Without it, out of sequence is still REJ.
    t, conn, _ = _srej_link(srej_ours=False)
    t.peer(i(0, 0, 128), b"0", pid=0xF0)
    t.peer(i(2, 0, 128), b"2", pid=0xF0)
    assert [(f.type, f.control.nr) for f in t.out(128)] == [(FrameType.REJ, 1)]


def test_srej_on_a_lossy_channel_resends_less_and_delivers_everything_in_order():
    """Two engines on the simulated channel at 38 400 baud, k 32, with bit
    errors: the same transfer with SREJ needs fewer retransmissions and
    less time than with REJ, and arrives intact either way."""
    import random

    from shack_packet.channel import ChannelConfig
    from shack_packet.sim import Medium, Simulation

    def transfer(srej):
        clock = SimClock()
        medium = Medium(clock, "frames", baud=38_400, ber=3e-5, seed=4, min_lock_s=0.0025)
        sim = Simulation(clock, medium)
        cfg = ChannelConfig(txdelay_s=0.05, txtail_s=0.01, persist=255, max_frames=33, baud=38_400)
        link = LinkConfig(modulo=128, k=32, n1=256, srej=srej)
        a, b = (Engine(Station.of(c, tx_enabled=True), medium.radio(c), channel=cfg, link=link, clock=clock,
                       rng=random.Random(c)) for c in ("G0ABC", "GM4BRI"))
        sim.add(a)
        sim.add(b)
        got = bytearray()
        b.listen("GM4BRI", lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
        conn = a.connect("GM4BRI")
        assert sim.run(30, lambda: conn.connected)
        data = random.Random(1).randbytes(40_000)
        start = clock()
        conn.send(data)
        assert sim.run(300, lambda: len(got) >= len(data))
        assert bytes(got) == data
        # The receiver's flag: it is the one that holds and names frames.
        [theirs] = b.connections.values()
        return clock() - start, conn.link.stats["retransmitted"], theirs.link.srej

    t_rej, re_rej, used = transfer(False)
    assert not used
    t_srej, re_srej, used = transfer(True)
    assert used and re_srej < re_rej / 2 and t_srej < t_rej, (t_rej, re_rej, t_srej, re_srej)
