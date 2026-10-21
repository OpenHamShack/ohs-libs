"""Connected mode between engines on the simulated channel."""

from __future__ import annotations

import random

import pytest
from stations import World, accept_all, collect

from shack_packet.datalink import LinkConfig, State



def test_connect_send_and_disconnect():
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    got_a, events_a = collect(conn)
    assert world.run(30, lambda: conn.connected)
    assert accepted and accepted[0][0].connected
    conn.send(b"hello from G0ABC\r")
    far, got_b, _ = accepted[0]
    assert world.run(30, lambda: bytes(got_b) == b"hello from G0ABC\r")
    far.send(b"and back\r")
    assert world.run(30, lambda: bytes(got_a) == b"and back\r")
    conn.close()
    assert world.run(30, lambda: conn.state is State.DISCONNECTED and far.state is State.DISCONNECTED)
    assert [e.reason for e in events_a] == ["connected", "closed"]


def _payload(size, seed=5):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


def _chatter(world, station, every_s=20.0):
    """A third station beaconing on a schedule, so the transfer meets
    collisions as it would on 144.800."""
    due = [world.clock.now]

    def tick():
        if world.clock.now >= due[0]:
            station.send_ui("APRS", ">on frequency " + "x" * 60)
            due[0] += every_s
    return tick


@pytest.mark.parametrize("modulo", [8, 128])
def test_100_kb_arrives_intact_through_loss_and_collisions(modulo):
    world = World(loss=0.05, seed=modulo)
    a = world.station("G0ABC", link=LinkConfig(modulo=modulo))
    b = world.station("GM4BRI", link=LinkConfig(modulo=modulo))
    c = world.station("M0XYZ")
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    _, events = collect(conn)
    assert world.run(60, lambda: conn.connected)
    assert conn.modulo == modulo
    data = _payload(100_000)
    started = world.clock.now
    conn.send(data)
    far, got, _ = accepted[0]
    chatter = _chatter(world, c)

    def done():
        chatter()
        return len(got) >= len(data)
    assert world.run(4000, done), f"{len(got)} of {len(data)} bytes"
    assert bytes(got) == data
    elapsed = world.clock.now - started
    # Measured 18/10/2026 (seed-dependent): about 650 bit/s at modulo 8 and
    # 570 at 128, on 1200 baud with 5 % frame loss and a beacon every 20 s.
    assert len(data) * 8 / elapsed > 400
    assert world.medium.collisions > 0 and world.medium.lost > 0, "nothing was put right"
    assert conn.link.stats["retransmitted"] > 0
    assert conn.connected and [e.reason for e in events] == ["connected"]


def test_a_transfer_through_the_real_modem_with_noise_and_a_third_station():
    """Audio mode: the modulator, white noise at 12 dB, the receiver and its
    DCD, and C's beacons colliding with the transfer for real."""
    world = World(mode="audio", snr_db=12.0, seed=3)
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    c = world.station("M0XYZ")
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected)
    data = _payload(3000)
    conn.send(data)
    far, got, _ = accepted[0]
    chatter = _chatter(world, c, every_s=15.0)

    def done():
        chatter()
        return len(got) >= len(data)
    assert world.run(300, done)
    assert bytes(got) == data
    assert b.receiver.frames > 0 and c.receiver.frames > 0


def test_nobody_answering_times_out_after_n2_tries():
    world = World()
    a = world.station("G0ABC", link=LinkConfig(n2=3))
    conn = a.connect("GM4BRI")
    _, events = collect(conn)
    assert world.run(300, lambda: conn.state is State.DISCONNECTED)
    assert [e.reason for e in events] == ["timeout"]
    assert conn.link.stats["sent_SABM"] == 4                # the first and three retries


def test_a_station_not_listening_refuses_with_dm():
    world = World()
    a = world.station("G0ABC")
    world.station("GM4BRI")                                 # listens to nobody
    conn = a.connect("GM4BRI")
    _, events = collect(conn)
    assert world.run(30, lambda: conn.state is State.DISCONNECTED)
    assert [e.reason for e in events] == ["refused"]


def test_a_reader_that_stops_reading_holds_the_sender_with_rnr():
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI", link=LinkConfig(rx_limit=2000))
    held = []
    b.listen("GM4BRI", lambda call: held.append)            # no on_data: B buffers
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected)
    data = _payload(12_000)
    conn.send(data)
    far = held[0]
    assert world.run(300, lambda: conn.link.peer_busy)
    buffered = len(far._rx)
    world.run(60)                                            # B still not reading
    assert len(far._rx) == buffered, "the sender did not stop for RNR"
    assert buffered <= 2000 + 4 * 256
    assert conn.connected and far.connected
    got = bytearray(far.read())
    assert world.run(600, lambda: (got.extend(far.read()) or len(got) >= len(data)))
    assert bytes(got) == data
    assert far.link.stats["sent_RNR"] > 0


def test_an_idle_link_is_checked_by_t3_and_stays_up():
    world = World()
    a = world.station("G0ABC", link=LinkConfig(t3_s=60))
    b = world.station("GM4BRI")
    accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected)
    world.run(200)
    assert conn.connected
    assert conn.link.stats["sent_RR"] >= 2                   # the enquiries


def test_a_link_whose_peer_vanishes_times_out_from_timer_recovery():
    world = World()
    a = world.station("G0ABC", link=LinkConfig(n2=4))
    b = world.station("GM4BRI")
    accept_all(b)
    conn = a.connect("GM4BRI")
    _, events = collect(conn)
    assert world.run(30, lambda: conn.connected)
    b.station.tx_enabled = False                             # B goes off the air
    conn.send(b"anyone there?\r")
    assert world.run(600, lambda: conn.state is State.DISCONNECTED)
    assert [e.reason for e in events] == ["connected", "timeout"]
    assert b.channel.stats.refused["transmit-disabled"] > 0


def test_modulo_128_negotiates_with_xid():
    world = World()
    a = world.station("G0ABC", link=LinkConfig(modulo=128, k=16))
    b = world.station("GM4BRI", link=LinkConfig(modulo=128, k=6, n1_rx=128))
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected and conn.link.stats["xid_applied"])
    assert conn.modulo == 128 and accepted[0][0].modulo == 128
    # A sends no more than B said it could take.
    assert conn.link.k == 6 and conn.link.n1 == 128
    data = _payload(3000)
    conn.send(data)
    assert world.run(300, lambda: len(accepted[0][1]) >= len(data))
    assert bytes(accepted[0][1]) == data


def test_sabme_to_a_v20_station_falls_back_to_sabm():
    world = World()
    a = world.station("G0ABC", link=LinkConfig(modulo=128))
    b = world.station("GM4BRI", link=LinkConfig(v22=False))
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    _, events = collect(conn)
    assert world.run(120, lambda: conn.connected)
    assert conn.modulo == 8 and accepted[0][0].modulo == 8
    assert [e.reason for e in events] == ["fallback-v20", "connected"]


def test_nothing_is_keyed_without_transmit_enabled():
    world = World()
    a = world.station("G0ABC", tx=False, link=LinkConfig(n2=2))
    b = world.station("GM4BRI")
    accept_all(b)
    refusals = []
    a.on_event = lambda e: refusals.append(e.reason) if e.kind == "tx-refused" else None
    conn = a.connect("GM4BRI")
    assert world.run(120, lambda: conn.state is State.DISCONNECTED)
    assert a.radio.keyed_at is None, "the radio was keyed"
    assert set(refusals) == {"transmit-disabled"}
    assert conn.reason == "timeout"


def test_disarming_mid_transfer_stops_at_the_next_keying():
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected)
    conn.send(_payload(20_000))
    assert world.run(60, lambda: len(accepted[0][1]) > 2000)
    a.station.tx_enabled = False
    keyed = []
    a.channel.on_transmit = lambda start, end, frames: keyed.append(start)
    world.run(60)
    assert not keyed
    assert a.channel.stats.refused["transmit-disabled"] > 0


def test_packets_longer_than_n1_are_segmented_and_reassembled():
    """AX.25 2.2 §6.6, PID 0x08: what direwolf does with anything longer
    than its PACLEN, and what IP in virtual-circuit mode needs for an MTU
    above N1."""
    world = World(loss=0.05, seed=9)
    a = world.station("G0ABC", link=LinkConfig(n1=100))
    b = world.station("GM4BRI")
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected)
    packets = []
    accepted_conn = accepted[0][0]
    accepted_conn.on_packet = lambda pid, data: packets.append((pid, data))
    text = _payload(950, seed=2)
    assert conn.send_packet(0xCC, text)
    assert conn.send_packet(0xCC, b"short")
    assert world.run(300, lambda: len(packets) == 2)
    assert packets == [(0xCC, text), (0xCC, b"short")]
    assert conn.link.stats["sent_I"] >= 11
    # Segmented plain text goes into the stream like any other.
    conn.send_packet(0xF0, b"segmented text " * 20)
    assert world.run(300, lambda: bytes(accepted[0][1]) == b"segmented text " * 20)


def test_a_slow_radio_gets_a_longer_txdelay_from_its_measured_turnaround():
    """A radio that takes 450 ms to come up to power, with TXDELAY set to
    300: the first flags go out before there is any RF. Told the measured
    turnaround, the channel stretches TXDELAY and the link works; told
    nothing (a link that does not measure), every frame is lost."""
    for told, works in ((None, True), (0.0, False)):
        world = World()
        a = world.station("G0ABC", link=LinkConfig(n2=2))
        world.medium.radios[-1].rise_s = 0.45
        world.medium.radios[-1].turnaround_s = 0.45 if told is None else told
        b = world.station("GM4BRI")
        accept_all(b)
        conn = a.connect("GM4BRI")
        world.run(120, lambda: conn.state in (State.CONNECTED, State.DISCONNECTED))
        assert conn.connected is works
        assert (a.channel.txdelay_s > 0.5) is works
