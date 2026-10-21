"""CI-V over a serial port, exercised against a radio made of a pty.

Ported from OpenHamShack Icom's tools/selftest_usb.py. Everything here runs
real code against real file descriptors (see usbbench.py for the pty radio and
the by-id directory it is found in).

What that covers, and it is more than it sounds: opening, exclusivity, the
address sweep, the baud ordering, the transcript, reconnection after the far
end closes, and a frame arriving in two pieces. What it cannot cover is
anything electrical - a pty has no line speed, so the baud is set and ignored,
and nothing here proves which of an Icom's two ports carries CI-V. Those were
checked against the IC-7610 on `shack` by hand, and the numbers that came back
are quoted in the Icom app's `docs/TRANSPORTS.md`.

**No radio is touched**, and nothing here keys one: the one check that sends
`T 1` sends it without a lease and asserts it is refused.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from helpers import Wire, until
from shack_rig.icom import civ, usbprobe
from shack_rig.icom.rig import IcomRig
from shack_rig.icom.usb import UsbLink
from shack_rig.rigctl import RigctlServer
from shack_rig.serialio import PortBusy, SerialPort
from usbbench import SEVEN_A, SEVEN_ADDR, SEVEN_B, Bench, FakeRadio


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Make a Bench of the given by-id names; each call gets its own directory."""
    made = []

    def make(*names):
        b = Bench(list(names), tmp_path / f"byid{len(made)}", monkeypatch)
        made.append(b)
        return b

    yield make
    for b in made:
        b.__exit__(None, None, None)


@pytest.fixture
def radios():
    """FakeRadios made in a test are stopped however the test ends."""
    made: list[FakeRadio] = []

    def make(cls=FakeRadio, *a, **kw):
        r = cls(*a, **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def _closing(port):
    if port is not None:
        port.close()


# -- the port itself --------------------------------------------------------


def test_the_port_is_held_exclusively(bench):
    """One radio, one opener. Nothing in a tty enforces this on its own.

    The radio accepts one client, so without something in the middle, running
    WSJT-X means not running anything else. On a serial port that premise is
    still true and nothing enforces it - two processes may open one tty and
    each gets a random half of the bytes, which is worse than a refusal
    because neither one fails.
    """
    b = bench(SEVEN_A)
    first = SerialPort(b.paths[SEVEN_A], 38400)
    first.open()
    second = SerialPort(b.paths[SEVEN_A], 38400)
    try:
        with pytest.raises(PortBusy) as refused:
            second.open()
    finally:
        second.close()
    assert "open" in str(refused.value) or "held" in str(refused.value), \
        "and is told that is what happened, not that the radio is quiet: " \
        + str(refused.value)
    first.close()
    # And the port is free again once the first one closes.
    third = SerialPort(b.paths[SEVEN_A], 38400)
    third.open()
    third.close()


def test_bytes_go_both_ways(bench, radios):
    """Send a frame, get an answer, at the level of the file descriptor."""
    b = bench(SEVEN_A)
    radios(FakeRadio, b.ports[SEVEN_A])
    port = SerialPort(b.paths[SEVEN_A], 38400)
    port.open()
    try:
        got = port.exchange(civ.frame(civ.read_freq(), to=SEVEN_ADDR), 0.5)
    finally:
        port.close()
    frames, _ = civ.split_frames(got)
    assert len(frames) == 1, f"a frequency read is answered: {got.hex(' ')}"
    to, frm, payload = civ.parse(frames[0])
    assert frm == SEVEN_ADDR, f"by the radio's own address: {hex(frm)}"
    assert civ.decode_freq(payload[1:6]) == 7_164_437, \
        f"with the frequency it is on: {payload.hex(' ')}"


# -- the probe --------------------------------------------------------------


def test_the_probe_finds_a_radio(bench, radios):
    """Port, speed and address together, which is why it is one loop.

    The three were all wrong at once on the real radio: it was not on the
    documented default speed, there was no way to tell its two ports apart from
    the host, and a wrong address is silence rather than an error. A probe that
    found one of the three and assumed the others would have failed on the
    first radio it was pointed at.
    """
    b = bench(SEVEN_A, SEVEN_B)
    radios(FakeRadio, b.ports[SEVEN_A])
    # The keying port: open, real, and silent at every speed. This is what
    # the second CP2102 on the real radio did.
    radios(FakeRadio, b.ports[SEVEN_B], answer=False)
    report, port = usbprobe.probe(deadline=15.0)
    try:
        got = report.chosen
        assert got is not None, f"a radio is found: {report.public()}"
        assert got.node.name == SEVEN_A, \
            f"on the port that answers, not the one that does not: {got.node.name}"
        assert got.address == SEVEN_ADDR, \
            f"at the address it actually answered from: {got.address}"
        assert got.model == "ic-7610", \
            f"and the model is read out of the port's own name: {got.model}"
        assert "7.164437" in got.summary, \
            f"the summary is a sentence, with the evidence in it: {got.summary}"
        assert port is not None and port.fd is not None, \
            "the port it found is handed back open"
    finally:
        _closing(port)


def test_the_probe_says_what_it_saw(bench, radios):
    """The transcript, which is the point of the whole probe.

    An appliance may be three hundred miles away with nobody on site. "No radio
    found" cannot be acted on from there; "this port exists, it is called this,
    these six speeds were tried and nothing came back" can.
    """
    b = bench(SEVEN_A)
    radios(FakeRadio, b.ports[SEVEN_A], answer=False)
    report, port = usbprobe.probe(deadline=25.0)
    _closing(port)
    assert report.chosen is None, "nothing is found"
    assert len(report.findings) == 1, \
        f"but the port is still reported: {report.public()}"
    f = report.findings[0]
    assert f.outcome == "silent", \
        f"said to be silent rather than missing: {f.outcome}"
    assert f.node.name == SEVEN_A, \
        f"with the by-id name, so somebody can see what is plugged in: {f.node.name}"
    assert len(f.exchanges) > 4, \
        f"and every question that was asked, with what came back: {len(f.exchanges)}"
    assert len({e.baud for e in f.exchanges}) > 1, \
        f"each one saying which speed it was asked at: {sorted({e.baud for e in f.exchanges})}"
    assert all(o in usbprobe.OUTCOMES
               for o in ("radio", "silent", "busy", "foreign", "skipped")), \
        "and every outcome the payload can carry is explained in words"


def test_a_port_in_use_is_left_alone(bench, radios):
    """Never open a port a radio in this process already has.

    The same fault as connecting to the IC-9700 twice, and worse: nothing
    refuses the second opener, so both get half the bytes and neither is told.
    It is reported as skipped rather than omitted, because a port missing from
    the list reads as a port that is not plugged in.
    """
    b = bench(SEVEN_A)
    radio = radios(FakeRadio, b.ports[SEVEN_A])
    report, port = usbprobe.probe(skip=(b.paths[SEVEN_A],), deadline=5.0)
    _closing(port)
    assert report.chosen is None, "nothing is opened"
    outcomes = [f.outcome for f in report.findings]
    assert outcomes == ["skipped"], f"and it is listed as skipped, not as absent: {outcomes}"
    assert radio.heard == [], \
        f"the radio holding it never heard a thing: {[f.hex(' ') for f in radio.heard]}"


def test_a_reply_that_is_not_an_icom_is_described_in_words():
    """A partial match is the interesting case, and must not be discarded.

    Two of the three radios that might be plugged into an appliance are Yaesu.
    This library cannot drive one yet, and saying "this looks like Yaesu CAT"
    in words is worth more than reporting it as nothing at all - it is the one
    sentence that turns a wasted day into a five-minute answer.
    """
    yaesu = usbprobe.describe_reply(b"ID0670;")
    assert "Yaesu" in yaesu, f"Yaesu CAT is named in words: {yaesu}"
    wrong_speed = usbprobe.describe_reply(bytes([0xFE, 0x8C, 0x13, 0x77]))
    assert "speed" in wrong_speed, \
        f"and the wrong speed is described as the wrong speed: {wrong_speed}"
    assert usbprobe.describe_reply(b"") == "nothing at all", \
        "silence is not described as either"


def test_a_yaesu_on_the_port_is_reported_as_foreign(bench, radios):
    class Yaesu(FakeRadio):
        def _reply(self, f: bytes) -> None:
            self.heard.append(f)
            self._write(b"ID0670;")

    b = bench(SEVEN_A)
    radios(Yaesu, b.ports[SEVEN_A])
    report, port = usbprobe.probe(deadline=25.0)
    _closing(port)
    assert report.chosen is None, \
        "it is not mistaken for a radio this library can drive"
    f = report.findings[0]
    assert f.outcome == "foreign", f"it is reported as foreign, not as silence: {f.outcome}"
    assert "Yaesu" in f.summary, f"and the summary names Yaesu CAT: {f.summary}"
    assert any("49 44" in e.got.hex(" ") for e in f.exchanges), \
        "with the bytes in the transcript for somebody to read: " \
        f"{[e.got.hex(' ') for e in f.exchanges][:2]}"


def test_the_probe_is_bounded(bench, radios):
    """It gives up and says so, rather than leaving an appliance in the dark."""
    b = bench(SEVEN_A, SEVEN_B)
    radios(FakeRadio, b.ports[SEVEN_A], answer=False)
    radios(FakeRadio, b.ports[SEVEN_B], answer=False)
    started = time.monotonic()
    report, port = usbprobe.probe(deadline=2.0)
    took = time.monotonic() - started
    _closing(port)
    assert took < 6.0, f"it stops near the deadline: {took:.1f}s"
    assert report.hit_deadline is True, "and says that is why it stopped"
    assert any(f.outcome == "gave-up" for f in report.findings), \
        f"naming the ports it never reached: {[f.outcome for f in report.findings]}"


def test_what_was_found_is_remembered(bench, radios, tmp_path):
    """A Pi switched off at the wall must not rediscover the radio every morning.

    Keyed by the by-id string, which carries the radio's own serial number - so
    a different radio in the same socket is a different key and gets a fresh
    search. That is the property that makes remembering safe; the cache is
    never believed without being confirmed on the wire.
    """
    b = bench(SEVEN_A)
    radio = radios(FakeRadio, b.ports[SEVEN_A])
    path = tmp_path / "usb-probe.json"
    first, port = usbprobe.probe(cache=usbprobe.Cache(path), deadline=15.0)
    _closing(port)
    assert path.exists(), "what worked is written down"
    asked_first = len(radio.heard)
    radio.heard.clear()

    second, port = usbprobe.probe(cache=usbprobe.Cache(path), deadline=15.0)
    _closing(port)
    assert second.chosen is not None, "the radio is found again"
    assert len(radio.heard) <= asked_first, \
        f"with no more questions than the first time: {len(radio.heard)} then, {asked_first} before"
    assert second.seconds < 1.0, f"and quickly: {second.seconds:.2f}s"

    # What was measured beats what was typed. A pty has no line speed, so
    # the only way to see the cache working is to ask for the wrong one and
    # watch the remembered one be tried first anyway - which is exactly the
    # case it exists for: a profile whose baud was right last year.
    third, port = usbprobe.probe(cache=usbprobe.Cache(path),
                                 prefer_baud=4800, deadline=15.0)
    _closing(port)
    first_try = third.findings[0].exchanges[0].baud
    assert first_try == first.chosen.baud, \
        "and what was measured is tried before what was typed: " \
        f"tried {first_try} first, remembered {first.chosen.baud}"


# -- the link, and an IcomRig on top of it ----------------------------------


def _rig_on(path: str, link: UsbLink) -> IcomRig:
    return IcomRig(make_link=lambda: link, where=path, model=None,
                   radio_addr=SEVEN_ADDR, poll_interval=0.1, scope=False)


def test_a_rig_on_a_serial_port(bench, radios):
    """The whole thing: an IcomRig, a UsbLink, a pty and a fake 7610.

    Through `IcomRig.start()` and the state cache rather than by calling the
    link directly, because what is being checked is that the poll loop, the
    supervisor and the reply handling all work over a transport none of them
    was written for.
    """
    b = bench(SEVEN_A)
    fake = radios(FakeRadio, b.ports[SEVEN_A])
    path = b.paths[SEVEN_A]

    async def run():
        link = UsbLink(device=path, baud=38400, address=SEVEN_ADDR,
                       model_key="ic-7610")
        rig = _rig_on(path, link)
        await rig.start()
        try:
            await until(lambda: rig.state["freq"], 6.0, 0.1)
            assert rig.state["connected"] is True, \
                f"it comes up connected: {rig.state['error']}"
            assert rig.state["freq"] == 7_164_437, \
                f"with the frequency the radio is on: {rig.state['freq']}"
            assert rig.state["mode"] == "USB", f"and the mode: {rig.state['mode']}"
            assert rig.where() == path, \
                f"where() names the port rather than an address: {rig.where()}"

            # A command from a person, and the answer that it went.
            assert rig.set_freq(14_074_000) is True, \
                "setting a frequency reports that it was sent"
            await asyncio.sleep(0.3)
            assert any(f[4:5] == b"\x05" for f in fake.heard), \
                f"and the radio heard it: {[f.hex(' ') for f in fake.heard[-3:]]}"
        finally:
            await rig.stop()
        assert link._port is None, "stopping releases the port"

    asyncio.run(run())
    # And the proof that it was released: something else can open it again.
    again = SerialPort(path, 38400)
    again.open()
    again.close()


def test_rigctl_drives_a_rig_on_a_serial_port_without_knowing_it(bench, radios):
    """The Rig boundary, as behaviour.

    Replaces the app selftest's `test_nothing_above_the_radio_speaks_civ`,
    which grepped the app's source for `radio.<attr>` outside the Rig surface
    and for transport imports in rigctl.py. That was a fact about the app's
    files, not about this library; what it protected - something above the
    rig driving a radio on a serial port without being told such a thing
    exists - can be executed here, so it is: hamlib's protocol over a real TCP
    socket, to an IcomRig on a UsbLink on a pty.

    Transmit stays shut: the server has no lease callable, so `T 1` must be
    refused and the keying frame must never reach the wire.
    """
    b = bench(SEVEN_A)
    fake = radios(FakeRadio, b.ports[SEVEN_A])
    path = b.paths[SEVEN_A]

    async def ask(reader, writer, line):
        writer.write((line + "\n").encode())
        await writer.drain()
        return (await asyncio.wait_for(reader.readline(), 2)).decode().strip()

    async def run():
        link = UsbLink(device=path, baud=38400, address=SEVEN_ADDR,
                       model_key="ic-7610")
        rig = _rig_on(path, link)
        rig.allow_ptt = True     # so a refusal can only be the missing lease
        await rig.start()
        server = RigctlServer(rig, "127.0.0.1", 0, model_key="ic-7610")
        await server.start()
        try:
            assert await until(lambda: rig.state["freq"], 6.0, 0.1), rig.state
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            assert await ask(reader, writer, "f") == "7164437"
            assert await ask(reader, writer, "m") == "USB"
            await reader.readline()                      # the passband line
            assert await ask(reader, writer, "F 14074000") == "RPRT 0"
            assert await until(lambda: any(f[4:5] == b"\x05" for f in fake.heard), 2)
            assert await ask(reader, writer, "T 1") == "RPRT -11", \
                "no lease callable, so the server is receive-only"
            await asyncio.sleep(0.2)
            keyed = [f for f in fake.heard if f[4:7] == b"\x1c\x00\x01"]
            assert keyed == [], f"and nothing keyed the radio: {keyed}"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()
            await rig.stop()

    asyncio.run(run())


def test_one_question_at_a_time(bench, radios):
    """The finding that made the difference, reproduced.

    Measured on the IC-7610 on `shack`: one CI-V command is answered in 5.2 to
    6.1 milliseconds, every time - and four commands written back to back, which
    is exactly what `IcomRig._poll` does four times a second, get **one**
    answer, to the last of them. The first symptom was a page with a live
    S-meter and no frequency and no mode at all, on a link that was plainly up:
    the meter was the last command in the burst.

    Without the pacing in `UsbLink` this test fails, which is the point of it:
    the pty is otherwise infinitely patient and would let a transport that
    cannot drive the real radio pass.
    """

    class Deaf(FakeRadio):
        """An IC-7610: it answers the last thing it heard and loses the rest.

        The mechanism behind the measurement rather than the measurement
        itself. CI-V is a one-wire bus and the radio is not listening while it
        is talking, so everything that arrived in the same burst is gone - it
        is never queued and never answered late.

        Modelled this way rather than as a five-millisecond rule because a rule
        about timing would be satisfied by a transport that merely dawdled. The
        thing that has to be true is that only one question is outstanding.
        """

        def _handle(self, frames: list[bytes]) -> None:
            self.heard.extend(frames[:-1])    # heard, and dropped on the floor
            if frames:
                time.sleep(0.006)             # the measured turnaround
                self._reply(frames[-1])

    b = bench(SEVEN_A)
    radios(Deaf, b.ports[SEVEN_A])

    async def run():
        link = UsbLink(device=b.paths[SEVEN_A], baud=38400,
                       address=SEVEN_ADDR, model_key="ic-7610")
        answers: list[bytes] = []
        link.on_civ = answers.append
        await link.start()
        try:
            answers.clear()
            # The poll loop's burst, exactly as rig.py writes it.
            for cmd in (civ.read_freq(), civ.read_mode_data(), civ.read_ptt(),
                        civ.read_smeter()):
                link.send_civ(civ.frame(cmd, to=SEVEN_ADDR))
            await asyncio.sleep(1.0)
        finally:
            await link.stop()
        frames, _ = civ.split_frames(b"".join(answers))
        got = {civ.parse(f)[2][0] for f in frames}
        assert {0x03, 0x26, 0x1C, 0x15} <= got, \
            "all four questions in a burst are answered, not just the last: " \
            f"{sorted(hex(c) for c in got)}"

    asyncio.run(run())


def _unstarted_rig() -> IcomRig:
    """An IcomRig fed by hand: the link is a list and the poll never runs."""
    link = Wire()
    rig = IcomRig(make_link=lambda: link, where="nowhere",
                  radio_addr=SEVEN_ADDR, poll_interval=60.0, scope=False)
    rig.state["connected"] = True
    rig._link = link
    link.on_civ = rig._on_civ_data
    return rig


WHOLE = civ.frame(bytes([0x03]) + civ.encode_freq(14_074_000),
                  to=civ.DEFAULT_CTRL_ADDR, frm=SEVEN_ADDR)


@pytest.mark.parametrize("at", range(1, len(WHOLE)))
def test_a_frame_split_across_two_reads(at):
    """The bug a serial port has and a datagram does not.

    On the LAN a CI-V frame arrives whole inside one datagram, near enough
    always. On a wire it arrives in whatever pieces the kernel felt like, and
    the rig's buffer with `civ.split_frames` is what copes. So it is exercised
    at every byte offset, because the interesting splits are the ones inside
    the preamble and inside the terminator.
    """
    rig = _unstarted_rig()
    rig._link.on_civ(WHOLE[:at])
    rig._link.on_civ(WHOLE[at:])
    assert rig.state["freq"] == 14_074_000, f"split at byte {at}: {rig.state['freq']}"


def test_two_frames_in_one_read_are_both_read():
    # The other half of the same buffer, and what a poll loop's burst produces.
    rig = _unstarted_rig()
    rig._link.on_civ(WHOLE + civ.frame(bytes([0x1C, 0x00, 0x01]),
                                       to=civ.DEFAULT_CTRL_ADDR, frm=SEVEN_ADDR))
    assert rig.state["freq"] == 14_074_000 and rig.state["ptt"] is True, rig.state


def test_our_own_echo_is_not_an_answer(bench, radios):
    """CI-V USB Echo Back puts our own frames back on the wire.

    They are addressed *to* the radio and *from* 0xE0, so the sender is what
    tells an echo from an answer. A probe that checked only the framing would
    report every port as a radio, at every speed, including the keying port.
    """
    b = bench(SEVEN_A)
    radios(FakeRadio, b.ports[SEVEN_A], answer=False, echo=True)
    report, port = usbprobe.probe(deadline=25.0)
    _closing(port)
    assert report.chosen is None, \
        f"an echo is not mistaken for an answer: {report.chosen and report.chosen.summary}"
    assert report.findings[0].outcome == "foreign", \
        f"and it is reported as something rather than as silence: {report.findings[0].outcome}"


def test_a_radio_that_echoes_and_answers_is_still_found(bench, radios):
    b = bench(SEVEN_A)
    radios(FakeRadio, b.ports[SEVEN_A], echo=True)
    report, port = usbprobe.probe(deadline=15.0)
    _closing(port)
    assert report.chosen is not None, "a radio that both echoes and answers is still found"
    assert report.chosen.address == SEVEN_ADDR, \
        f"at its own address, not at ours: {report.chosen.address}"


def test_the_scope_is_refused_on_a_slow_link():
    """A waterfall that cannot fit takes the whole link with it.

    475 levels in eleven frames, ten times a second, is about 6.6 kB/s. A line
    at 38400 carries 3.8 kB/s and at 19200 half of that, so asking for a
    spectrum does not produce a slow one: it produces a link with no room for
    the poll loop and a frequency that stops following the dial. Refused with a
    sentence, because "the waterfall is slow" is otherwise diagnosed as a bug
    for a week.
    """
    slow = UsbLink(device="/dev/null", baud=38400)
    fast = UsbLink(device="/dev/null", baud=115200)
    assert slow.allows_scope is False, "refused at 38400"
    assert fast.allows_scope is True, "allowed at 115200"
    assert "115200" in (slow.scope_refusal() or ""), \
        f"and the refusal says what to do about it: {slow.scope_refusal()}"
    assert fast.scope_refusal() is None, "with nothing to say when it is allowed"


# -- a port named outside by-id, read from a thread, and transmit audio -----


class _Lease:
    holder = "test"

    def refusal(self):
        return None


def test_a_port_named_outside_by_id_read_from_a_thread(tmp_path, monkeypatch, radios):
    """A Mac's /dev/cu.* or a Windows COM port: no by-id directory, so the
    named port is examined as it is; and read by a thread rather than the
    event loop, which is the road a Windows COM port takes."""
    monkeypatch.setattr(usbprobe, "BY_ID", str(tmp_path / "empty"))
    master, slave = os.openpty()
    path = os.ttyname(slave)
    radios(FakeRadio, master)

    async def run():
        link = UsbLink(device=path, baud=38400, address=SEVEN_ADDR, model_key="ic-7610",
                       audio=False, reader="thread")
        rig = _rig_on(path, link)
        await rig.start()
        try:
            assert await until(lambda: rig.state["freq"] == 7_164_437, 6.0, 0.1), rig.state
            assert link._reader_thread is not None and link._reader_thread.is_alive()
        finally:
            await rig.stop()
        assert link._reader_thread is None and link._port is None

    try:
        asyncio.run(run())
    finally:
        os.close(master)
        os.close(slave)


def test_transmit_audio_over_usb_goes_to_aplay_only_while_keyed(tmp_path, monkeypatch, radios):
    import array
    import stat
    import sys
    script = tmp_path / "aplay"
    script.write_text(f"""#!{sys.executable}
import sys
if "--dump-hw-params" in sys.argv:
    sys.stderr.write("CHANNELS: 1\\nRATE: [8000 48000]\\n"); sys.exit(0)
with open({str(tmp_path / 'played')!r}, "ab") as f:
    f.write(sys.stdin.buffer.read())
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(usbprobe, "BY_ID", str(tmp_path / "empty"))
    master, slave = os.openpty()
    path = os.ttyname(slave)
    radios(FakeRadio, master)
    tone = array.array("h", [((i * 41) % 3000) - 1500 for i in range(1200)]).tobytes()

    async def run():
        link = UsbLink(device=path, baud=38400, address=SEVEN_ADDR, model_key="ic-7610",
                       audio=False, tx_audio="alsa", audio_device="hw:9,0", aplay=str(script))
        rig = _rig_on(path, link)
        rig.allow_ptt = True
        await rig.start()
        try:
            assert await until(lambda: rig.state["freq"], 6.0, 0.1)
            assert rig.tx_rate == 12000 and rig.tx_audio_ready
            assert rig.send_audio(tone[:480]) is False, "not keyed: nothing reaches the codec"
            assert rig.set_ptt(True, _Lease())
            for at in range(0, len(tone), 480):
                assert rig.send_audio(tone[at:at + 480])
            rig.tx_end()
            drain = rig.tx_drain_s
            assert 0.0 < drain < 1.5
            await asyncio.sleep(0.2)
            rig.set_ptt(False)
        finally:
            await rig.stop()

    try:
        asyncio.run(run())
    finally:
        os.close(master)
        os.close(slave)
    played = array.array("h")
    played.frombytes((tmp_path / "played").read_bytes())
    want = array.array("h")
    want.frombytes(tone)
    assert len(played) == 4 * len(want)
    assert list(played[3::4]) == list(want)


# -- the unkey goes first, and nothing queued is lost at close --------------
#
# 18/10/2026 on the real IC-9700: an unkey queued behind poll reads reached
# a USB radio late, and DATA MOD was left on USB after a USB session whose
# close had "sent" the restore - queued, then thrown away with the queue
# when the port closed. Each test below fails on the writer as it was.


def _frames_heard(radio) -> list[bytes]:
    return [civ.parse(f)[2] for f in radio.heard]


class Mute(FakeRadio):
    """Answers the probe's ID question and nothing else, so every frame the
    link writes costs it the whole reply timeout."""

    def _reply(self, f: bytes) -> None:
        try:
            payload = civ.parse(f)[2]
        except civ.CivError:
            return
        if payload[:1] == b"\x19":
            super()._reply(f)
        else:
            self.heard.append(f)


def test_an_unkey_jumps_a_full_queue_of_reads(bench, radios):
    """Thirty-two reads to a radio that answers none of them take 1.6 s to
    drain at the reply timeout; the unkey queued after them must not wait."""
    b = bench(SEVEN_A)
    radio = radios(Mute, b.ports[SEVEN_A])

    async def run():
        link = UsbLink(device=b.paths[SEVEN_A], baud=38400, address=SEVEN_ADDR, model_key="ic-7610")
        await link.start()
        try:
            read = civ.frame(civ.read_freq(), to=SEVEN_ADDR)
            accepted = [link.send_civ(read) for _ in range(40)]
            assert accepted.count(True) <= 33, "the outbox is bounded as before"
            assert link.send_civ(civ.frame(civ.set_ptt(False), to=SEVEN_ADDR)), \
                "a full outbox does not refuse the unkey"
            assert await until_async(lambda: civ.set_ptt(False) in _frames_heard(radio), 0.3), \
                "the unkey reached the radio within 0.3 s, ahead of the reads"
            heard = [f for f in _frames_heard(radio) if f[:1] != b"\x19"]     # the probe's own
            assert heard.index(civ.set_ptt(False)) <= 1, f"at most one frame went before it: {heard[:4]}"
        finally:
            await link.stop()

    asyncio.run(run())


def test_frames_queued_before_stop_are_written(bench, radios):
    """What a caller sends just before closing - the restores - is written
    before the port is let go."""
    b = bench(SEVEN_A)
    radio = radios(Mute, b.ports[SEVEN_A])
    restore = civ.set_data_mod(116, 0x05)

    async def run():
        link = UsbLink(device=b.paths[SEVEN_A], baud=38400, address=SEVEN_ADDR, model_key="ic-7610")
        await link.start()
        for _ in range(5):
            link.send_civ(civ.frame(civ.read_freq(), to=SEVEN_ADDR))
        link.send_civ(civ.frame(restore, to=SEVEN_ADDR))
        await link.stop()

    asyncio.run(run())
    deadline = time.monotonic() + 1.0
    while restore not in _frames_heard(radio) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert restore in _frames_heard(radio), "the restore queued last went before the close"


def test_a_refused_unkey_write_is_tried_again_and_counted(bench, radios):
    b = bench(SEVEN_A)
    radio = radios(FakeRadio, b.ports[SEVEN_A])

    async def run():
        link = UsbLink(device=b.paths[SEVEN_A], baud=38400, address=SEVEN_ADDR, model_key="ic-7610")
        await link.start()
        try:
            port = link._port
            real = port.write
            refusals = [False, False]

            def flaky(data):
                return refusals.pop(0) if refusals else real(data)

            port.write = flaky
            link.send_civ(civ.frame(civ.set_ptt(False), to=SEVEN_ADDR))
            assert await until_async(lambda: civ.set_ptt(False) in _frames_heard(radio), 1.0), \
                "the third try reached the radio"
            assert link.write_failures == 2 and link.unkeys_lost == 0

            port.write = lambda data: False
            link.send_civ(civ.frame(civ.set_ptt(False), to=SEVEN_ADDR))
            assert await until_async(lambda: link.unkeys_lost == 1, 1.0), "an unkey that never went is counted"
            assert link.diagnostics()["unkeys_lost"] == 1
            port.write = real
        finally:
            await link.stop()

    asyncio.run(run())


async def until_async(cond, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return bool(cond())
