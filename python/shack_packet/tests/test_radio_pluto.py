"""The Pluto as a packet radio (radios/pluto.py), against the fake libiio:
its transmitter quiet before anything else, after every over, at close,
on error and - with the writer and the supervisor both stuck - by its own
watchdog; the gate on transmit writes; frames through its FM modulator
and back through its FM receiver; and receive carrying on through an over."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from radiohelp import Sink, heard_frames, until
from sdrhelp import FakePluto

from shack_packet import Station, afsk
from shack_packet.ax25 import Frame
from shack_packet.errors import PacketError
from shack_packet.radios import REASONS, LinkRefused, PlutoLink
from shack_packet.radios.pluto import RX_BLOCK_S, TX_KERNEL_BUFFERS, FmModulator
from shack_packet.sdr.pluto import AD9363_REFUSAL, TX_ATTEN_MAX

FREQ = 144_800_000
IFO = 100_000
RATE = 2_400_000
ARMED = Station.of("GM4BRI", tx_enabled=True)
QUIET = [("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", str(TX_ATTEN_MAX))]


def make(radio=None, *, realtime=True, station=ARMED, **kw):
    radio = radio or FakePluto()
    radio.rx_realtime = realtime
    sink = Sink(station)
    kw.setdefault("allow_unguarded", "guard" not in kw)
    link = PlutoLink("ip:192.168.2.1", FREQ, iio_module=radio.module(), **kw)
    link.attach(sink)
    return radio, link, sink


def frames(n=3, tag="pluto"):
    return [Frame.from_monitor(f"GM4BRI>APZOHS,WIDE1-1:>{tag} {i}").encode() for i in range(n)]


def over(n=3, tag="pluto") -> bytes:
    return afsk.pcm16(afsk.modulate(frames(n, tag), 12_000, amplitude=0.5, txdelay_ms=300))


def test_the_new_reasons_are_keys_the_app_translates():
    assert {"tx-not-quiet", "out-of-range"} <= set(REASONS)


def test_the_transmitter_is_quieted_and_read_back_before_anything_else_is_written():
    radio, link, _ = make()                        # booted as Tezuka leaves it: TX LO up, -10 dB
    assert not radio.tx_quiet()
    with link:
        assert radio.writes[:2] == QUIET
        assert radio.tx_quiet()
        assert link.tx_state() == {"powerdown": 1.0, "gain_db": TX_ATTEN_MAX, "quiet": True} and link.tx_quiet
        # Both LOs below the channel, so DC and LO leakage sit outside it.
        assert radio.attr("rx_lo", "frequency") == radio.attr("tx_lo", "frequency") == str(FREQ - IFO)
        assert radio.attr("rx", "sampling_frequency") == str(RATE)
        assert len(radio.contexts) == 2            # the watchdog's own
        assert not any(w == ("tx_lo", "powerdown", "0") for w in radio.writes)
    assert radio.tx_quiet() and radio.tx_writes()[-2:] == QUIET


def test_a_pluto_whose_transmitter_will_not_read_quiet_is_refused_untouched():
    radio, link, _ = make()
    radio.ignore.add(("tx_lo", "powerdown"))       # the write "works" and the LO stays up
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "tx-not-quiet"
    assert not link.is_open
    assert not [w for w in radio.writes if w[0] in ("rx", "rx_lo")]
    assert radio.buffers == []
    assert set(radio.tx_writes()) == set(QUIET)


def test_a_stock_pluto_on_2m_is_refused_with_the_receivers_sentence():
    radio, link, _ = make(FakePluto(identity="ad9363"))
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "out-of-range" and e.value.detail == AD9363_REFUSAL
    assert radio.writes[:2] == QUIET and radio.tx_quiet()
    assert radio.buffers == []


def test_settings_out_of_bounds_are_refused_when_the_link_is_made():
    for kw in ({"tx_gain_db": 3.0}, {"tx_gain_db": -100.0}, {"rate_iq": 1_000_000}, {"ifo_hz": 5_000}):
        with pytest.raises(LinkRefused) as e:
            PlutoLink("ip:192.168.2.1", FREQ, iio_module=FakePluto().module(), **kw)
        assert e.value.reason == "setup"


def test_the_gate_refuses_any_transmit_write_not_on_its_list():
    radio, link, _ = make()
    with link:
        before = list(radio.writes)
        for chan, name, value in [("tx", "hardwaregain", -5.0),         # not the configured gain
                                  ("tx", "hardwaregain", 0),
                                  ("tx", "rf_port_select", "B"),
                                  ("tx_lo", "frequency", FREQ),           # the channel, not the LO
                                  ("tx_lo", "powerdown", 2),
                                  ("tx", "sampling_frequency", 30_720_000),
                                  ("rx", "xo_correction", 40_000_000)]:
            with pytest.raises(PacketError, match="refused"):
                link._write(chan, name, value)
        assert radio.writes == before                                     # none reached the radio


def test_an_over_goes_out_and_the_transmitter_reads_quiet_after_it():
    radio, link, sink = make()
    with link:
        link.key()
        link.send_pcm(over(1))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        assert radio.tx_quiet() and link.tx_state()["quiet"]
        assert ("tx_lo", "powerdown", "0") in radio.writes
        assert any(live for _d, live in radio.pushed)
        tx = radio.tx_writes()
        assert tx[tx.index(("tx_lo", "powerdown", "0")) + 1:][:2] == QUIET
        assert ("tx", "hardwaregain", "-10.0") in tx
        # Fixed-size buffers, the kernel's queue kept short.
        assert {s for d, s, _c in radio.buffers if d == "cf-ad9361-dds-core-lpc"} == {48_000}
        assert radio.kernel_buffers["cf-ad9361-dds-core-lpc"] == TX_KERNEL_BUFFERS
        # Carrier before the first piece and after the last, none between.
        assert link.stats["tx_fill_s"] > 0 and link.stats["tx_gaps"] == 0
    assert not [e for e in sink.events if e[0] == "unkeyed"]


def test_closing_while_keyed_quiets_the_transmitter():
    radio, link, _ = make()
    link.open()
    link.key()
    link.send_pcm(over(3))
    assert until(lambda: any(live for _d, live in radio.pushed))
    link.close()
    assert radio.tx_quiet() and not link.keyed
    link.close()                                   # safe twice
    assert radio.tx_quiet()


def test_a_brief_flicker_on_an_empty_channel_is_not_busy():
    # 10 ms of weak carrier: its one block reads about 10 dB over the
    # channel's floor (over BUSY_DB, which one block alone used to decide),
    # its 100 ms average about 5 dB - as the real Pluto's empty channel
    # flickered about once a second. Not a station, and it must not hold off
    # the engine's transmit or be counted as one by the harness.
    mod = FmModulator(RATE, 3_000, IFO)
    rng = np.random.default_rng(2)
    k = int(1.5 * RATE)

    def noise(n):
        return rng.normal(0, 0.01, n) + 1j * rng.normal(0, 0.01, n)

    blip = mod(np.zeros(120)) * 0.006
    air = np.concatenate([noise(k), noise(len(blip)) + blip, noise(k)])
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([air.real, air.imag]).ravel() * 2047).astype(np.int16)
    radio, link, sink = make(rx)
    with link:
        assert until(lambda: link.level_db is not None, 5)
        time.sleep(3.5)
    assert True not in sink.busy


def test_a_closed_link_reports_the_quiet_it_read_back_as_it_closed():
    radio, link, _ = make()
    link.open()
    link.close()
    state = link.tx_state()
    assert state["closed"] and state["quiet"] and state["powerdown"] == 1
    assert link.tx_quiet


def test_a_push_that_fails_mid_over_quiets_the_transmitter_and_says_radio_error():
    radio, link, sink = make()
    radio.push_error, radio.push_error_after = OSError(5, "Input/output error"), 5
    with link:
        link.key()
        link.send_pcm(over(3))
        assert until(lambda: not link.keyed, 5)
        assert radio.tx_quiet()
        assert until(lambda: ("unkeyed", "radio-error") in [(k, r) for k, r, _d in sink.events])
    # Quieted twice after the key: by the writer itself the moment its push
    # failed, then by the base's abort - and once more at close.
    tx = radio.tx_writes()
    after = tx[tx.index(("tx_lo", "powerdown", "0")) + 1:]
    assert [after[i:i + 2] for i in range(0, len(after), 2)] == [QUIET] * 3


def test_the_watchdog_quiets_the_transmitter_with_the_writer_and_the_supervisor_stuck():
    radio, link, sink = make(max_key_s=0.3)
    radio.push_hold, radio.hold_after = threading.Event(), 3
    link.open()
    try:
        t0 = time.monotonic()
        link.key()
        link.send_pcm(over(3))
        # The fourth push hangs holding the main context; the base's own
        # watchdog (0.3 s) then hangs in its unkey, behind that context,
        # holding the link's lock. Neither can quiet anything.
        time.sleep(0.8)
        assert radio.push_count == 4 and not radio.tx_quiet() and link.keyed
        assert until(radio.tx_quiet, 0.3 + 1.0 + 0.5 - (time.monotonic() - t0))
        took = time.monotonic() - t0
        assert 0.3 + 1.0 <= took <= 0.3 + 1.0 + 0.5
        assert link.keyed                          # the supervisor is still stuck: this was a watchdog
        # Two lock-free watchdogs time the same limit - PlutoLink's own and
        # the base's hard watchdog, whose emergency unkey goes through the
        # same second context - and whichever looks first quiets it.
        assert until(lambda: link.stats["hard-watchdog"] or any(
            r == "watchdog" and d.get("by") == "pluto-watchdog" for _k, r, d in sink.events))
    finally:
        radio.push_hold.set()
        link.close()
    assert radio.tx_quiet() and not link.keyed


def test_an_unkey_is_confirmed_and_sent_again_when_the_transmitter_comes_back_up():
    radio, link, sink = make()
    with link:
        link.key()
        link.send_pcm(over(1))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        # Inside the confirmation window something powers the TX LO up again
        # behind the link's back: the read-back sees it and quiets it again.
        radio.phy.find_channel("altvoltage1", True).attrs["powerdown"]._value = "0"
        assert not radio.tx_quiet()
        assert until(lambda: link.stats["unkey-retried"] >= 1, 2)
        assert until(radio.tx_quiet, 1)
        time.sleep(link.UNKEY_RETRY_S + 0.1)
        assert link._confirm is None                # confirmed, and done asking
    assert not [e for e in sink.events if e[1] == "unkey-unconfirmed"]


def test_the_emergency_unkey_goes_round_a_push_stuck_holding_the_main_context():
    radio, link, _ = make(max_key_s=30)
    radio.push_hold, radio.hold_after = threading.Event(), 3
    link.open()
    try:
        link.key()
        link.send_pcm(over(3))
        assert until(lambda: radio.push_count == 4, 2)
        assert link._radio_ptt_readback() is True   # read through the second context, not blocked
        t0 = time.monotonic()
        link.emergency_unkey()
        assert time.monotonic() - t0 < 0.5 and radio.tx_quiet()
        assert link._radio_ptt_readback() is False
    finally:
        radio.push_hold.set()
        link.close()
    assert radio.tx_quiet()


def _live(radio) -> np.ndarray:
    live = [i for i, (_d, on) in enumerate(radio.pushed) if on]
    raw = radio.pushed[live[0]:live[-1] + 1]
    keep = FakePluto()
    keep.pushed = raw
    return keep.tx_iq()


def test_frames_go_out_as_fm_beside_the_lo_and_come_back_through_the_receiver():
    radio, link, _ = make()
    with link:
        link.key()
        link.send_pcm(over(3, "loop"))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
    iq = _live(radio)
    # No hole anywhere in the over: constant envelope, the DAC never starved.
    assert len(iq) > RATE and np.abs(iq).min() > 0.65
    # The signal is a channel at +ifo; DC, where the LO leaks, is outside it.
    spec = np.abs(np.fft.fft(iq * np.hanning(len(iq)))) ** 2
    f = np.fft.fftfreq(len(iq), 1 / RATE)
    assert abs(f[np.argmax(spec)] - IFO) < 6_000
    in_channel = spec[np.abs(f - IFO) < 12_500].sum()
    assert in_channel > 0.999 * spec.sum()
    assert spec[np.abs(f) < 12_500].sum() < 1e-7 * in_channel

    # Back through a second Pluto's receiver: 600 Hz off, in noise.
    n = np.arange(len(iq))
    rng = np.random.default_rng(7)
    noise = lambda k: (rng.normal(0, 0.03, k) + 1j * rng.normal(0, 0.03, k))  # noqa: E731
    air = np.concatenate([noise(RATE // 2), iq * np.exp(2j * np.pi * 600 * n / RATE) + noise(len(iq)),
                          noise(RATE // 2)])
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([air.real, air.imag]).ravel() * 2047).astype(np.int16)
    radio2, link2, sink2 = make(rx, realtime=False)
    with link2:
        assert until(lambda: len(sink2.pcm) >= 2 * 12_000 * 2 * len(air) / RATE, 20)
    heard = set(heard_frames(sink2.pcm, 12_000))
    assert {f"GM4BRI>APZOHS,WIDE1-1:>loop {i}" for i in range(3)} <= heard


def test_receive_stops_for_an_over_and_starts_again_on_a_fresh_buffer():
    """Half-duplex by default: the USB network carries one stream at a time
    (radios/pluto.py `_read_iq`), and nothing received during the over -
    our own signal - is handed on afterwards."""
    radio, link, sink = make()
    with link:
        assert until(lambda: radio.refills > 2)
        link.key()
        time.sleep(0.15)                            # a refill already under way finishes
        during = radio.refills
        link.send_pcm(over(2))
        assert until(lambda: link.stats["tx_pushed"] > 20, 5)
        assert radio.refills == during              # not one read while transmitting
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        assert until(lambda: radio.refills > during + 2, 2)
    rx_buffers = [b for b in radio.buffers if b[0] == "cf-ad9361-lpc"]
    assert len(rx_buffers) == 2 and link.stats["rx_paused"] == 1


def test_receive_carries_on_through_an_over_when_asked():
    radio, link, sink = make(rx_during_tx=True)
    with link:
        assert until(lambda: len(sink.pcm) > 0)
        link.key()
        t0, n0 = time.monotonic(), len(sink.pcm)
        link.send_pcm(over(2))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        keyed_s, heard_s = time.monotonic() - t0, (len(sink.pcm) - n0) / 2 / 12_000
    # Most of it: the receiver's own latency (the channeliser's overlap, a
    # block in hand) comes off the end; a receiver that stopped would hear
    # none of the over.
    assert keyed_s > 0.5 and heard_s > 0.5 * keyed_s


def test_busy_follows_a_signal_above_the_channels_floor():
    mod = FmModulator(RATE, 3_000, IFO)
    rng = np.random.default_rng(1)
    k = int(1.5 * RATE)
    quiet = (rng.normal(0, 0.01, k) + 1j * rng.normal(0, 0.01, k))
    tone = 0.5 * np.sin(2 * np.pi * 1200 * np.arange(int(0.6 * 12_000)) / 12_000)
    air = np.concatenate([quiet, mod(tone)])
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([air.real, air.imag]).ravel() * 2047).astype(np.int16)
    radio, link, sink = make(rx)
    with link:
        assert until(lambda: True in sink.busy, 5)
        assert until(lambda: sink.busy[-1] is False, 3)


# -- the guard on the Pluto (shack_pluto.guard) ----------------------------------------

class StubGuard:
    """A guard's three calls; `fail` makes start() raise as Guard does."""

    def __init__(self, fail=None):
        self.fail = fail
        self.confirmed = False
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.fail:
            raise type("GuardError", (Exception,), {"reason": self.fail, "detail": "stub"})(self.fail)
        self.confirmed = True
        return self

    def stop(self):
        self.calls.append("stop")
        self.confirmed = False


def test_with_no_guard_given_a_pluto_opens_receive_only():
    radio, link, sink = make(allow_unguarded=False)
    with link:
        assert not link.tx_guarded and link.guard_state == "none"
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "tx-unguarded"
        assert ("tx_lo", "powerdown", "0") not in radio.writes
    assert "tx-unguarded" in REASONS


def test_transmit_not_set_up_starts_no_guard_and_refuses_as_receive_only():
    guard = StubGuard()
    radio, link, sink = make(guard=guard, transmit=False)
    with link:
        assert guard.calls == [] and link.guard_state == "off"
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "receive-only"
    assert "tx-unguarded" not in sink.reasons()


def test_a_guard_that_will_not_start_leaves_the_pluto_receive_only_with_its_reason():
    guard = StubGuard(fail="guard-no-ssh")
    radio, link, sink = make(guard=guard)
    with link:
        assert link.unguarded_why.startswith("guard-no-ssh") and link.guard_state == "failed"
        assert until(lambda: "tx-unguarded" in sink.reasons())
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "tx-unguarded"
    assert radio.tx_quiet()


def test_the_guard_starts_after_the_first_quiet_and_stops_after_the_last():
    guard = StubGuard()
    radio, link, _ = make(guard=guard)
    order = []
    real_start, real_stop = guard.start, guard.stop
    guard.start = lambda: (order.append(("start", radio.tx_quiet(), len(radio.tx_writes()))), real_start())[1]
    guard.stop = lambda: (order.append(("stop", radio.tx_quiet())), real_stop())[1]
    with link:
        assert link.tx_guarded
        link.key()
        link.send_pcm(over(1))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
    # Started with the transmitter already quiet and nothing else written to
    # it yet; stopped with it quiet again.
    assert order == [("start", True, 2), ("stop", True)]


def test_a_guard_lost_mid_over_unkeys_within_a_tick_or_two():
    guard = StubGuard()
    radio, link, sink = make(guard=guard)
    with link:
        link.key()
        link.send_pcm(over(3))
        assert until(lambda: any(live for _d, live in radio.pushed), 2)
        guard.confirmed = False                    # the guard's session ended under us
        t0 = time.monotonic()
        assert until(lambda: not link.keyed, 1)
        assert time.monotonic() - t0 < 0.1
        assert radio.tx_quiet()
        assert until(lambda: ("unkeyed", "tx-unguarded") in [(k, r) for k, r, _d in sink.events])


# -- the crystal correction ---------------------------------------------------------------

def test_the_crystal_correction_is_written_before_either_lo_and_put_back_at_close():
    radio, link, _ = make(xo_correction=39_999_236)
    with link:
        writes = [w for w in radio.writes if w[1] in ("xo_correction", "frequency")]
        assert writes[0] == ("ad9361-phy", "xo_correction", "39999236")
        assert radio.phy.attrs["xo_correction"].value == "39999236"
    assert radio.phy.attrs["xo_correction"].value == "40000000"


def test_no_correction_given_leaves_the_radios_own():
    radio, link, _ = make()
    with link:
        pass
    assert not [w for w in radio.writes if w[1] == "xo_correction"]


def test_a_correction_more_than_200_ppm_out_is_refused_when_the_link_is_made():
    with pytest.raises(LinkRefused) as e:
        make(xo_correction=40_020_000)
    assert e.value.reason == "setup"


def test_the_correction_can_be_looked_up_for_the_pluto_actually_plugged_in():
    asked = []

    def lookup(attrs):
        asked.append(attrs.get("hw_model"))
        return 39_999_236
    radio, link, _ = make(xo_correction=lookup)
    with link:
        assert link.xo_applied == 39_999_236 and radio.phy.attrs["xo_correction"].value == "39999236"
    assert asked == ["Analog Devices PlutoSDR Rev.C (Z7010-AD9363A)"]
    radio, link, _ = make(xo_correction=lambda attrs: 41_000_000)      # a damaged store: not used
    with link:
        assert link.xo_applied is None
    assert not [w for w in radio.writes if w[1] == "xo_correction"]


# -- the turnaround: an over's end, and a reply straight after it ---------------------------

def test_a_reply_starting_200_ms_after_the_unkey_is_heard_and_the_timing_is_reported():
    """The other station answering as fast as it can: the Pluto's receiver
    is never blanked, so a reply 200 ms after our transmitter reads back
    off is decoded whole. The receive side here is a stand-in that knows
    when the transmitter went off (to a 50 ms block)."""
    reply_audio = np.frombuffer(over(2, "reply"), "<i2").astype(np.float64) / 32768.0
    reply = FmModulator(RATE, 3_000.0, IFO, amplitude=0.5)(reply_audio)
    reply_raw = np.round(np.column_stack([reply.real, reply.imag]).ravel() * 2047).astype(np.int16)
    radio, link, sink = make()
    radio.rx_realtime = True
    state = {"pos": 0, "was_up": False, "off_at": None}

    def rx(n):
        up = radio.attr("tx_lo", "powerdown") == "0"
        if state["was_up"] and not up and state["off_at"] is None:
            state["off_at"] = state["pos"]
        state["was_up"] |= up
        out = np.zeros(n, np.int16)
        if state["off_at"] is not None:
            start = state["off_at"] + 2 * int(0.2 * RATE)          # 200 ms after the unkey, in int16s
            lo, hi = state["pos"], state["pos"] + n
            a, b = max(lo, start), min(hi, start + len(reply_raw))
            if a < b:
                out[a - lo:b - lo] = reply_raw[a - start:b - start]
        state["pos"] += n
        return out
    radio.rx_source = rx
    with link:
        link.key()
        link.send_pcm(over(1, "ours"))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        assert until(lambda: {f"GM4BRI>APZOHS,WIDE1-1:>reply {i}" for i in range(2)}
                     <= set(heard_frames(sink.pcm, 12_000)), 10)
        t = link.timing()
    assert state["off_at"] is not None
    assert 0.0 <= t["unkey_tail_s"] < 0.12 and t["quiet_write_s"] < 0.05
    assert t["rx_after_unkey_s"] <= 0.1 + RX_BLOCK_S
    assert t["turnaround_s"] == link.turnaround_s and t["rx_queue_s"] == 0.2


def _long_over(push_delay_s, seconds=3.0):
    radio = FakePluto()
    radio.push_delay_s = push_delay_s
    radio, link, sink = make(radio, max_key_s=30)
    frames = [Frame.from_monitor(f"GM4BRI>APZOHS:>{'x' * 200} {i}").encode() for i in range(40)]
    pcm = afsk.pcm16(afsk.modulate(frames, 12_000, amplitude=0.5, txdelay_ms=300))[:int(seconds * 12_000) * 2]
    with link:
        link.key()
        link.send_pcm(pcm)
        link.unkey()
        assert until(lambda: not link.keyed, seconds + 5)
    return link.stats


def test_a_long_over_on_a_link_that_keeps_up_is_never_late():
    # 5 ms a push: shack's Pluto took 3 ms for the quiet writes, and a
    # 192 kB push at ~18 MB/s is about 10 ms.
    st = _long_over(0.005)
    assert st["tx_late"] == 0 and st["tx_gaps"] == 0
    assert st["tx_slack_min_ms"] > 60                 # LEAD_S keeps well over 60 ms queued
    assert st["tx_push_ms_max"] >= 5


def test_a_link_too_slow_for_the_stream_shows_as_late_pushes():
    # 30 ms to push 20 ms of IQ: the DAC must run dry, and the stats say so.
    st = _long_over(0.030, seconds=2.0)
    assert st["tx_late"] > 5 and st["tx_late_ms"] > 50


def test_a_receiver_whose_stream_breaks_is_made_again_and_says_so(caplog):
    """Workbench's nano, 18/10/2026: a refill failed with EPIPE seconds into
    a receive-only run and the receiver stopped for good. Now the buffer is
    made again - here after the device was still busy once - and receive
    carries on, with the failure counted and logged."""
    radio, link, sink = make()
    with link:
        assert until(lambda: link.rx_listening.is_set() and radio.refills > 2)
        radio.rx_busy = 1
        radio.refill_failures = 1
        assert until(lambda: link.stats["rx_rebuilds"] == 1, 5)
        assert until(lambda: link.rx_listening.is_set(), 2)
        before = len(sink.pcm)
        assert until(lambda: len(sink.pcm) > before, 2)
        assert link.stats["rx_failures"] == 1
        assert ("radio", "radio-error") in [(k, r) for k, r, _d in sink.events]
    assert "receive buffer made again" in caplog.text


def test_a_receiver_that_keeps_failing_gives_up_and_says_so(caplog):
    radio, link, sink = make()
    with link:
        assert until(lambda: radio.refills > 2)
        radio.refill_failures = 10_000
        assert until(lambda: "the receiver stops" in caplog.text, 20)
        assert not link.rx_listening.is_set()
    assert radio.tx_quiet()


def _booted(radio):
    """What a Pluto that has just rebooted holds: its own receive rate and
    the TX LO up at -10 dB (GUIDE §5)."""
    for c in radio.phy.channels:
        if c.label == "rx":
            c.attrs["sampling_frequency"]._value = "30720000"
        if c.label == "tx_lo":
            c.attrs["powerdown"]._value = "0"
        if c.label == "tx":
            c.attrs["hardwaregain"]._value = "-10.000000 dB"


def test_a_pluto_that_restarted_under_the_receiver_is_quieted_first(caplog):
    """Workbench's nano froze in a receive-only soak (18/10/2026), its own
    watchdog reset it, and it came back transmitting its LO for nearly
    five minutes. The receiver's recovery now quiets any Pluto it reaches
    on a fresh context before anything else, and stops on one whose
    settings are gone."""
    radio, link, sink = make()
    with link:
        assert until(lambda: radio.refills > 2)
        radio.rx_busy = 1000                        # the old stream's device stays busy
        _booted(radio)
        radio.refill_failures = 1
        assert until(lambda: link.stats["radio_restarted"] == 1, 10)
        assert radio.tx_quiet()
        assert "has restarted" in caplog.text
        assert ("radio", "radio-error", {"what": "restarted", "tx_quiet": True}) in sink.events
