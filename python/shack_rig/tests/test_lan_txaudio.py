"""Transmit audio over the LAN arrives as one unbroken stream.

On 18/10/2026 an IC-9700 keyed over the LAN by shack_packet transmitted
packet audio with holes in it - 15-20 ms of nothing about every 100 ms - and
nothing decoded it (shack-packet HANDOVER.md). These run the real path
(IcomRig, LanLink, the audio stream) against FakeIcom, feed it transmit audio
the way a transmitter does - 20 ms pieces against an absolute clock, a little
ahead, late by a scheduler's whim - and ask the fake's playout model
(FakeIcom.tx_playout) for the holes the radio would have transmitted.
"""

import asyncio
import random
import time

from helpers import OnFake, until
from shack_rig import Lease
from shack_rig.icom import lan


RATE = 12000
CHUNK = int(RATE * 0.02) * 2
#: The most any hole may be: a rounding error, not a missing packet.
HOLE_S = 0.002


async def transmit(rig, seconds: float, *, lead: float = 0.06, jitter: float = 0.0, seed: int = 1):
    """Key, send `seconds` of audio paced like shack_packet's links, unkey."""
    rng = random.Random(seed)
    lease = Lease("test")
    assert rig.set_ptt(True, lease)
    pcm = bytes(range(256)) * (int(RATE * seconds) * 2 // 256)
    start = time.monotonic()
    sent = 0.0
    for at in range(0, len(pcm), CHUNK):
        # Wait until this piece is no more than `lead` ahead of real time,
        # then a little longer at random - a busy event loop.
        wait = start + sent - lead - time.monotonic()
        await asyncio.sleep(max(0.0, wait) + rng.uniform(0, jitter))
        chunk = pcm[at:at + CHUNK]
        assert rig.send_audio(chunk)
        sent += len(chunk) / 2 / RATE
    rig.tx_end()
    await asyncio.sleep(rig.tx_drain_s)
    rig.set_ptt(False)
    return len(pcm)


def test_audio_sent_ahead_of_time_plays_without_a_hole():
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            await asyncio.sleep(0.3)
            n = await transmit(rig, 1.2)
            assert await until(lambda: len(radio.tx_pcm) >= n, 2)
            holes = [h for h in radio.tx_playout() if h[1] > HOLE_S]
            assert holes == [], f"holes (at s, for s): {holes}"
            assert radio.tx_pcm[:n] == bytes(range(256)) * (n // 256)

    asyncio.run(run())


def test_nothing_but_audio_takes_a_place_in_the_sequence_while_it_flows():
    # The idle the keepalive sends is what made the holes: a place in the
    # sequence with no audio in it. While audio is flowing it carries the
    # sequence forward itself, so no idle is wanted (wfview's rule).
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            await asyncio.sleep(0.3)
            await transmit(rig, 1.0)
            stream = radio.tx_stream
            audio = [i for i, e in enumerate(stream) if e[2]]
            between = stream[audio[0]:audio[-1] + 1]
            idles = [e for e in between if not e[2]]
            assert idles == [], f"{len(idles)} idles among {len(between)} packets"
            # ...and the keepalive still idles once the audio stops.
            n = len(radio.tx_stream)
            assert await until(lambda: any(not e[2] for e in radio.tx_stream[n:]), 1)

    asyncio.run(run())


def test_a_busy_event_loop_does_not_make_the_radio_underrun():
    # Scheduling jitter of up to 30 ms on every piece: the 60 ms lead and the
    # radio's own 100 ms buffer absorb it.
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            await asyncio.sleep(0.3)
            await transmit(rig, 1.2, jitter=0.03, seed=7)
            holes = [h for h in radio.tx_playout() if h[1] > HOLE_S]
            assert holes == [], f"holes (at s, for s): {holes}"
            assert rig._link._audio.tx_underruns == 0, "and the sender's own books agree"

    asyncio.run(run())



def test_audio_sent_later_than_the_radio_can_wait_is_reported():
    # The sender cannot see the radio's buffer, but it knows how much audio
    # it has handed over and since when: audio later than the buffer can
    # cover is counted and said, so a real-radio run reports it.
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            await asyncio.sleep(0.3)
            stream = rig._link._audio
            assert rig.set_ptt(True, Lease("test"))
            assert rig.send_audio(bytes(CHUNK))
            await asyncio.sleep(0.02 + lan.TX_BUFFER_MS / 1000 + 0.05)
            assert rig.send_audio(bytes(CHUNK))
            rig.set_ptt(False)
            assert stream.tx_underruns == 1
            assert stream.tx_over["late_ms"] >= 40

    asyncio.run(run())
