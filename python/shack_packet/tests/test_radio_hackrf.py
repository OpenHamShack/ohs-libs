"""The HackRF as a packet radio (radios/hackrf.py), through its real ctypes
binding against a stand-in libhackrf (hackrfhelp): half-duplex keying, the
transceiver OFF at every open and unkey, frames out through the FM
modulator and back through the receiver, the unkey confirmed from the
firmware's own state, the emergency unkey round a stuck link, and transmit
refused on firmware too old to stop on an underrun."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from hackrfhelp import HackrfControl, stub_libhackrf
from radiohelp import Sink, heard_frames, until

from shack_packet import Station, afsk
from shack_packet.ax25 import Frame
from shack_packet.radios import REASONS, HackRFLink, LinkRefused
from shack_packet.radios.hackrf import iq_int8
from shack_packet.radios.pluto import FmModulator
from shack_packet.radios.sdrrx import FmChannelReceiver

FREQ = 144_800_000
IFO = 100_000
RATE = 2_400_000
ARMED = Station.of("GM4BRI", tx_enabled=True)


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    return stub_libhackrf(tmp_path_factory.mktemp("hackrf"))


@pytest.fixture
def ctl(stub):
    return HackrfControl(stub)


def make(stub, station=ARMED, **kw):
    link = HackRFLink(FREQ, lib_path=stub, **kw)
    sink = Sink(station)
    link.attach(sink)
    return link, sink


def frames(tag, n=2):
    return [Frame.from_monitor(f"GM4BRI>APZOHS:>{tag} {i}").encode() for i in range(n)]


def over(tag, n=2) -> bytes:
    return afsk.pcm16(afsk.modulate(frames(tag, n), 12_000, amplitude=0.5, txdelay_ms=300))


def heard_in_iq(raw: bytes) -> set[str]:
    x = np.frombuffer(raw, np.int8).astype(np.float32) / 128.0
    rx = FmChannelReceiver(RATE, FREQ - IFO, FREQ)
    pcm = bytearray()
    for at in range(0, len(x), 2 * 120_000):
        pcm += rx.hear(x[at:at + 2 * 120_000].view(np.complex64))
    return set(heard_frames(pcm, 12_000))


def test_open_puts_the_transceiver_off_first_then_receives(stub, ctl):
    ctl.lib.stub_set_mode(2)                             # left transmitting by a crashed run
    link, _ = make(stub)
    with link:
        calls = ctl.calls()
        assert calls[:3] == ["init", "open", "stop_tx"]
        assert ("freq", FREQ - IFO) in ctl.log() and ("rate", RATE) in ctl.log()
        assert ctl.mode() == 1 and link.tx_refused_why is None
    assert ctl.mode() == 0 and ctl.calls()[-2:] == ["close", "exit"]


def test_frames_on_the_air_reach_the_engine(stub, ctl):
    audio = np.frombuffer(over("heard", 3), "<i2").astype(np.float64) / 32768.0
    iq = FmModulator(RATE, 3_000.0, IFO, amplitude=0.5)(np.concatenate([np.zeros(6_000), audio, np.zeros(6_000)]))
    ctl.lib.stub_set_rx_iq(iq_int8(iq), len(iq) * 2)
    link, sink = make(stub)
    with link:
        assert until(lambda: len(set(heard_frames(sink.pcm, 12_000))) == 3, 15)


def test_an_over_goes_out_half_duplex_and_receive_resumes(stub, ctl):
    link, sink = make(stub, tx_vga_db=12)
    with link:
        link.key()
        assert ctl.mode() == 2
        link.send_pcm(over("sent"))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        assert until(lambda: ctl.mode() == 1, 2)          # back to receive
        calls = ctl.calls()
        k = calls.index("start_tx")
        assert calls[calls.index("stop_rx")] and calls.index("stop_rx") < k
        assert ("txvga", 12) in ctl.log() and ("amp", 0) in ctl.log()   # the RF amp stays off unless asked
        assert ("underrun", int(0.25 * RATE * 2)) in ctl.log()
    assert heard_in_iq(ctl.tx_bytes()) == {f"GM4BRI>APZOHS:>sent {i}" for i in range(2)}
    assert not [e for e in sink.events if e[0] == "unkeyed"]


def test_closing_while_keyed_puts_it_off(stub, ctl):
    link, _ = make(stub)
    link.open()
    link.key()
    link.send_pcm(over("cut", 4))
    assert until(lambda: ctl.lib.stub_tx_len() > 0, 2)
    link.close()
    assert ctl.mode() == 0 and not link.keyed


def test_old_firmware_opens_receive_only(stub, ctl):
    ctl.lib.stub_set_api(0x0105)
    link, sink = make(stub)
    with link:
        assert link.tx_refused_why and "0x0105" in link.tx_refused_why
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "tx-firmware" and ctl.mode() == 1
        assert until(lambda: "tx-firmware" in sink.reasons())
    assert "start_tx" not in ctl.calls() and "tx-firmware" in REASONS


def test_the_unkey_is_confirmed_from_the_firmwares_own_state(stub, ctl):
    link, _ = make(stub)
    with link:
        assert link.CONFIRMS_UNKEY
        link.key()
        assert link._radio_ptt_readback() is True
        link.send_pcm(over("c", 1))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        # The firmware back in TX behind the link's back (it would not be):
        # the read-back sees it and the unkey goes again.
        ctl.lib.stub_set_mode(2)
        assert until(lambda: link.stats["unkey-retried"] >= 1, 2)
        assert until(lambda: ctl.mode() in (0, 1), 1)


def test_the_emergency_unkey_works_with_the_link_stuck(stub, ctl):
    link, _ = make(stub)
    link.open()
    try:
        link.key()
        link.send_pcm(over("stuck", 6))
        held = threading.Event()
        release = threading.Event()

        def stuck():
            with link._lock, link._hw_lock:          # a supervisor and a key both stuck holding their locks
                held.set()
                release.wait(5)
        threading.Thread(target=stuck, daemon=True).start()
        assert held.wait(1)
        t0 = time.monotonic()
        link.emergency_unkey()
        assert time.monotonic() - t0 < 0.5 and ctl.mode() == 0
        release.set()
    finally:
        link.close()


def test_settings_out_of_bounds_are_refused_when_the_link_is_made(stub):
    for kw in ({"tx_vga_db": 48}, {"tx_vga_db": -1}):
        with pytest.raises(LinkRefused):
            HackRFLink(FREQ, lib_path=stub, **kw)
    with pytest.raises(LinkRefused) as e:
        HackRFLink(7.5e9, lib_path=stub)
    assert e.value.reason == "out-of-range"


def test_transmit_not_set_up_is_receive_only(stub, ctl):
    link, _ = make(stub, transmit=False)
    with link:
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "receive-only" and ctl.mode() == 1
    assert "start_tx" not in ctl.calls()
