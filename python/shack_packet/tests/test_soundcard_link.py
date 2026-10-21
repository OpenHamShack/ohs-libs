"""SoundcardLink: any radio on a sound card, PTT by a serial line, VOX or
none - against shack_rig's FakeBackend and a serial port stand-in (a pty
has no modem lines). The end-to-end test is two engines holding an AX.25
connection through two fake interfaces, each "radio" passing audio only
while its PTT line is up."""

from __future__ import annotations

import time

import pytest

from radiohelp import heard_frames, ui, until
from shack_packet import Engine, Runner, Station
from shack_packet.radios import LinkRefused, SoundcardLink

pytest.importorskip("shack_rig")
from shack_rig import sound  # noqa: E402


class LinePort:
    """serialio.SerialPort as a PTT driver sees it, recording every line."""

    def __init__(self, device):
        self.device = device
        self.opened = self.closed = False
        self.line_state = {"rts": False, "dtr": False}
        self.history: list[tuple[float, str, bool]] = []

    def open(self):
        self.opened = True

    def set_line(self, name, on):
        if not self.opened or self.closed:
            return False
        self.line_state[name] = on
        self.history.append((time.monotonic(), name, on))
        return True

    def close(self):
        self.line_state = {"rts": False, "dtr": False}
        self.closed = True


def backend(*names, rates=(48000,)):
    return sound.FakeBackend([(n, 1, 1, rates) for n in names], realtime=True)


def test_the_link_runs_at_the_interfaces_own_rate_and_refuses_bad_set_ups():
    be = backend("SignaLink", rates=(44100,))
    try:
        link = SoundcardLink("SignaLink", ptt="vox", sound_backend=be)
        assert link.rate == 44100, "no resampling: the modem runs at the card's rate"
        with pytest.raises(LinkRefused, match="setup"):
            SoundcardLink("SignaLink", ptt="rts", sound_backend=be)        # no port named
        with pytest.raises(LinkRefused, match="setup"):
            SoundcardLink("Nowhere", ptt="none", sound_backend=be)
        with pytest.raises(LinkRefused, match="setup"):
            SoundcardLink("", ptt="none", sound_backend=be)                # never the default
    finally:
        be.close()


def test_receive_only_never_keys():
    be = backend("Scanner")
    link = SoundcardLink("Scanner", ptt="none", sound_backend=be)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    try:
        with link:
            with pytest.raises(LinkRefused) as e:
                link.key()
            assert e.value.reason == "receive-only"
            eng.channel.config.persist = 255
            eng.send_ui("APRS", ">hello")
            eng.poll()
            assert eng.channel.stats.refused["receive-only"] == 1
            assert be.stream("Scanner", output=True) is None, "no output was ever opened"
    finally:
        be.close()


def test_vox_is_allowed_with_its_warning_and_a_longer_turnaround():
    be = backend("Handheld")
    link = SoundcardLink("Handheld", ptt="vox", sound_backend=be)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    try:
        with link:
            assert until(lambda: any(e.reason == "vox" for e in eng.events), 1)
            assert link.turnaround_s == SoundcardLink.VOX_TURNAROUND_S
            assert eng.channel.txdelay_s >= link.turnaround_s + 0.08
            link.key()
            link.send_pcm(b"\x00\x10" * 4800)
            link.unkey()
            assert until(lambda: not link.keyed, 2)
            assert be.stream("Handheld", output=True) is None, "the output is let go after the over"
            assert sum(len(s.played) for s in be.streams if s.output) >= 9600
    finally:
        be.close()


def test_two_stations_connect_through_their_interfaces():
    be = backend("A interface", "B interface")
    ports = {}

    def port(device):
        ports[device] = LinePort(device)
        return ports[device]

    links = {call: SoundcardLink(f"{call[0]} interface", ptt="rts", ptt_device=f"/dev/{call}",
                                 sound_backend=be, open_port=port)
             for call in ("A0AAA", "B0BBB")}
    # Each radio puts its audio on the other's receiver only while its PTT
    # line is up: a transmitter, as far as the far end can tell.
    be.route("A interface", "B interface", gate=lambda: ports["/dev/A0AAA"].line_state["rts"])
    be.route("B interface", "A interface", gate=lambda: ports["/dev/B0BBB"].line_state["rts"])
    a = Engine(Station.of("A0AAA", tx_enabled=True), links["A0AAA"])
    b = Engine(Station.of("B0BBB", tx_enabled=True), links["B0BBB"])
    got = bytearray()
    b.listen("B0BBB", lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
    runners = []
    try:
        for link in links.values():
            link.open()
        assert all(p.opened and p.history == [] for p in ports.values()), "opened low, nothing raised"
        runners = [Runner(a).start(), Runner(b).start()]
        conn = a.connect("B0BBB")
        assert until(lambda: conn.connected, 20), f"never connected: {a.events}"
        conn.send(b"hello through two sound cards\r")
        assert until(lambda: bytes(got) == b"hello through two sound cards\r", 20)
        conn.close()
        assert until(lambda: not conn.connected, 20)
    finally:
        for r in runners:
            r.stop()
        for link in links.values():
            link.close()
        be.close()
    for p in ports.values():
        # The line went up and down in pairs, and was down at the end.
        states = [on for _t, _n, on in p.history]
        assert states and states[::2] == [True] * len(states[::2]) and states[1::2] == [False] * len(states[1::2])
        assert states[-1] is False and p.closed


def test_what_a_station_sends_decodes_and_is_sent_only_while_keyed():
    be = backend("Interface", "Monitor")
    ports = {}

    def port(device):
        ports[device] = LinePort(device)
        return ports[device]

    link = SoundcardLink("Interface", ptt="dtr", ptt_device="COM9", sound_backend=be, open_port=port)
    be.route("Interface", "Monitor", gate=lambda: ports["COM9"].line_state["dtr"])
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    heard = bytearray()
    monitor = sound.Input("Monitor", heard.extend, backend=be)
    runner = None
    try:
        link.open()
        runner = Runner(eng).start()
        eng.send_frame(ui())
        assert until(lambda: link.stats["keyed"] == 1 and not link.keyed, 10)
        time.sleep(0.1)
    finally:
        if runner:
            runner.stop()
        link.close()
        monitor.close()
        be.close()
    assert heard_frames(heard, 48000) == [ui().monitor()]
