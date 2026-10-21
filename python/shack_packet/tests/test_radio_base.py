"""The safety core every radio link shares (radios/base.py), against a link
whose radio is a list: the station checked at keying and while keyed, the
watchdog, unkey on error, disconnect and close, the owed unkey, audio paced
and played before the unkey, busy in order, and the channel's handling of
a link that will not key."""

from __future__ import annotations

import random
import time

import pytest

from radiohelp import Recorder, Sink, until
from shack_packet import Station
from shack_packet.channel import ChannelAccess, ChannelConfig, Outgoing
from shack_packet.engine import Engine
from shack_packet.radios import REASONS, Claim, LinkRefused

ARMED = Station.of("GM4BRI", tx_enabled=True)


def link(station=ARMED, **kw) -> tuple[Recorder, Sink]:
    sink = Sink(station)
    r = Recorder(**kw)
    r.attach(sink)
    return r, sink


def tone(seconds: float, rate: int = 12000) -> bytes:
    return b"\x10\x00" * int(seconds * rate)


def test_every_reason_is_a_key_the_app_translates():
    assert {"no-callsign", "transmit-disabled", "watchdog", "disarmed", "claim-lost",
            "receive-only", "disconnected", "unkey-failed"} <= set(REASONS)


@pytest.mark.parametrize("station, reason", [
    (Station(), "no-callsign"),
    (Station.of("GM4BRI", tx_enabled=False), "transmit-disabled"),
])
def test_the_station_is_asked_at_the_moment_of_keying(station, reason):
    r, _ = link(station)
    with r:
        with pytest.raises(LinkRefused) as e:
            r.key()
        assert e.value.reason == reason
    assert "key" not in r.what()


def test_a_closed_link_and_a_lost_claim_refuse():
    class Lost(Claim):
        def held(self):
            return False

    r, _ = link()
    with pytest.raises(LinkRefused, match="not-open"):
        r.key()
    r2, _ = link(claim=Lost())
    with r2, pytest.raises(LinkRefused) as e:
        r2.key()
    assert e.value.reason == "claim-lost"


def test_a_claim_that_will_not_be_had_stops_the_link_opening():
    class Taken(Claim):
        def acquire(self):
            return "claim-lost"

    r, _ = link(claim=Taken())
    with pytest.raises(LinkRefused, match="claim-lost"):
        r.open()
    assert "open" not in r.what()


def test_audio_is_paced_and_the_unkey_waits_for_it_to_play():
    r, _ = link()
    with r:
        t0 = time.monotonic()
        r.key()
        r.send_pcm(tone(0.5))
        r.unkey()                                   # the engine says done at once
        assert until(lambda: not r.keyed, 3)
        key, unkey = r.when("key")[0], r.when("unkey")[0]
        audio = r.when("audio")
        assert key <= audio[0] and audio[-1] <= r.when("end")[0] <= unkey
        assert sum(n for _t, w, n in r.log if w == "audio") == len(tone(0.5))
        # Never handed over faster than it plays, plus the lead.
        assert audio[-1] - t0 >= 0.5 - r.CHUNK_S - r.LEAD_S - 0.02
        # And unkeyed only once it has played and drained.
        assert unkey - key >= 0.5 + r.drain_s - 0.03
        assert unkey - key < 0.5 + r.drain_s + 0.3
    assert r.what()[-1] == "close"


def test_audio_is_handed_over_at_the_rate_it_plays():
    # The 18/10/2026 real-radio runs raised the question of a link feeding
    # the radio slower than real time (holes over the LAN, a stretched over
    # on USB). Over two seconds the pieces go to the radio at the rate they
    # play, within half a percent, and never later than their turn.
    r, _ = link()
    with r:
        r.key()
        r.send_pcm(tone(2.0))
        r.unkey()
        assert until(lambda: not r.keyed, 4)
    key = r.when("key")[0]
    pieces = [(t, n) for t, w, n in r.log if w == "audio"]
    played = 0.0
    late = []
    for t, n in pieces:
        if t - key > played + r.TICK_S * 3:
            late.append(round(t - key - played, 3))
        played += n / 2 / r.rate
    assert late == [], f"pieces handed over after their turn (s late): {late}"
    # Steady state, past the lead: audio handed over per second of clock.
    (t0, _n), (t1, _m) = pieces[10], pieces[-1]
    audio = sum(n for _t, n in pieces[10:-1]) / 2 / r.rate
    assert abs(audio / (t1 - t0) - 1.0) < 0.005, audio / (t1 - t0)


def test_audio_to_an_unkeyed_radio_is_never_sent():
    r, _ = link()
    with r:
        r.send_pcm(tone(0.1))
        time.sleep(0.05)
    assert "audio" not in r.what() and r.stats["audio_refused"] == 1


def test_disarming_mid_transmission_unkeys_within_a_tick():
    station = Station.of("GM4BRI", tx_enabled=True)
    r, sink = link(station)
    with r:
        r.key()
        r.send_pcm(tone(2.0))
        time.sleep(0.2)
        station.tx_enabled = False
        t = time.monotonic()
        assert until(lambda: not r.keyed, 1)
        assert r.when("unkey")[0] - t < 0.1
        assert until(lambda: "disarmed" in sink.reasons(), 1)
        audio_after = [a for a in r.when("audio") if a > r.when("unkey")[0]]
        assert audio_after == [], "the rest of the audio was dropped"


def test_the_watchdog_unkeys_whatever_the_engine_asked():
    r, sink = link(max_key_s=0.3)
    with r:
        r.key()
        r.send_pcm(tone(2.0))
        assert until(lambda: not r.keyed, 2)
        held = r.when("unkey")[0] - r.when("key")[0]
        assert 0.3 <= held < 0.45
        assert until(lambda: "watchdog" in sink.reasons(), 1)


def test_an_audio_failure_unkeys_at_once():
    r, sink = link()
    with r:
        r.key()
        r.fail_audio = True
        r.send_pcm(tone(0.3))
        assert until(lambda: not r.keyed, 1)
        assert until(lambda: "radio-error" in sink.reasons(), 1)


def test_a_radio_that_goes_away_is_unkeyed():
    r, sink = link()
    with r:
        r.key()
        r.send_pcm(tone(1.0))
        r.alive = False
        assert until(lambda: not r.keyed, 1)
        assert until(lambda: "disconnected" in sink.reasons(), 1)


def test_an_unkey_that_fails_is_owed_and_sent_when_it_can_be():
    r, sink = link()
    with r:
        r.key()
        r.fail_unkey = 3
        r.unkey()
        assert until(lambda: "unkey" in r.what(), 2), "retried every tick until it went"
        assert until(lambda: sink.reasons().count("unkey-failed") >= 1 and "unkey-sent" in sink.reasons(), 1)
        assert not r.on_air


def test_closing_a_keyed_link_unkeys_before_anything_else_goes():
    r, _ = link()
    r.open()
    r.key()
    r.send_pcm(tone(2.0))
    time.sleep(0.05)
    r.close()
    assert r.what().index("unkey") < r.what().index("close")
    assert not r.on_air


def test_a_key_that_fails_is_unkeyed_and_refused():
    r, _ = link()
    with r:
        r.fail_key = OSError("no CI-V")
        with pytest.raises(LinkRefused) as e:
            r.key()
        assert e.value.reason == "radio-refused" and "no CI-V" in e.value.detail
        assert "unkey" in r.what() and not r.keyed


def test_a_second_transmission_while_draining_stays_keyed():
    r, _ = link()
    with r:
        r.key()
        r.send_pcm(tone(0.1))
        r.unkey()
        r.key()                                     # straight on: a digipeat, an RR
        r.send_pcm(tone(0.1))
        r.unkey()
        assert until(lambda: not r.keyed, 2)
        assert r.what().count("key") == 1 and r.what().count("unkey") == 1


def test_busy_reaches_the_engine_in_order_and_a_stuck_squelch_is_said():
    r, sink = link()
    r.BUSY_STUCK_S = 0.2
    with r:
        r.busy_now = True
        assert until(lambda: sink.busy == [True], 1)
        r.busy_now = False
        r.busy_now = True
        assert until(lambda: "busy-stuck" in sink.reasons(), 2)
        r.busy_now = False
        assert until(lambda: sink.busy[-1] is False, 1)
    assert sink.reasons().count("busy-stuck") == 1


def test_the_channel_treats_a_refusing_link_as_a_refused_transmission():
    class Lost(Claim):
        def held(self):
            return False

    r = Recorder(claim=Lost())
    eng = Engine(ARMED, r, rng=random.Random(1))
    r.open()
    try:
        sent = []
        eng.channel.queue(b"\x00" * 20, on_sent=sent.append)
        eng.channel.config.persist = 255
        eng.poll()
        assert eng.channel.stats.refused["claim-lost"] == 1 and eng.channel.stats.transmissions == 0
        assert sent, "the frame's sender is told, so its T1 runs as on a dead channel"
        assert any(e.kind == "tx-refused" and e.reason == "claim-lost" for e in eng.events)
        assert not eng.channel.transmitting and "key" not in r.what()
    finally:
        r.close()


def test_link_events_become_engine_events():
    r = Recorder(max_key_s=0.2)
    eng = Engine(ARMED, r)
    with r:
        r.key()
        r.send_pcm(tone(1.0))
        assert until(lambda: any(e.reason == "watchdog" for e in eng.events), 2)


# -- the defences added after Ray's IC-9700 stuck on transmit (18/10/2026) ---------


def test_a_stuck_supervisor_does_not_keep_the_radio_keyed():
    # The supervisor holding the lock in a radio call that never returns: the
    # hard watchdog, a thread of its own, unkeys without the lock.
    import threading
    r, _ = link(max_key_s=0.3)
    stuck = threading.Event()
    with r:
        try:
            r.key()
            assert r.on_air
            t0 = time.monotonic()
            r._tick = lambda events: stuck.wait()   # holds the lock, as a call into a hung device would
            assert until(lambda: not r.on_air, 0.3 + 1.0 + 1.0)    # max_key_s + HARD_MARGIN_S + slack
            assert time.monotonic() - t0 <= 0.3 + 1.0 + 0.3
            assert r.stats["hard-watchdog"] >= 1
        finally:
            stuck.set()


def test_a_link_left_keyed_at_exit_unkeys_the_radio():
    # No close(), as a crash on the way out or a forgotten close would leave
    # it: atexit sends the unkey.
    import os
    import subprocess
    import sys
    code = (
        "import sys\n"
        "from radiohelp import Recorder, Sink\n"
        "from shack_packet import Station\n"
        "class Loud(Recorder):\n"
        "    def _radio_unkey(self):\n"
        "        super()._radio_unkey(); print('unkey sent', flush=True)\n"
        "r = Loud(); r.attach(Sink(Station.of('GM4BRI', tx_enabled=True)))\n"
        "r.open(); r.key(); r.send_pcm(b'\\0\\0' * 120000)\n"
        "print('keyed', r.on_air, flush=True)\n"
        "sys.exit(0)\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([os.path.dirname(__file__), *sys.path]))
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == ["keyed True", "unkey sent"]


def test_an_unkey_is_confirmed_by_asking_the_radio_and_sent_again():
    class Deaf(Recorder):
        CONFIRMS_UNKEY = True
        UNKEY_CONFIRM_S = UNKEY_RETRY_S = 0.05
        ignore = 1
        asked = 0

        def _radio_unkey(self):
            if self.ignore:
                self.ignore -= 1
                self._note("unkey-ignored")
                return                              # sent; the radio did not act on it
            super()._radio_unkey()

        def _radio_ptt_readback(self):
            self.asked += 1
            return self.on_air

    sink = Sink(ARMED)
    r = Deaf()
    r.attach(sink)
    with r:
        r.key()
        r.unkey()
        assert until(lambda: not r.on_air, 2)
        assert r.what().count("unkey-ignored") == 1 and r.what().count("unkey") == 1
        assert until(lambda: r._confirm is None, 1)
        assert r.stats["unkey-retried"] == 1 and r.asked == 2


def test_an_unkey_the_radio_never_confirms_is_reported():
    class Stuck(Recorder):
        CONFIRMS_UNKEY = True
        UNKEY_CONFIRM_S = UNKEY_RETRY_S = 0.02

        def _radio_ptt_readback(self):
            return True                             # stuck on transmit whatever it is told

    sink = Sink(ARMED)
    r = Stuck()
    r.attach(sink)
    with r:
        r.key()
        r.unkey()
        assert until(lambda: "unkey-unconfirmed" in sink.reasons(), 2)
        assert r.what().count("unkey") == 1 + r.UNKEY_TRIES


# -- receive audio off the radio's thread; the engine's lock off the radio's path ---
#
# 18/10/2026 on air, IC-9700 over the LAN with the app's engine running:
# TimeoutErrors from rig.set_ptt and from the minimum-power read at keying,
# and a 15 s silence before the radio's polls were answered - none of which
# happened from a bare script. The rig hands receive audio to its listeners
# on its own asyncio loop; the link handed it straight to Engine.pcm_in,
# which took the engine's lock and demodulated, there. The engine's poll
# thread meanwhile held that lock while it keyed, waiting on the same loop.


def test_received_audio_reaches_the_engine_from_a_thread_of_its_own():
    import threading
    gate = threading.Event()
    seen = []

    class Slow(Sink):
        def pcm_in(self, pcm):
            seen.append(threading.current_thread())
            gate.wait(2)
            super().pcm_in(pcm)

    r = Recorder()
    sink = Slow(ARMED)
    r.attach(sink)
    with r:
        t = time.monotonic()
        r._rx(b"\x01\x00" * 240)
        assert time.monotonic() - t < 0.01, "the radio's thread did not wait for the demodulator"
        assert until(lambda: seen, 1)
        assert seen[0] is not threading.current_thread()
        gate.set()
        assert until(lambda: bytes(sink.pcm) == b"\x01\x00" * 240, 1)


def test_a_receiver_that_falls_behind_loses_the_oldest_audio_and_counts_it():
    import threading
    gate, entered = threading.Event(), threading.Event()

    class Stuck(Sink):
        def pcm_in(self, pcm):
            entered.set()
            gate.wait(5)
            super().pcm_in(pcm)

    r = Recorder()
    r.RX_QUEUE_S = 0.2                              # ten 20 ms blocks
    sink = Stuck(ARMED)
    r.attach(sink)
    with r:
        blocks = [bytes([i, 0]) * 240 for i in range(40)]
        r._rx(blocks[0])
        assert entered.wait(1)
        t = time.monotonic()
        for b in blocks[1:]:
            r._rx(b)
        assert time.monotonic() - t < 0.05, "a full queue never holds up the radio's thread"
        gate.set()
        assert until(lambda: len(sink.pcm) >= 11 * 480, 2)
        time.sleep(0.05)
    got = [bytes(sink.pcm[i:i + 480]) for i in range(0, len(sink.pcm), 480)]
    # The block already in the engine's hands when the rest arrived, then
    # the newest ten: what was dropped is the oldest, and is counted.
    assert got == [blocks[0]] + blocks[-10:]
    assert r.stats["rx_dropped"] == 40 - 11


def test_the_engine_lets_go_of_its_lock_while_the_radio_keys_and_unkeys():
    # key() and unkey() on a real link wait for the radio's own thread; if
    # that thread needs the engine (received audio, busy) the engine must
    # not be holding its lock meanwhile.
    import threading

    from shack_packet import ax25
    from shack_packet.channel import RadioLink

    class Waits(RadioLink):
        rate = 12000

        def __init__(self):
            self.eng = None
            self.got = []

        def _radio_thread_needs_the_engine(self) -> bool:
            done = threading.Event()

            def rx():
                self.eng.pcm_in(b"\x00\x00" * 240)
                self.eng.busy_in(False)
                done.set()

            threading.Thread(target=rx, daemon=True).start()
            return done.wait(1.0)

        def key(self):
            self.got.append(("key", self._radio_thread_needs_the_engine()))

        def unkey(self):
            self.got.append(("unkey", self._radio_thread_needs_the_engine()))

        def send_pcm(self, pcm):
            pass

    radio = Waits()
    eng = Engine(ARMED, radio, rng=random.Random(1))
    radio.eng = eng
    eng.channel.config.persist = 255
    eng.send_frame(ax25.Frame.from_monitor("GM4BRI>APRS:>lock test"))
    end = eng.poll()
    assert eng.channel.transmitting and eng.channel.stats.transmissions == 1
    eng.poll(end)
    assert radio.got == [("key", True), ("unkey", True)]
    assert not eng.channel.transmitting
