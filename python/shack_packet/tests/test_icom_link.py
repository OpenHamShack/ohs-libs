"""IcomLink against shack_rig's FakeIcom (the LAN protocol, on 127.0.0.1)
and against a CI-V radio on a pty with a stand-in aplay (USB): the set-up
it makes and puts back, frames out that decode and are sent only while
keyed, frames in, the squelch as busy, a disarm mid-over, the IC-705's
unsourced DATA MOD. Nothing leaves the loopback; no radio is touched."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
import time

import pytest

from radiohelp import heard_frames, ui, until
from shack_packet import Engine, Frame, Runner, Station, afsk
from shack_packet.radios import IcomLink, LinkRefused

pytest.importorskip("shack_rig")
from shack_rig.icom import civ  # noqa: E402
from shack_rig.icom.fake import FakeIcom  # noqa: E402


class LoopRadio:
    """A FakeIcom on a loop of its own, as a real radio runs apart from us."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.radio = FakeIcom(username="op", password="secret")
        asyncio.run_coroutine_threadsafe(self.radio.start(), self.loop).result(5)

    def profile(self, **kw):
        return {"model": "ic-9700", "host": "127.0.0.1", "port": self.radio.control_port, "username": "op", **kw}

    def close(self):
        asyncio.run_coroutine_threadsafe(self.radio.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def fake():
    f = LoopRadio()
    yield f
    f.close()


def on_air(frames, rate=12000):
    """A test's signal: frames as AFSK, then silence, 20 ms at a time."""
    pcm = afsk.pcm16(afsk.modulate([f.encode() for f in frames], rate, txdelay_ms=200))
    pos = [0]

    def source(r, n):
        out = pcm[pos[0]:pos[0] + 2 * n]
        pos[0] += 2 * n
        return out.ljust(2 * n, b"\0")
    return source


def test_opening_sets_the_radio_up_for_packet_and_closing_puts_it_back(fake, tmp_path):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000, setup_dir=tmp_path)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert radio.freq == 144_800_000
        assert (radio.mode, radio.data, radio.filter) == (0x05, 0x01, 0x01), "FM-D, FIL1"
        assert radio.duplex == 0x10 and set(radio.tone_functions.values()) == {0}
        assert radio.functions[0x40] == 0, "NR off: it ruins AFSK receive"
        assert radio.data_mod == 0x05, "DATA MOD on the network"
        assert radio.setup_menu[115] == 0x00, "DATA OFF MOD = MIC"
        changed = {ln.key for ln in link.setup_report if ln.status == "set"}
        assert changed == {"frequency", "mode", "simplex", "tone", "nr", "data_mod", "data_off_mod"}
        assert all(link.setup[k] for k in ("frequency", "mode", "simplex", "data_mod"))
        assert link.rate == 12000 and link.turnaround_s == IcomLink.DEFAULT_TURNAROUND_S
        assert list(tmp_path.glob("icom-ic-9700-127-0-0-1.json")), "saved on disk while changed"
        assert until(lambda: any(e.reason == "setup-report" for e in eng.events), 2)
    assert radio.data_mod == 0x00, "put back as found"
    assert radio.functions[0x40] == 1 and radio.tone_functions[0x42] == 1 and radio.setup_menu[115] == 0x02
    assert radio.duplex == 0x12, "the repeater shift it was found on, read and put back"
    assert radio.freq == 144_174_000 and (radio.mode, radio.data) == (0x01, 0x01)
    assert {ln.status for ln in link.setup_restored} == {"restored"}
    assert radio.menu_writes == [] and not radio.ptt
    assert not list(tmp_path.iterdir()), "nothing left to put back"


def test_a_session_that_died_is_undone_by_the_next_open(fake, tmp_path):
    """The app is killed with the radio set up: its journal is all that is
    left. The next link on the same radio puts the radio back first."""
    radio = fake.radio
    first = IcomLink(fake.profile(), password="secret", frequency=144_800_000, setup_dir=tmp_path)
    Engine(Station.of("GM4BRI", tx_enabled=True), first)
    first.open()
    assert radio.data_mod == 0x05 and radio.functions[0x40] == 0
    first._data_setup = None                               # dies here: nothing is put back
    first.close()
    assert radio.data_mod == 0x05, "the dead session's close did not restore"
    assert list(tmp_path.iterdir())
    second = IcomLink(fake.profile(), password="secret", frequency=144_800_000, setup_dir=tmp_path)
    Engine(Station.of("GM4BRI", tx_enabled=True), second)
    with second:
        assert {ln.key for ln in second.setup_recovered} >= {"frequency", "nr", "data_mod"}
        assert {ln.status for ln in second.setup_recovered} == {"recovered"}
        assert {ln.key for ln in second.setup_report if ln.status == "set"} >= {"nr", "data_mod"}, \
            "found as the operator left it, then set again"
    assert radio.data_mod == 0x00 and radio.functions[0x40] == 1 and radio.freq == 144_174_000
    assert not list(tmp_path.iterdir())


def test_a_wrong_password_fails_the_open_at_once_and_says_why(fake):
    """The radio refusing the login ends the open straight away with its own
    reason (the app shows it and waits for Reconnect), instead of waiting
    out CONNECT_S while the rig asks again."""
    link = IcomLink(fake.profile(), password="wrong", transmit=False, connect_s=15)
    t0 = time.monotonic()
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "radio-auth" and "username and password" in e.value.detail
    assert time.monotonic() - t0 < 5


def test_a_mod_level_given_is_set_and_put_back(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000, mod_level_percent=40)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert radio.setup_menu[114] == 102, "LAN MOD Level 40%"
    assert radio.setup_menu[114] == 128


def test_a_frame_goes_out_decodes_and_only_while_keyed(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        runner = Runner(eng).start()
        try:
            eng.send_frame(ui())
            assert until(lambda: link.stats["keyed"] == 1 and not link.keyed, 10)
            assert until(lambda: radio.ptt_commands == [True, False], 2), radio.ptt_commands
        finally:
            runner.stop()
    assert heard_frames(radio.tx_pcm, 12000) == [ui().monitor()]
    assert radio.tx_pcm_unkeyed == 0, "no audio reached the radio while it was not keyed"
    key_t = radio.ptt_times[0][0]
    unkey_t = radio.ptt_times[1][0]
    assert all(key_t <= t <= unkey_t for t, _k, _n in radio.tx_log)


def test_an_over_reaches_the_radio_unbroken_with_its_loop_kept_busy(fake):
    # The 18/10/2026 real-radio run: holes of 15-20 ms about every 100 ms in
    # what an IC-9700 transmitted over the LAN, and nothing decoded. Here the
    # whole path (pacing, the rig's loop, the LAN audio stream) against the
    # fake's playout model, with the rig's loop stalled up to 30 ms at a
    # time as receive work on it might: no hole longer than 2 ms, and the
    # sender's own count of late audio agrees.
    import random
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=432_650_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    rng = random.Random(3)
    stalling = [True]

    def stall():
        if stalling[0]:
            time.sleep(rng.uniform(0, 0.03))
            link._loop.call_later(0.04, stall)

    with link:
        time.sleep(0.3)
        link._loop.call_soon_threadsafe(stall)
        pcm = afsk.pcm16(afsk.modulate([ui().encode()] * 3, 12000, txdelay_ms=300))
        link.key()
        link.send_pcm(pcm)
        link.unkey()
        assert until(lambda: link.stats["keyed"] == 1 and not link.keyed, 10)
        stalling[0] = False
        holes = [h for h in radio.tx_playout() if h[1] > 0.002]
        assert holes == [], f"holes (at s, for s): {holes}"
        assert link.rig._link._audio.tx_underruns == 0
    assert heard_frames(radio.tx_pcm, 12000) == [ui().monitor()] * 3


def test_frames_heard_and_the_squelch_as_busy(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret")
    eng = Engine(Station.of("GM4BRI"), link)
    heard = []
    eng.monitors.append(lambda f: heard.append(f.monitor()))
    sent = Frame.from_monitor("G0ABC-1>APRS,WIDE1-1:!5600.00N/00300.00W-heard over the LAN")
    with link:
        radio.audio_source = on_air([sent])
        assert until(lambda: heard == [sent.monitor()], 10), heard
        assert eng.channel.radio_busy is False
        radio.squelch_open = True
        assert until(lambda: eng.channel.radio_busy is True, 3)
        radio.squelch_open = False
        assert until(lambda: eng.channel.radio_busy is False, 3)


def test_disarming_mid_over_unkeys_the_radio_at_once(fake):
    radio = fake.radio
    station = Station.of("GM4BRI", tx_enabled=True)
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    Engine(station, link)
    with link:
        link.key()
        link.send_pcm(b"\x00\x10" * 12000 * 3)
        assert until(lambda: radio.ptt, 2)
        time.sleep(0.3)
        station.tx_enabled = False
        t = time.monotonic()
        assert until(lambda: not radio.ptt, 1)
        assert time.monotonic() - t < 0.3
        assert link.stats["disarmed"] == 1


# -- the operator's own VFO (0.3.2) --------------------------------------------

def test_the_radios_dial_is_followed_and_never_keyed_on_before_it_is_confirmed(fake, tmp_path):
    """Brian, 21/10/2026: he turned the IC-9700's VFO and the app went on as
    if it had not. The radio volunteers the change (CI-V transceive, 00 to
    the broadcast address) and the link's cache has it; the link will not
    key there until the app confirms the channel, and at close the
    frequency he chose stays while the rest of the set-up goes back."""
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_925_000, setup_dir=tmp_path)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert until(lambda: link.tuned()["freq"] == 144_925_000 and link.tuned()["mode"] == "FM", 2)
        assert link.tuned()["data"] is True and link.tuned()["label"] == "FM-D"
        fake.loop.call_soon_threadsafe(radio.turn_dial, 144_800_000)
        assert until(lambda: link.tuned()["freq"] == 144_800_000, 2)
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "off-channel"
        assert True not in radio.ptt_commands, "nothing reached the radio"
        link.confirm_channel(144_800_000)
        link.key()
        assert until(lambda: radio.ptt, 2)
        link.abort()
        assert until(lambda: not radio.ptt, 2)
    lines = {ln.key: ln.status for ln in link.setup_restored}
    assert lines["frequency"] == "kept" and lines["mode"] == "restored"
    assert radio.freq == 144_800_000, "the operator's channel, not the one the radio was found on"
    assert (radio.mode, radio.data) == (0x01, 0x01) and radio.data_mod == 0x00, "the rest put back"
    assert not list(tmp_path.iterdir())


def test_a_dial_turned_mid_over_unkeys_the_radio(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_925_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        link.key()
        link.send_pcm(b"\x00\x10" * 12000 * 3)
        assert until(lambda: radio.ptt, 2)
        fake.loop.call_soon_threadsafe(radio.turn_dial, 145_500_000)
        t = time.monotonic()
        assert until(lambda: not radio.ptt, 2)
        assert time.monotonic() - t < 0.6
        assert link.stats["off-channel"] == 1


def test_the_mode_knob_off_fm_stops_keying_and_a_pass_owns_the_vfos(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_925_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        fake.loop.call_soon_threadsafe(radio.turn_dial, None, 0x01)     # USB
        assert until(lambda: link.tuned()["mode"] == "USB", 2)
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "off-mode"
        # A satellite pass steps the VFOs for Doppler on purpose: the guard
        # and tuned() stand aside while it owns them.
        link.vfo_owner = "satellite"
        assert link.tuned() is None and link._channel_refusal() is None
        link.vfo_owner = None
        assert link._channel_refusal() == "off-mode"


# -- the real-radio test's "lowest power" step -------------------------------

def test_minimum_power_is_set_before_the_first_key_and_restored_at_close(fake):
    radio = fake.radio
    radio.rf_power_level = 200
    station = Station.of("GM4BRI", tx_enabled=True)
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000,
                     minimum_power_for_test=True)
    Engine(station, link)
    with link:
        link.key()
        assert until(lambda: radio.ptt, 2)
        assert radio.rf_power_level == 0, "set to minimum before the key reached the radio"
        link.unkey()
        assert until(lambda: not radio.ptt, 2)
        writes_after_first_key = len(radio.rf_power_writes)
        # A second key in the same armed session costs no extra read/set/read:
        # the radio keeps the setting itself.
        link.key()
        assert until(lambda: radio.ptt, 2)
        link.unkey()
        assert until(lambda: not radio.ptt, 2)
        assert len(radio.rf_power_writes) == writes_after_first_key, "checked once a session, not every over"
    assert radio.rf_power_level == 200, "restored at close"
    assert radio.rf_power_writes[-1] == 200


def test_keying_is_refused_if_the_radio_ignores_the_minimum_power_set(fake):
    radio = fake.radio
    radio.rf_power_level = 200
    radio.ignore_rf_power_writes = True
    station = Station.of("GM4BRI", tx_enabled=True)
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000,
                     minimum_power_for_test=True)
    Engine(station, link)
    with link:
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "radio-refused"
        assert "RF power level" in e.value.detail
    assert True not in radio.ptt_commands, "PTT never rose"
    assert radio.rf_power_level == 200, "the ignored set never took, and nothing here undoes it"


def test_no_restore_if_the_level_was_never_changed(fake):
    radio = fake.radio
    radio.rf_power_level = 180
    station = Station.of("GM4BRI", tx_enabled=True)
    # The test setting is off, so the minimum-power step never runs.
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    Engine(station, link)
    with link:
        link.key()
        assert until(lambda: radio.ptt, 2)
        link.unkey()
        assert until(lambda: not radio.ptt, 2)
    assert radio.rf_power_writes == [], "never touched, so never written back"
    assert radio.rf_power_level == 180


def test_a_receive_only_station_asks_the_radio_for_no_transmit_stream(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", transmit=False)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        req = radio.stream_requests[-1]
        assert req["tx_enable"] == 0
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "receive-only"
    assert True not in radio.ptt_commands, "never keyed (unkeys are always sent, and harmless)"


def test_an_ic705_on_wifi_is_told_what_to_set_by_hand(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(model="ic-705"), password="secret")
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert link.setup["data_mod"] is False and "IC-705" in link.setup["data_mod_note"]
        assert until(lambda: any(e.reason == "setup" and e.detail.get("what") == "data_mod" for e in eng.events), 2)
        assert radio.data_mod_writes == [] and radio.setup_menu_writes == [], \
            "an unsourced menu item is never written"
        assert radio.functions[0x40] == 0, "its NR switch is hamlib-sourced, and set"
    assert radio.functions[0x40] == 1


# -- USB: a CI-V radio on a pty, transmit audio through aplay ---------------------


class PtyRadio:
    """Just enough of an Icom on a pty: answers 19 00, 03, 26 and 1C 00, keys
    on 1C 00 01, and records what it was sent. With `answer_all`, anything
    else gets NG (FA) as a real radio's unknown command does, rather than
    silence: the cable's writer waits up to 50 ms for an answer to each
    command (shack_rig.icom.usb REPLY_TIMEOUT), so silence puts that much
    in front of every key and unkey queued behind the rig's poll."""

    def __init__(self, addr=0xA4, freq=144_800_000, answer_all=False):
        self.addr, self.freq, self.answer_all = addr, freq, answer_all
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.ptt = False
        self.keys: list[tuple[float, bool]] = []
        #: 1A 05 items and 16 xx switches it keeps, when a test gives it
        #: some (item -> one-byte value, sub -> 0/1); others are as before.
        self.menu: dict[int, int] = {}
        self.switches: dict[int, int] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        import select
        buf = b""
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                buf += os.read(self.master, 512)
            except OSError:
                return
            frames, buf = civ.split_frames(buf)
            for f in frames:
                to, frm, p = civ.parse(f)
                if to != self.addr or not p:
                    continue
                body = None
                if p[:2] == b"\x19\x00":
                    body = bytes([0x19, 0x00, self.addr])
                elif p == b"\x03":
                    body = b"\x03" + civ.encode_freq(self.freq)
                elif p[:1] == b"\x26" and len(p) <= 2:
                    body = bytes([0x26, 0x00, 0x05, 0x01, 0x01])
                elif p == b"\x1c\x00":
                    body = b"\x1c\x00" + (b"\x01" if self.ptt else b"\x00")
                elif p[:2] == b"\x1c\x00" and len(p) == 3:
                    self.ptt = bool(p[2])
                    self.keys.append((time.monotonic(), self.ptt))
                    body = b"\xfb"
                elif p[:2] == b"\x1a\x05" and len(p) >= 4 and int(f"{p[2]:02x}{p[3]:02x}") in self.menu:
                    item = int(f"{p[2]:02x}{p[3]:02x}")
                    if len(p) == 4:
                        body = p + bytes([self.menu[item]])
                    else:
                        self.menu[item] = p[4]
                        body = b"\xfb"
                elif p[:1] == b"\x16" and len(p) >= 2 and p[1] in self.switches:
                    if len(p) == 2:
                        body = p + bytes([self.switches[p[1]]])
                    else:
                        self.switches[p[1]] = p[2]
                        body = b"\xfb"
                elif p[:1] in (b"\x05", b"\x26", b"\x0f", b"\x16"):
                    body = b"\xfb"
                elif self.answer_all:
                    body = b"\xfa"
                if body is not None:
                    os.write(self.master, civ.frame(body, to=frm, frm=self.addr))

    def close(self):
        self._stop.set()
        self._t.join(2)
        for fd in (self.master, self.slave):
            os.close(fd)


def test_an_ic705_on_its_usb_cable_transmits_through_aplay(tmp_path, monkeypatch):
    from shack_rig.icom import usbprobe
    monkeypatch.setattr(usbprobe, "BY_ID", str(tmp_path / "none"))
    aplay = tmp_path / "aplay"
    aplay.write_text(f"""#!{sys.executable}
import sys
if "--dump-hw-params" in sys.argv:
    sys.stderr.write("CHANNELS: 1\\nRATE: [8000 48000]\\n"); sys.exit(0)
with open({str(tmp_path / 'played')!r}, "ab") as f:
    f.write(sys.stdin.buffer.read())
""")
    aplay.chmod(aplay.stat().st_mode | stat.S_IEXEC)
    radio = PtyRadio()
    profile = {"model": "ic-705", "transport": "usb", "civ_device": radio.path, "civ_baud": 115200,
               "audio_rate": 0, "audio_device": "hw:9,0", "tx_audio": "alsa"}
    link = IcomLink(profile, frequency=144_800_000)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    from shack_rig.icom import usb as icomusb
    real = icomusb.UsbLink.__init__

    def with_fake_aplay(self, *a, **kw):
        kw["aplay"] = str(aplay)
        real(self, *a, **kw)

    monkeypatch.setattr(icomusb.UsbLink, "__init__", with_fake_aplay)
    try:
        with link:
            assert link.data_source == "usb"
            runner = Runner(eng).start()
            try:
                eng.send_frame(ui())
                assert until(lambda: link.stats["keyed"] == 1 and not link.keyed, 10)
                assert until(lambda: [k for _t, k in radio.keys] == [True, False], 3), radio.keys
            finally:
                runner.stop()
        time.sleep(0.3)
    finally:
        radio.close()
    played = (tmp_path / "played").read_bytes()
    assert heard_frames(played, 48000) == [ui().monitor()], "12 kHz in, the codec's 48 kHz out, and it decodes"


def test_an_ic9700_on_its_usb_cable_gets_data_mod_back_when_the_link_closes(tmp_path, monkeypatch):
    """18/10/2026: after a USB session the 9700 was left on DATA MOD = USB.
    The restore had been queued behind the poll and was thrown away when
    the port closed. Now it is written, read back, and the port drained."""
    from shack_rig.icom import usbprobe
    monkeypatch.setattr(usbprobe, "BY_ID", str(tmp_path / "none"))
    radio = PtyRadio(addr=0xA2, answer_all=True)
    radio.menu = {116: 0x05, 115: 0x02, 107: 0x00}                  # DATA MOD LAN, as Brian's was
    radio.switches = {0x40: 0x01}                                  # NR on
    profile = {"model": "ic-9700", "transport": "usb", "civ_device": radio.path, "civ_baud": 115200,
               "audio_rate": 0}
    link = IcomLink(profile, frequency=144_800_000, transmit=False, setup_dir=tmp_path / "setup")
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    try:
        with link:
            assert radio.menu[116] == 0x03, "DATA MOD = USB for the cable"
            assert radio.switches[0x40] == 0x00, [ln.text() for ln in link.setup_report]
        assert radio.menu[116] == 0x05, "and LAN again the moment close() returns"
        assert radio.switches[0x40] == 0x01 and radio.menu[115] == 0x02
        assert {ln.status for ln in link.setup_restored} == {"restored"}
        assert not list((tmp_path / "setup").iterdir())
    finally:
        radio.close()


def test_the_rig_unkeys_by_itself_if_the_links_supervisor_is_stuck(fake):
    """The second road to an unkey: the rig re-asks the link's lease on its
    own poll, so a watchdog limit passed while this link's supervisor does
    nothing still unkeys the radio."""
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000, max_key_s=0.4)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        link._tick = lambda events: None            # the supervisor, stuck
        link.key()
        assert until(lambda: radio.ptt, 2)
        t = time.monotonic()
        assert until(lambda: not radio.ptt, 2), "the rig's own lease check unkeyed it"
        assert 0.2 < time.monotonic() - t < 0.8


# -- stuck-transmit defences (Ray's IC-9700 on USB, 18/10/2026) --------------------


def test_an_unkey_the_radio_ignores_is_found_by_reading_ptt_back_and_sent_again(fake):
    radio = fake.radio
    real = radio._answer
    ignored = []

    def deaf_once(body):
        if body == b"\x1c\x00\x00" and not ignored:
            ignored.append(body)                    # lost on the way, or NG'd: the radio stays keyed
            return
        real(body)

    radio._answer = deaf_once
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        link.key()
        assert until(lambda: radio.ptt, 2)
        link.unkey()
        assert until(lambda: ignored and not radio.ptt, 3), "the read-back found it keyed and unkeyed it again"
        assert link.stats["unkey-retried"] >= 1
        assert until(lambda: link._confirm is None, 2)


def test_a_key_that_reaches_the_rig_after_key_gave_up_is_refused_there(fake):
    # 18/10/2026, our side: TimeoutError from rig.set_ptt while the rig's
    # loop was busy. The key was still queued on that loop and ran after
    # key() had given up - into a lease that still permitted. Now the lease
    # says "not keyed" and the rig never sends it.
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        time.sleep(0.3)
        link._loop.call_soon_threadsafe(time.sleep, 2.5)   # the loop busy past _call's 2 s
        time.sleep(0.05)
        with pytest.raises(LinkRefused):
            link.key()
        time.sleep(1.5)
        assert True not in radio.ptt_commands, radio.ptt_commands
        assert not radio.ptt


def test_transmit_audio_that_blocks_the_rigs_loop_cannot_hold_a_usb_radio_keyed(tmp_path, monkeypatch):
    # Ray's 12:00:25: the transmit audio's call into the sound device blocked
    # and nothing unkeyed for two minutes. Here the rig's send_audio never
    # returns, which stops the rig's loop dead - its poll, its lease check,
    # every call the link makes - and the hard watchdog writes the unkey to
    # the serial port itself within max_key_s + HARD_MARGIN_S.
    from shack_rig.icom import usbprobe
    monkeypatch.setattr(usbprobe, "BY_ID", str(tmp_path / "none"))
    aplay = tmp_path / "aplay"
    aplay.write_text(f"""#!{sys.executable}
import sys
if "--dump-hw-params" in sys.argv:
    sys.stderr.write("CHANNELS: 1\\nRATE: [8000 48000]\\n"); sys.exit(0)
sys.stdin.buffer.read()
""")
    aplay.chmod(aplay.stat().st_mode | stat.S_IEXEC)
    from shack_rig.icom import usb as icomusb
    real = icomusb.UsbLink.__init__

    def with_fake_aplay(self, *a, **kw):
        kw["aplay"] = str(aplay)
        real(self, *a, **kw)

    monkeypatch.setattr(icomusb.UsbLink, "__init__", with_fake_aplay)
    radio = PtyRadio()
    profile = {"model": "ic-705", "transport": "usb", "civ_device": radio.path, "civ_baud": 115200,
               "audio_rate": 0, "audio_device": "hw:9,0", "tx_audio": "alsa"}
    link = IcomLink(profile, frequency=144_800_000, max_key_s=0.8)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    hung = threading.Event()
    try:
        with link:
            link.rig.send_audio = lambda pcm: hung.wait() or True
            link.key()
            t0 = time.monotonic()
            assert until(lambda: radio.ptt, 2)
            link.send_pcm(b"\x10\x00" * 12000 * 5)
            limit = 0.8 + 1.0                                      # max_key_s + HARD_MARGIN_S
            assert until(lambda: not radio.ptt, limit + 1.5), "still keyed"
            assert time.monotonic() - t0 <= limit + 0.3 + 0.5     # + EMERGENCY_LOOP_S + slack
            assert link.stats["hard-watchdog"] >= 1
            hung.set()
    finally:
        hung.set()
        radio.close()


# -- a slow demodulator never holds up the radio (18/10/2026, on air) ---------------
#
# With the app's engine running, an IC-9700 over the LAN timed out
# rig.set_ptt twice and the minimum-power read (14 0A) at the first key,
# and went 15 s without its polls answered; bare scripts never did. The
# rig's loop was running the engine's demodulator, under the engine's lock,
# while the engine held that lock and waited on the loop to key.

#: The longest any call into the rig may take with receive work pouring in:
#: a key or an unkey is a CI-V round trip on a loopback, a few milliseconds.
PROMPT_S = 0.1


def slow_demodulator(eng, factor: float = 3.0) -> None:
    """Demodulating each block of received audio takes `factor` times as
    long as the audio lasts: slower than real time, as a busy laptop's
    might be."""
    real = eng.receiver.feed

    def feed(pcm):
        time.sleep(len(pcm) / 2 / eng.receiver.rate * factor)
        return real(pcm)

    eng.receiver.feed = feed


class CallTimes:
    """Every call the link makes into the rig, timed, and any that failed."""

    def __init__(self, link):
        self.calls: list[tuple[str, float]] = []
        self.failed: list[tuple[str, str]] = []
        real = link._call

        def timed(fn, *args, **kw):
            name = getattr(fn, "__name__", repr(fn))
            t = time.monotonic()
            try:
                return real(fn, *args, **kw)
            except BaseException as e:
                self.failed.append((name, repr(e)))
                raise
            finally:
                self.calls.append((name, time.monotonic() - t))

        link._call = timed

    def slow(self) -> list[tuple[str, float]]:
        return [(n, round(d, 3)) for n, d in self.calls if d > PROMPT_S]


def overs(eng, link, n: int, timeout: float) -> bool:
    """`n` transmissions, one frame each, one after another: each queued once
    the last has gone and the radio is unkeyed again."""
    for i in range(n):
        eng.send_frame(ui(f"GM4BRI>APRS:>over {i}"))
        if not until(lambda: eng.channel.stats.transmissions == i + 1 and not link.keyed, timeout):
            return False
    return True


def test_a_slow_demodulator_never_delays_a_key_over_the_lan(fake):
    radio = fake.radio
    radio.rf_power_level = 120
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000, minimum_power_for_test=True)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    eng.channel.config.persist = 255
    slow_demodulator(eng)
    link.RX_QUEUE_S = 0.5                           # behind within the test, not after 2 s of it
    with link:
        times = CallTimes(link)
        runner = Runner(eng).start()
        try:
            time.sleep(0.5)                         # the loop full of receive work first
            assert overs(eng, link, 2, 10)
            assert until(lambda: link._confirm is None, 2)
        finally:
            runner.stop()
        assert times.failed == [], times.failed
        assert times.slow() == [], times.slow()
        assert {"set_ptt", "ask"} <= {n for n, _d in times.calls}, "the key, the unkey and the 14 0A read were timed"
        assert eng.channel.stats.refused == {}
        assert link.stats["rx_dropped"] > 0, "the demodulator really was behind"
    assert radio.ptt_commands[:4] == [True, False, True, False] and not radio.ptt


def test_keys_while_audio_pours_in_stay_prompt(fake):
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_800_000)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    eng.channel.config.persist = 255
    slow_demodulator(eng, 1.5)
    # And the app's pages reading the engine under its lock, as they do.
    reading = [True]

    def page():
        while reading[0]:
            with eng.lock:
                time.sleep(0.002)
            time.sleep(0.01)

    reader = threading.Thread(target=page, daemon=True)
    with link:
        times = CallTimes(link)
        runner = Runner(eng).start()
        reader.start()
        try:
            assert overs(eng, link, 8, 10), eng.channel.stats
            assert until(lambda: link._confirm is None, 2)
        finally:
            reading[0] = False
            runner.stop()
            reader.join(2)
        assert times.failed == [], times.failed
        assert times.slow() == [], times.slow()
        assert eng.channel.stats.refused == {}
        assert link.stats["keyed"] == 8
    assert radio.ptt_commands.count(True) == 8 and not radio.ptt
    assert radio.tx_pcm_unkeyed == 0


FAKE_ARECORD = """#!{python}
import os, sys, time
if "--dump-hw-params" in sys.argv:
    sys.stderr.write("CHANNELS: 1\\nRATE: [8000 48000]\\n"); sys.exit(0)
out = sys.stdout.buffer
start, k = time.monotonic(), 0
while True:
    out.write(os.urandom(48000 * 2 // 50)); out.flush()
    k += 1
    time.sleep(max(0.0, start + k / 50 - time.monotonic()))
"""


def test_a_slow_demodulator_never_delays_a_key_over_usb(tmp_path, monkeypatch):
    # The same over the cable: the codec's audio comes off arecord's pipe
    # on the rig's loop, as the LAN stream does.
    from shack_rig.icom import usbprobe
    monkeypatch.setattr(usbprobe, "BY_ID", str(tmp_path / "none"))
    aplay = tmp_path / "aplay"
    aplay.write_text(f"""#!{sys.executable}
import sys
if "--dump-hw-params" in sys.argv:
    sys.stderr.write("CHANNELS: 1\\nRATE: [8000 48000]\\n"); sys.exit(0)
sys.stdin.buffer.read()
""")
    arecord = tmp_path / "arecord"
    arecord.write_text(FAKE_ARECORD.format(python=sys.executable))
    for exe in (aplay, arecord):
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    from shack_rig.icom import usb as icomusb
    real = icomusb.UsbLink.__init__

    def with_fake_aplay(self, *a, **kw):
        kw["aplay"] = str(aplay)
        real(self, *a, **kw)

    monkeypatch.setattr(icomusb.UsbLink, "__init__", with_fake_aplay)
    radio = PtyRadio(answer_all=True)
    profile = {"model": "ic-705", "transport": "usb", "civ_device": radio.path, "civ_baud": 115200,
               "audio_rate": 0, "audio_device": "hw:9,0", "tx_audio": "alsa"}
    link = IcomLink(profile, frequency=144_800_000)
    eng = Engine(Station.of("GM4BRI", tx_enabled=True), link)
    eng.channel.config.persist = 255
    slow_demodulator(eng)
    link.RX_QUEUE_S = 0.5                           # behind within the test, not after 2 s of it
    try:
        with link:
            assert until(lambda: link.rig._link.audio is not None and link.rig._link.audio.blocks > 10, 5)
            times = CallTimes(link)
            runner = Runner(eng).start()
            try:
                time.sleep(0.3)
                assert overs(eng, link, 2, 10)
                assert until(lambda: link._confirm is None, 2)
            finally:
                runner.stop()
            assert times.failed == [], times.failed
            assert times.slow() == [], times.slow()
            assert eng.channel.stats.refused == {}
            assert link.stats["rx_dropped"] > 0, "the demodulator really was behind"
    finally:
        radio.close()
    assert [k for _t, k in radio.keys][:4] == [True, False, True, False]


def test_a_channel_the_app_tunes_to_is_the_apps_and_goes_back_at_close(fake, tmp_path):
    """The scope's click (Packet Radio's rigfeatures.retune) is the app's
    change, not the operator's: confirmed at once, put back at close."""
    radio = fake.radio
    link = IcomLink(fake.profile(), password="secret", frequency=144_925_000, setup_dir=tmp_path)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert link.tune_channel(144_800_000)
        assert radio.freq == 144_800_000 and link.frequency == 144_800_000
        assert link._channel_refusal() is None
    assert {ln.key: ln.status for ln in link.setup_restored}["frequency"] == "restored"
    assert radio.freq == 144_174_000
