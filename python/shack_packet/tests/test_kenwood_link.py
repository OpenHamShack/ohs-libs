"""KenwoodLink against shack_rig's FakeTmV71 on a pty, its interface a
FakeBackend sound device whose output reaches "the air" only while the
pretend radio transmits, keyed by the interface's RTS wired to the DATA
jack's PKS. The set-up and its undoing, the band, AFSK 1200 and G3RUH 9600
out and in at the interface's 48 kHz, busy from BY, power H/M/L from the
control strip, and never a CAT transmit. No radio is touched."""

from __future__ import annotations

import pytest

from radiohelp import heard_frames, ui, until
from shack_packet import Engine, Frame, Runner, Station, afsk, ax25, fsk, modem
from shack_packet.radios import KenwoodLink, LinkRefused

pytest.importorskip("shack_rig")
from shack_rig import sound  # noqa: E402
from shack_rig.serialio import SerialPort  # noqa: E402
from shack_rig.kenwood.fake import FakeTmV71  # noqa: E402

CARD = "USB Audio Device"


@pytest.fixture
def bench():
    radio = FakeTmV71().start()
    be = sound.FakeBackend([(CARD, 1, 1, (48000, 44100)), ("Air", 1, 0, (48000,))], realtime=True)
    be.route(CARD, "Air", gate=lambda: radio.transmitting)
    port = radio.interface_port("rts")

    def open_port(device, baud, bits):
        return port if device == "digirig" else SerialPort(device, baud, stop_bits=bits)
    yield radio, be, port, open_port
    be.close()
    radio.stop()


def link_for(bench, **kw):
    radio, be, _port, open_port = bench
    kw.setdefault("frequency", 144_800_000)
    return KenwoodLink(radio.device, audio=CARD, ptt="rts", ptt_device="digirig", sound_backend=be,
                       open_port=open_port, **kw)


def test_opening_sets_the_tmv71_up_and_closing_puts_it_back(bench):
    radio = bench[0]
    vfo_a, menu = list(radio.vfo[0]), list(radio.menu)
    link = link_for(bench, speed=9600)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    assert link.rate == 48000 and link.baud == 9600
    with link:
        assert link.setup["band"] == "A", "band A is on 2 m; B (the DATA jack's) is on 70 cm"
        assert radio.freq(0) == 144_800_000 and radio.band_mode[0] == 0, "VFO, tuned"
        assert radio.vfo[0][3] == "0" and radio.vfo[0][5:8] == ["0", "0", "0"], "simplex, no tone"
        assert radio.data_band() == 0 and radio.data_speed() == 9600
        assert all(link.setup[k] for k in ("frequency", "mode", "shift", "tones", "data_band", "data_speed"))
    assert radio.vfo[0] == vfo_a and radio.menu == menu and radio.band_mode[0] == 1, "all put back"
    assert not radio.transmitting and radio.cat_keys == []


def test_a_channel_neither_band_is_on_is_refused_with_what_to_press(bench):
    link = link_for(bench, frequency=1_299_100_000)
    Engine(Station.of("GM4BRI"), link)
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "out-of-range" and "press BAND" in e.value.detail
    assert not link.is_open and bench[0].menu_writes == []


def _send_and_hear(bench, speed, rx_frame_audio):
    radio, be, port, _ = bench
    link = link_for(bench, speed=speed)
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
            assert [b for _t, b in radio.tx_history] == [0, None], "band A keyed by PKS, then released"
            sent = Frame.from_monitor("G0ABC>APRS:>heard on the TM-V71")
            be.feed(CARD, rx_frame_audio(sent))
            assert until(lambda: sent.monitor() in heard, 10), heard
        finally:
            runner.stop()
            listener.close()
    assert ("rts", True) in port.history and radio.cat_keys == []
    return bytes(air)


def test_afsk_1200_goes_out_through_the_interface_and_comes_in(bench):
    air = _send_and_hear(bench, 1200, lambda f: afsk.pcm16(afsk.modulate([f.encode()], 48000, txdelay_ms=200)))
    assert heard_frames(air, 48000) == [ui().monitor()]


def test_g3ruh_9600_goes_out_through_the_interface_at_48k_and_comes_in(bench):
    def g3ruh(frame):
        return afsk.pcm16(modem.modulate([frame.encode()], 48000, mode="g3ruh9600", txdelay_ms=100))
    air = _send_and_hear(bench, 9600, g3ruh)
    rx = fsk.Receiver(48000, 9600)
    got = [h.frame for h in rx.feed(air)] + [h.frame for h in rx.flush()]
    assert [ax25.parse(f).monitor() for f in got] == [ui().monitor()]


def test_a_9600_link_refuses_a_1200_channel(bench):
    from shack_packet.channel import ChannelConfig
    from shack_packet.errors import PacketError
    link = link_for(bench, speed=9600)
    with pytest.raises(PacketError, match="afsk1200"):
        Engine(Station.of("GM4BRI"), link, channel=ChannelConfig(baud=1200))


def test_busy_is_the_bands_squelch(bench):
    radio = bench[0]
    link = link_for(bench)
    eng = Engine(Station.of("GM4BRI"), link)
    with link:
        assert until(lambda: eng.channel.radio_busy is False, 2)
        radio.set_busy(0, True)
        assert until(lambda: eng.channel.radio_busy is True, 2)
        assert link.read_meters() == {"busy": True}


def test_power_is_changed_by_name_and_put_back_and_the_squelch_only_read(bench):
    radio = bench[0]
    link = link_for(bench)
    Engine(Station.of("GM4BRI"), link)
    with link:
        assert link.control_keys() == ["power_level", "squelch"]
        assert link.control_choices("power_level") == [(0, "H"), (1, "M"), (2, "L")]
        assert not link.control_writable("squelch") and link.control_range("squelch") == 31
        assert link.read_control("squelch") == 5
        line = link.set_control("power_level", 2)
        assert line.status == "set" and radio.power[0] == 2
    assert radio.power[0] == 0, "back to H at close"


def test_without_a_sound_device_or_a_ptt_port_there_is_no_link(bench):
    radio, be, _, _ = bench
    with pytest.raises(LinkRefused, match="setup"):
        KenwoodLink(radio.device, audio="", ptt="vox", sound_backend=be)
    with pytest.raises(LinkRefused, match="serial port"):
        KenwoodLink(radio.device, audio=CARD, ptt="rts", sound_backend=be)
    with pytest.raises(LinkRefused, match="1200 or 9600"):
        KenwoodLink(radio.device, audio=CARD, ptt="vox", speed=19200, sound_backend=be)


def test_a_receive_only_link_opens_no_ptt_port(bench):
    radio, be, port, open_port = bench
    link = link_for(bench, transmit=False)
    Engine(Station.of("GM4BRI"), link)
    with link:
        assert not port.opened
    assert not radio.transmitting


def test_the_radios_dial_is_followed_and_never_keyed_on_before_it_is_confirmed(bench):
    """0.3.2: the operator turns the TM-V71's VFO. The poll's FO has it; the
    link will not key there until the app confirms the channel, and at
    close the frequency stays as the operator chose."""
    radio = bench[0]
    link = link_for(bench)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert until(lambda: link.tuned()["freq"] == 144_800_000 and link.tuned()["mode"] == "FM", 3)
        radio.vfo[0][1] = f"{144_925_000:010d}"            # the knob
        assert until(lambda: link.tuned()["freq"] == 144_925_000, 3)
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "off-channel" and not radio.transmitting
        link.confirm_channel(144_925_000)
        link.key()
        assert until(lambda: radio.transmitting, 2)
        link.abort()
        assert until(lambda: not radio.transmitting, 2)
        # The band switched to a memory channel: it would transmit on the
        # memory's frequency, which FO does not show.
        radio.band_mode[0] = 1
        assert until(lambda: link.tuned()["memory"], 5)
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "off-channel"
        radio.band_mode[0] = 0
        assert until(lambda: not link.tuned()["memory"], 5)
    assert {ln.key: ln.status for ln in link.setup_restored}["frequency"] == "kept"
    assert radio.freq(0) == 144_925_000 and radio.band_mode[0] == 1, "the channel kept, the rest put back"
    assert radio.cat_keys == []
