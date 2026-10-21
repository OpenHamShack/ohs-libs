"""Channel access: p-persistence, busy, TXDELAY against the radio's
turnaround, the airtime limit, and the transmit switch at keying."""

from __future__ import annotations

import random

import numpy as np

from shack_packet import ax25, demod
from shack_packet.channel import MAX_BUSY_WAIT_S, PARTNER_MAX_WAIT_S, ChannelAccess, ChannelConfig, RadioLink
from shack_packet.identity import Station


class Recorder(RadioLink):
    rate = 12_000

    def __init__(self, turnaround_s=0.0):
        self.turnaround_s = turnaround_s
        self.log = []
        self.audio = []

    def key(self):
        self.log.append("key")

    def unkey(self):
        self.log.append("unkey")

    def send_pcm(self, pcm):
        self.log.append("pcm")
        self.audio.append(pcm)


def frame(text="hello"):
    return ax25.Frame.from_monitor(f"GM4BRI>APRS:{text}").encode()


def armed():
    return Station.of("GM4BRI", tx_enabled=True)


def test_waits_while_busy_then_draws_slots():
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=63), random.Random(1))
    ch.queue(frame())
    ch.dcd = True
    assert ch.poll(0.0) == MAX_BUSY_WAIT_S and link.log == []
    ch.dcd = False
    now, slots = 0.0, 0
    while "key" not in link.log:
        nxt = ch.poll(now)
        if "key" in link.log:
            break
        assert abs(nxt - now - 0.1) < 1e-9          # one SLOTTIME at a time
        now, slots = nxt, slots + 1
    assert slots < 30
    end = ch.poll(now)
    assert ch.poll(end) is None and link.log == ["key", "pcm", "unkey"]


class Draws:
    """An rng whose persistence draws are given: 255 never transmits at
    PERSIST 63, 0 always does."""

    def __init__(self, *draws):
        self.draws = list(draws)

    def randrange(self, n):
        return self.draws.pop(0)


def test_busy_stuck_on_is_waited_out_for_the_maximum_then_persistence_still_decides(caplog):
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=63, slottime_s=0.1), Draws(255, 255, 0, 0))
    ch.queue(frame("one"))
    ch.radio_busy = True                            # stuck on: nothing will ever clear it
    with caplog.at_level("WARNING", logger="shack_packet.channel"):
        assert ch.poll(0.0) == MAX_BUSY_WAIT_S      # woken at the give-up time, not left waiting for good
        assert ch.poll(MAX_BUSY_WAIT_S - 0.01) == MAX_BUSY_WAIT_S and link.log == []
        # From the give-up time the channel is gone through as if clear,
        # still one persistence draw a slot: two misses, then a hit.
        now = MAX_BUSY_WAIT_S
        assert abs(ch.poll(now) - (now + 0.1)) < 1e-9 and link.log == []
        assert abs(ch.poll(now + 0.1) - (now + 0.2)) < 1e-9 and link.log == []
        end = ch.poll(now + 0.2)
        assert link.log == ["key", "pcm"] and ch.stats.busy_overrides == 1
        # Busy still on: the next frame is not made to wait it out again,
        # but still takes its draw, and the warning is not repeated.
        ch.queue(frame("two"))
        assert ch.poll(end) > end and link.log == ["key", "pcm", "unkey", "key", "pcm"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "busy for 15 s without a break" in warnings[0]
    assert ch.stats.busy_overrides == 1


def test_the_wait_starts_again_once_busy_has_dropped():
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=255), random.Random(1))
    ch.queue(frame())
    ch.dcd = True
    assert ch.poll(0.0) == MAX_BUSY_WAIT_S
    ch.dcd = False                                  # a real station's over ends...
    ch.queue(frame("again"))
    ch.dcd = True                                   # ...and another starts before we were polled clear
    assert ch.poll(10.0) == MAX_BUSY_WAIT_S        # (not polled: the stretch is one)
    ch.dcd = False
    ch.poll(11.0)                                   # seen clear: transmits, the stretch is over
    assert link.log == ["key", "pcm"]
    end = 11.0 + ch.stats.airtime_s
    ch.dcd = True
    ch.queue(frame("third"))
    assert ch.poll(end) == end + MAX_BUSY_WAIT_S and link.log == ["key", "pcm", "unkey"]


def test_our_link_partners_over_is_waited_out_past_the_busy_cap():
    # A window of overs back to back from the station we are connected to
    # (k 63 at 9600: two 10 s overs): the RR waits for its end instead of
    # going into the second over at 15 s.
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=255), random.Random(1))
    ch.queue(frame("RR"))
    ch.dcd = True
    assert ch.poll(0.0) == MAX_BUSY_WAIT_S
    ch.partner_heard(10.0)                          # the first over's frames, decoded
    assert ch.poll(10.0) == PARTNER_MAX_WAIT_S
    assert ch.poll(MAX_BUSY_WAIT_S + 4.0) == PARTNER_MAX_WAIT_S and link.log == []
    assert ch.stats.busy_overrides == 0
    ch.dcd = False                                  # its over ends: ours goes at once
    ch.poll(20.5)
    assert link.log == ["key", "pcm"]


def test_the_partner_wait_is_bounded_and_other_stations_keep_the_cap():
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=255), random.Random(1))
    ch.queue(frame("RR"))
    ch.dcd = True
    ch.poll(0.0)
    ch.partner_heard(1.0)
    ch.poll(PARTNER_MAX_WAIT_S)                     # a squelch stuck open after it: gone through at last
    assert link.log == ["key", "pcm"] and ch.stats.busy_overrides == 1
    # A new stretch with nothing from the partner in it: the plain cap.
    end = PARTNER_MAX_WAIT_S + ch.stats.airtime_s
    ch.poll(end)
    ch.dcd = False
    ch.poll(end + 0.01)
    ch.dcd = True
    ch.queue(frame("again"))
    t = end + 1.0
    assert ch.poll(t) == t + MAX_BUSY_WAIT_S


def test_partner_heard_before_the_channel_is_polled_still_counts():
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=255), random.Random(1))
    ch.dcd = True
    ch.partner_heard(5.0)                           # decoded before anything wanted to send
    ch.queue(frame("RR"))
    assert ch.poll(19.0) == 5.0 + PARTNER_MAX_WAIT_S and link.log == []


def test_giving_up_on_busy_never_gets_past_the_transmit_switch():
    link = Recorder()
    ch = ChannelAccess(Station.of("GM4BRI", tx_enabled=False), link, ChannelConfig(persist=255))
    ch.queue(frame())
    ch.dcd = True
    ch.poll(0.0)
    ch.poll(MAX_BUSY_WAIT_S)
    assert link.log == [] and ch.stats.refused["transmit-disabled"] == 1 and ch.stats.busy_overrides == 1


def test_persistence_spreads_starts_over_slots():
    """p = (63+1)/256: about a quarter of the draws transmit."""
    starts = []
    for seed in range(400):
        ch = ChannelAccess(armed(), Recorder(), ChannelConfig(), random.Random(seed))
        ch.queue(frame())
        now = 0.0
        while not ch.transmitting:
            now = ch.poll(now) if not ch.transmitting else now
        starts.append(round(now - ch.stats.airtime_s + 0.0, 6))
    first_slot = sum(1 for s in starts if s < 0.05) / len(starts)
    assert 0.18 < first_slot < 0.32


def test_txdelay_is_never_shorter_than_the_radios_turnaround():
    link = Recorder(turnaround_s=0.5)
    ch = ChannelAccess(armed(), link, ChannelConfig(txdelay_s=0.2, persist=255))
    assert abs(ch.txdelay_s - 0.58) < 1e-9
    ch.queue(frame())
    ch.poll(0.0)
    audio = np.frombuffer(link.audio[0], dtype="<i2").astype(float) / 32768
    # The frame still decodes, after a preamble long enough for the radio.
    rx = demod.Receiver(12_000)
    assert [h.frame for h in rx.feed(audio) + rx.flush()] == [frame()]
    assert len(audio) / 12_000 > 0.58


def test_a_long_queue_is_split_at_the_airtime_limit():
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=255, max_tx_s=3.0, max_frames=50))
    for i in range(10):
        ch.queue(frame("x" * 200 + str(i)))
    end = ch.poll(0.0)
    assert end <= 3.0 and ch.stats.frames < 10
    while ch.pending() or ch.transmitting:
        end = ch.poll(end) or end
    assert ch.stats.frames == 10 and ch.stats.transmissions >= 4


def test_digipeated_frames_skip_the_persistence_draw():
    link = Recorder()
    ch = ChannelAccess(armed(), link, ChannelConfig(persist=0), random.Random(3))
    ch.queue(frame(), urgent=True)
    ch.poll(0.0)
    assert link.log[:2] == ["key", "pcm"]


def test_refused_at_keying_without_a_callsign_or_when_disarmed():
    for station, reason in ((Station(), "no-callsign"), (Station.of("GM4BRI"), "transmit-disabled")):
        link = Recorder()
        told = []
        ch = ChannelAccess(station, link, ChannelConfig(persist=255))
        ch.on_refused = lambda key, n: told.append((key, n))
        ch.queue(frame())
        ch.queue(frame("two"))
        ch.poll(0.0)
        assert link.log == [] and told == [(reason, 2)]
        assert not ch.pending()
    # Armed after queuing, refused at keying all the same if disarmed again.
    station = armed()
    link = Recorder()
    ch = ChannelAccess(station, link, ChannelConfig(persist=255))
    ch.queue(frame())
    station.tx_enabled = False
    ch.poll(0.0)
    assert link.log == []
