"""The engine on the fast modem: the channel's baud picks it (modem.py),
the airtime sums follow the baud, and connected mode works through the
real G3RUH modulator and receiver on a simulated channel."""

from __future__ import annotations

import pytest
from stations import World, accept_all

from shack_packet import fsk
from shack_packet.channel import ChannelConfig, airtime_s, frame_airtime_s
from shack_packet.datalink import LinkConfig
from shack_packet.engine import Engine
from shack_packet.errors import PacketError
from shack_packet.identity import Station
from shack_packet.sim import Medium, SimClock


def _payload(n):
    return bytes((i * 7 + i // 256) % 256 for i in range(n))


def test_airtime_follows_the_baud():
    frame = b"\x01" * 200
    assert frame_airtime_s(frame, 9600) == pytest.approx(frame_airtime_s(frame) / 8)
    # The frames' bits take a 32nd of the time; TXDELAY and the tail are
    # times, the same at any speed.
    assert airtime_s([frame] * 3, 0.1, 0.01, None, 38_400) == pytest.approx(
        airtime_s([frame] * 3, 0, 0) / 32 + 0.11, rel=0.01)


def test_the_channel_takes_the_links_baud_and_refuses_a_mismatch():
    clock = SimClock()
    fast = Medium(clock, "audio", rate=76_800, baud=9600).radio("A")
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), fast, clock=clock)
    assert eng.channel.baud == 9600 and isinstance(eng.receiver, fsk.Receiver)
    slow = Medium(clock, "audio").radio("B")
    with pytest.raises(PacketError):
        Engine(Station.of("GM4BRI"), slow, channel=ChannelConfig(baud=9600), clock=clock)
    with pytest.raises(PacketError):
        ChannelConfig(baud=4800)


@pytest.mark.parametrize("baud", [9600, 38_400])
def test_a_transfer_through_the_fsk_modem_with_noise(baud):
    """Audio mode at the modem's own rate: the G3RUH modulator, white noise,
    the receiver and its DCD. 6 KB in eight-frame transmissions, which the
    airtime budget allows only because it counts at this baud."""
    world = World(mode="audio", snr_db=15.0, seed=5, rate=baud * 8, baud=baud)
    cfg = ChannelConfig(txdelay_s=0.05, txtail_s=0.01)
    link = LinkConfig(k=7)
    a = world.station("G0ABC", channel=cfg, link=link)
    b = world.station("GM4BRI", channel=cfg, link=link)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected)
    data = _payload(6000)
    start = world.clock()
    conn.send(data)
    _, got, _ = accepted[0]
    assert world.run(120, lambda: len(got) >= len(data))
    assert bytes(got) == data
    elapsed = world.clock() - start
    assert len(data) * 8 / elapsed > (2000 if baud == 9600 else 4000), elapsed
    assert isinstance(b.receiver, fsk.Receiver) and b.receiver.frames > 0
    assert a.channel.stats.frames / a.channel.stats.transmissions > 3
