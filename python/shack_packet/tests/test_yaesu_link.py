"""YaesuLink against shack_rig's FakeFtx1 on a pty, its USB codec a
FakeBackend device whose output reaches "the air" only while the pretend
radio transmits with MOD SOURCE on USB. The set-up and its undoing, frames
out and in, busy from RI, keying by CAT and by RTS on the Standard port.
No radio is touched."""

from __future__ import annotations

import time

import pytest

from radiohelp import heard_frames, ui, until
from shack_packet import Engine, Frame, Runner, Station, afsk
from shack_packet.radios import LinkRefused, YaesuLink

pytest.importorskip("shack_rig")
from shack_rig import sound  # noqa: E402
from shack_rig.serialio import SerialPort  # noqa: E402
from shack_rig.yaesu.fake import FakeFtx1  # noqa: E402

CODEC = "FTX-1 USB Audio"


@pytest.fixture
def bench():
    radio = FakeFtx1().start()
    be = sound.FakeBackend([(CODEC, 1, 1, (48000, 44100)), ("Air", 1, 0, (48000,))], realtime=True)
    be.route(CODEC, "Air", gate=radio.transmitting_usb)
    yield radio, be
    be.close()
    radio.stop()


def test_opening_sets_the_ftx1_up_and_closing_puts_it_back(bench):
    radio, be = bench
    link = YaesuLink(radio.device, audio=CODEC, frequency=144_800_000, sound_backend=be)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    assert link.rate == 48000, "the codec's own rate, read before the radio is opened"
    with link:
        assert radio.freq == 144_800_000 and radio.mode == "A", "DATA-FM"
        assert radio.ctcss == 0 and radio.shift == 0
        assert radio.menu[(1, 4, 13)][0] == 1 and radio.menu[(1, 3, 12)][0] == 1, "MOD SOURCE USB, both blocks"
        assert radio.switches["RL"] == 0, "DNR off"
        assert all(link.setup[k] for k in ("frequency", "mode", "shift", "tones", "mod_source_data"))
    assert radio.menu[(1, 4, 13)][0] == 0 and radio.menu[(1, 3, 12)][0] == 0, "put back"
    assert (radio.freq, radio.mode, radio.ctcss, radio.shift) == (145_500_000, "4", 2, 1)
    assert radio.switches["RL"] == 5
    assert radio.tx_state == 0
    assert {w for w, _v in radio.menu_writes} == {(1, 4, 13), (1, 3, 12)}


def test_a_frame_goes_out_on_the_air_and_one_comes_in(bench):
    radio, be = bench
    link = YaesuLink(radio.device, audio=CODEC, frequency=144_800_000, sound_backend=be)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    air = bytearray()
    heard = []
    eng.monitors.append(lambda f: heard.append(f.monitor()))
    with link:
        listener = sound.Input("Air", air.extend, backend=be)
        runner = Runner(eng).start()
        try:
            eng.send_frame(ui())
            assert until(lambda: link.stats["keyed"] == 1 and not link.keyed, 10)
            assert [s for _t, s in radio.ptt_times] == [1, 0], "keyed by TX1; and unkeyed by TX0;"
            sent = Frame.from_monitor("G0ABC>APRS:>heard on the FTX-1")
            be.feed(CODEC, afsk.pcm16(afsk.modulate([sent.encode()], 48000, txdelay_ms=200)))
            assert until(lambda: sent.monitor() in heard, 10), heard
        finally:
            runner.stop()
            listener.close()
    assert heard_frames(air, 48000) == [ui().monitor()]


def test_busy_is_the_radios_squelch(bench):
    radio, be = bench
    link = YaesuLink(radio.device, audio=CODEC, sound_backend=be)
    eng = Engine(Station.of("GM4BRI"), link)
    with link:
        assert until(lambda: eng.channel.radio_busy is False, 2)
        radio.squelch_open = True
        assert until(lambda: eng.channel.radio_busy is True, 2)


def test_rts_on_the_standard_port_keys_only_when_asked(bench):
    radio, be = bench
    radio.menu[(1, 4, 15)][0] = 1                  # RPTT SELECT = RTS in the DATA block
    line = radio.line_port()

    def open_port(device, baud, bits):
        return line if device == "standard" else SerialPort(device, baud, stop_bits=bits)

    link = YaesuLink(radio.device, audio=CODEC, ptt="rts", ptt_device="standard", frequency=144_800_000,
                     sound_backend=be, open_port=open_port)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert radio.line_history == [], "opening raised nothing"
        runner = Runner(eng).start()
        try:
            eng.send_frame(ui())
            assert until(lambda: link.stats["keyed"] == 1 and not link.keyed, 10)
        finally:
            runner.stop()
        assert [s for _t, s in radio.ptt_times] == [2, 0], "keyed by the line, not by CAT"
    changes = [(n, on) for _t, n, on in radio.line_history]
    # Raised once, for the one transmission; lowered after it (and lowered
    # again on closing, which is always sent and always harmless).
    assert changes.count(("rts", True)) == 1 and changes[:2] == [("rts", True), ("rts", False)]
    assert changes[-1] == ("rts", False) and line.closed


def test_without_a_sound_device_there_is_no_packet_link(bench):
    radio, be = bench
    with pytest.raises(LinkRefused, match="setup"):
        YaesuLink(radio.device, audio="", sound_backend=be)


def test_a_radio_that_does_not_answer_is_not_opened(bench):
    radio, be = bench
    radio.answer = False
    link = YaesuLink(radio.device, audio=CODEC, sound_backend=be, connect_s=1.5)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "radio-error"
    assert not link.is_open


def test_the_radios_dial_is_followed_and_never_keyed_on_before_it_is_confirmed(bench):
    """0.3.2: the operator turns the FTX-1's VFO, then its mode to USB."""
    radio, be = bench
    link = YaesuLink(radio.device, audio=CODEC, frequency=144_800_000, sound_backend=be)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert until(lambda: link.tuned()["freq"] == 144_800_000 and link.tuned()["mode"] == "FM", 3)
        radio.freq = 144_925_000                           # the knob
        assert until(lambda: link.tuned()["freq"] == 144_925_000, 3)
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "off-channel" and radio.tx_state == 0
        link.confirm_channel(144_925_000)
        link.key()
        assert until(lambda: radio.tx_state != 0, 2)
        link.abort()
        assert until(lambda: radio.tx_state == 0, 2)
        radio.mode = "2"                                    # USB
        assert until(lambda: link.tuned()["mode"] == "USB", 3)
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "off-mode"
    lines = {ln.key: ln.status for ln in link.setup_restored}
    assert lines["frequency"] == "kept" and lines["mode"] == "kept"
    assert (radio.freq, radio.mode) == (144_925_000, "2"), "both are the operator's now"
    assert radio.menu[(1, 4, 13)][0] == 0, "what the operator did not touch is put back"
