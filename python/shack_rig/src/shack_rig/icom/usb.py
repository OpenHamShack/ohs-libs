"""CI-V on a serial port: the whole of the USB transport.

The sibling of `link.py`'s `LanLink`, and much the simpler of the two. There is
no handshake, no token to renew, no retransmit buffer and no sequence numbers -
a serial port either carries the bytes or it does not. What it has instead is
three things that have to be *found* rather than configured, which is where all
the difficulty went: which of the radio's ports carries CI-V, at what speed, and
to which CI-V address. `usbprobe.py` is that, and this file is what happens
afterwards.

**Reading is event-driven, not polled.** The port's file descriptor is handed
to the event loop with `add_reader`, so bytes arriving wake the loop exactly as
a datagram does on the LAN path. A read loop that spun would cost a Raspberry Pi
400 a decode period of a four-core 1.8 GHz budget it does not have, and this
module is expected to run for a month on one.

`docs/TRANSPORTS.md` §7.1 proposed `loop.connect_read_pipe`. This uses
`add_reader` instead, for one practical reason: the same descriptor is written
to as well as read from, and `connect_read_pipe` takes ownership of it and
closes it when its transport closes. Two descriptors for one port would mean two
`flock`s and two chances to leave one held. `add_reader` is the same epoll
underneath with none of that.

**Stage two is receive audio, and it is in `usbaudio.py`.** A radio on a cable
can now be heard as well as controlled, which is what makes the appliance a
station rather than a remote control: without it there is no decoding and no
waterfall. It is `arecord` as a subprocess, as stage one predicted, but **not**
under `plughw` - that turned out to be the one thing that must not be done,
because ALSA's plug layer resampling 48 kHz down to 12 put every decoded
frequency 2.1% out while looking entirely healthy. The card is asked what it
can do, captured at that, and divided down here where the arithmetic is exact.
The argument is in `usbaudio.py`'s header and the measurements are in
`docs/OFF-AIR.md`.

Two things follow from that file which this one has to carry. The capture is
**two channels on an IC-7610**, because the radio puts Main on one and Sub on
the other and both decode independently, so audio arrives here named by the
receiver it came from rather than as "the audio", and which of them anything
that did not choose gets is decided in `rig.py`, where a radio is one thing
again. And what leaves here is 12 kHz, so `DecodeRunner` needs no change at
all: it reads `audio_rate`, finds 12000, and does not resample.

**Stage three, transmit audio, is here as an option (18/10/2026)**, built for
the packet radio links and off unless asked for, so the Icom app's USB radios
are unchanged: `tx_audio="alsa"` is TRANSPORTS.md's `aplay`, on the card the
receive side found; `tx_audio="portaudio"` is a named device through
`shack_rig.sound`, for a desktop. Either way 12 kHz goes in and the codec's
own rate comes out (`txaudio.py` says why, and how). `tx_rate` stays 0 with
neither, so the transmit panel stays off a radio nobody set up to transmit.

**And for a desktop: receive audio through PortAudio, and a serial port
read from a thread.** `audio_backend="portaudio"` captures a named device at
its native rate and divides it by the same exact `Split` as the `arecord`
path. Where the event loop cannot watch the port - a Windows COM port, or a
loop without `add_reader` - a thread reads it and hands the bytes to the
loop, which is the same delivery by another road (`reader="thread"` forces
it, which is how the tests drive it on a pty).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from pathlib import Path
from typing import Callable

from . import civ, usbprobe
from ..i18n import _
from ..serialio import SerialError, SerialPort
from ..usbaudio import DECODE_RATE, Split, UsbAudio

log = logging.getLogger("shack_rig.icom.usb")

#: How long to wait for an answer before sending the next question.
#:
#: **This is the difference between a serial link that works and one that does
#: not, and it was not in the design.** Measured on the IC-7610 on `shack`: one
#: CI-V command at a time is answered in 5.2 to 6.1 milliseconds, every time.
#: Four commands written back to back - which is exactly what `Radio._poll`
#: does, four times a second - and the radio answers **only the last one**. The
#: first symptom was a page showing a live S-meter and no frequency and no
#: mode, for ever, on a link that was plainly up: the meter was the last
#: command in the burst and the other three were swallowed in silence.
#:
#: The floor is somewhere between zero and five milliseconds: a gap of 0 ms got
#: both of two commands answered 0 times out of 4, and a gap of 5 ms got both
#: 4 times out of 4, as did every larger gap tried. So this waits for the
#: *answer* rather than for a fixed gap, which is self-pacing and costs the
#: measured 6 ms rather than a guessed margin - and falls back to this timeout
#: for the commands that are never answered.
#:
#: Fifty milliseconds is eight times the measured turnaround, and four commands
#: at that worst case is 200 ms, which still fits inside a 250 ms poll. It is a
#: timeout rather than a delay: it is paid only when a command gets no reply.
REPLY_TIMEOUT = 0.05

#: And a floor under the gap, whatever the answer did.
#:
#: Waiting for the reply happens to guarantee this on the IC-7610, because its
#: reply takes six milliseconds - but only by accident of that radio being that
#: fast. The measurement is about the *gap between writes*: 0 ms got both of two
#: commands answered 0 times out of 4, and 5 ms got both 4 times out of 4. So
#: the floor is written down as itself rather than left to be a side effect of
#: how quickly the far end happens to answer, which is a property of a radio
#: nobody here has spoken to yet.
#:
#: Four commands a poll at five milliseconds is twenty milliseconds of a 250 ms
#: cycle. It is not worth measuring more finely than that.
MIN_GAP = 0.005

#: Frames waiting to go out. Bounded, because the alternative on a radio that
#: has been switched off is a queue that grows for as long as the module runs.
#: A full queue refuses, and `Radio.send` already returns that refusal upwards -
#: a poll reading missed now is replaced a quarter of a second later, and a
#: command from a person is reported as not having happened rather than
#: silently dropped.
OUTBOX = 32

#: The unkey, as it leaves this link: 1C 00 00. Recognised here, below the
#: rig, so it can go ahead of everything queued (see `send_civ`).
UNKEY = civ.set_ptt(False)

#: How many times an unkey whose write the port refused is written again,
#: and the pause between. A refused write on a serial port usually means the
#: port has gone; three tries 20 ms apart cost 60 ms and cover a USB hiccup.
UNKEY_WRITES = 3
UNKEY_RETRY_S = 0.02

#: How long `stop` lets the writer empty the queue before it lets go of the
#: port. Thirty-two frames at the 50 ms reply timeout is 1.6 s in the worst
#: case; a radio that answers takes about 6 ms a frame.
DRAIN_S = 2.0

#: Below this the spectrum will not fit and is refused rather than attempted.
#: A sweep is 475 levels in 11 frames of about 60 bytes, so roughly 660 bytes;
#: at ten sweeps a second that is 6.6 kB/s, and a line spends ten bits on a
#: byte. 19200 gives 1.9 kB/s - not close - and 115200 gives 11.5 kB/s, of
#: which the scope takes 57% and the poll loop wants the rest. So it is off by
#: default on this transport and refused outright below here, with a reason,
#: rather than presenting as a waterfall that never starts.
SCOPE_MIN_BAUD = 115200


class UsbLink:
    """One Icom on one tty: a CI-V pipe, and the probe that found it.

    The three fields that describe the port may all be empty, and that is the
    ordinary case on the Raspberry Pi appliance: nobody is going to type a
    by-id path and a baud rate into a headless box in a shack. Empty means
    "find it", and what was found is written into `report` where a person three
    hundred miles away can read it.
    """

    def __init__(self, device: str = "", baud: int = 0,
                 address: int | None = None, model_key: str = "",
                 cache_path: Path | str | None = None,
                 busy_ports: Callable[[], tuple[str, ...]] | None = None,
                 audio_device: str = "", audio: bool = True,
                 audio_backend: str = "alsa", tx_audio: str = "",
                 tx_audio_device: str = "", sound_backend=None,
                 aplay: str = "aplay", reader: str = "auto"):
        self.device = device
        self.baud = baud
        self.address = address
        self.model_key = model_key
        self.cache_path = cache_path
        self._busy_ports = busy_ports or (lambda: ())

        #: Which sound card, or empty for "find it" - the same rule as
        #: `device` above, and for the same reason: nobody is going to read a
        #: card number off a headless box in a shack, and a card *number* is an
        #: enumeration order that moves when something else is plugged in.
        self.audio_device = audio_device
        #: Whether audio is wanted at all. A radio can legitimately be control
        #: only - a rotator controller's worth of CPU instead of a continuous
        #: capture - and on the LAN transport that is what AUDIO_RATE=0 means.
        #: It means the same here.
        self.want_audio = audio
        #: What leaves this link, and it is not what the card sends. 12 kHz is
        #: what the decoders read; the card's own rate and the division down to
        #: this are `usbaudio.py`'s business and are reported in the stats.
        #: Zero until the capture is actually running, so that `Radio._follow`
        #: publishes "this radio has audio" only when it does.
        self.audio_rate = 0

        #: "alsa" (arecord, the appliance) or "portaudio" (a desktop: the
        #: device named in `audio_device`, through shack_rig.sound).
        self.audio_backend = audio_backend
        #: Transmit audio: "" none (tx_rate 0, as before stage three), "alsa"
        #: (aplay on the card `audio_device` finds) or "portaudio" (the
        #: device named in `tx_audio_device`). See txaudio.py.
        self.tx_audio = tx_audio
        self.tx_audio_device = tx_audio_device
        self._sound_backend = sound_backend
        self._aplay = aplay
        self._tx = None
        self._sound_in = None
        #: Why PortAudio receive audio did not start, for audio_stats.
        self.error_audio: str | None = None
        #: "auto": the event loop watches the port where it can; "thread":
        #: a reader thread always (module docstring).
        self.reader = reader
        self._reader_thread = None
        self._reader_stop = None
        self._loop = None

        self.on_civ: Callable[[bytes], None] | None = None
        #: Receive audio, with the name of the receiver it came from. Named
        #: rather than anonymous because on this transport a radio really can
        #: be two receivers, and a caller that wants the other one has to be
        #: able to say so. Which of them is "the audio" is decided a layer up,
        #: in `Radio`, where a radio is one thing.
        self.on_audio_from: Callable[[str, bytes], None] | None = None

        self.connected = False
        self.error: str | None = None
        self.reported_civ_address: int | None = None
        #: A serial link has no capabilities packet, so the radio never says
        #: what it is called. The model's own name is used one layer up.
        self.radio_name: str | None = None
        self.last_received = 0.0

        self.report: usbprobe.Report | None = None
        self.audio: UsbAudio | None = None
        self._port: SerialPort | None = None
        self._bytes = 0
        self._outbox: asyncio.Queue[bytes] | None = None
        self._answered: asyncio.Event | None = None
        self._writer: asyncio.Task | None = None
        self._refused = 0
        #: Unkeys waiting to be written, ahead of the outbox (`send_civ`).
        self._urgent: collections.deque[bytes] = collections.deque()
        #: Set whenever something is queued, so the writer wakes for an unkey
        #: as well as for the outbox.
        self._queued: asyncio.Event | None = None
        #: Whether the writer has a frame in hand (written, or waiting for its
        #: answer) - what `drain` waits on besides the two queues.
        self._busy = False
        #: Writes the port refused, and unkeys among them that never went.
        self.write_failures = 0
        self.unkeys_lost = 0

    def wake_preamble(self) -> bytes:
        """The FE run a sleeping radio needs on a serial line at this speed."""
        return civ.wake_preamble(self.baud)

    # -- identity ----------------------------------------------------------

    def where(self) -> str:
        """Where this radio is, for a person and for a log.

        The port that was found once there is one, and the stored one before
        that. Empty means nothing is configured and nothing has been found,
        which `main.py` reads as "there is nowhere to connect to" - the same
        test it used to make against a LAN address.
        """
        port = self._port
        if port is not None:
            return port.device
        return self.device

    @property
    def allows_scope(self) -> bool:
        return self.baud >= SCOPE_MIN_BAUD

    def scope_refusal(self) -> str | None:
        if self.allows_scope:
            return None
        return (f"The spectrum needs about 6.6 kB/s and this link runs at "
                f"{self.baud or 0} baud. It is refused rather than started, "
                f"because a waterfall that cannot keep up takes the whole link "
                f"with it and the frequency stops following the dial. Set the "
                f"radio to {SCOPE_MIN_BAUD} at Menu > Set > Connectors > CI-V "
                f"> CI-V USB Baud Rate, or use the Ethernet port.")

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Find the port if it is not known, open it, and start reading.

        The probe runs in a worker thread. It is a sequence of blocking opens
        and `select` waits that can take up to twenty seconds in the worst
        case, and the event loop it would otherwise block is the one keeping
        the IC-9700's LAN session alive four times a second - so a slow search
        for one radio must not be able to drop another.
        """
        self.error = None
        loop = asyncio.get_running_loop()
        try:
            report, port = await loop.run_in_executor(None, self._find)
        except Exception as e:                       # pragma: no cover
            log.exception("probing for a radio")
            self.error = str(e)
            return
        self.report = report

        if port is None:
            self.error = _why_not(report)
            log.warning("no radio found on USB: %s", self.error)
            return

        found = report.chosen
        self._port = port
        self.device = port.device
        self.baud = port.baud
        if found is not None and found.address is not None:
            # The same rule the LAN path follows with the capabilities packet:
            # what the radio answered from beats what was configured, because a
            # radio whose address was changed in its own menu is exactly the
            # case a probe exists to survive.
            self.reported_civ_address = found.address
            self.address = found.address
        self._outbox = asyncio.Queue(maxsize=OUTBOX)
        self._urgent.clear()
        self._queued = asyncio.Event()
        self._answered = asyncio.Event()
        self._writer = asyncio.create_task(self._write_one_at_a_time())
        self._loop = loop
        self._start_reading(loop, port)
        self.last_received = time.monotonic()
        self.connected = True
        log.info("radio on %s at %d baud, CI-V 0x%02X, found in %.1fs",
                 port.device, port.baud, self.address or 0, report.seconds)
        if not self.allows_scope:
            log.info("the spectrum is off on this link: %s", self.scope_refusal())
        await self._start_audio()
        self._start_tx_audio()

    def _start_reading(self, loop, port: SerialPort) -> None:
        """The event loop watches the port, or a thread reads it (see the
        module docstring); either way `_readable`'s work runs on the loop."""
        if self.reader != "thread":
            try:
                loop.add_reader(port.fileno(), self._readable)
                return
            except (NotImplementedError, SerialError, RuntimeError):
                pass
        import threading
        stop = threading.Event()
        self._reader_stop = stop

        def run() -> None:
            while not stop.is_set():
                try:
                    data = port.read_wait(0.1)
                except OSError as e:
                    loop.call_soon_threadsafe(self._went_away, port, e)
                    return
                if data and not stop.is_set():
                    loop.call_soon_threadsafe(self._arrived, data)

        self._reader_thread = threading.Thread(target=run, name=f"civ {port.device}", daemon=True)
        self._reader_thread.start()

    def _start_tx_audio(self) -> None:
        """Make the transmit sink, if one was asked for. It opens its device
        at the first sample of a transmission, not now (txaudio.py)."""
        from .. import txaudio
        if self.tx_audio == "alsa":
            self._tx = txaudio.AplayOut(self.audio_device, aplay=self._aplay)
        elif self.tx_audio == "portaudio":
            self._tx = txaudio.SoundOut(self.tx_audio_device, backend=self._sound_backend)
        elif self.tx_audio:
            log.warning("transmit audio %r is not alsa or portaudio; none is offered", self.tx_audio)

    async def _start_audio(self) -> None:
        """Bring the sound card up, and carry on without it if it will not.

        Deliberately after the link is marked connected and deliberately unable
        to fail the connection. The two halves of a USB radio are two devices
        that happen to be in one box: a radio whose serial port is assigned to
        this module and whose sound card is not should control perfectly and
        say in words that it cannot be heard, which is a thing somebody can
        fix. Refusing to connect at all would say the cable was wrong.
        """
        if not self.want_audio:
            log.info("receive audio is switched off for this radio")
            return
        if self.audio_backend == "portaudio":
            self._start_sound_in()
            return
        audio = UsbAudio(self.audio_device, on_pcm=self._on_audio)
        self.audio = audio
        await audio.start()
        if audio.format is not None:
            # 12 kHz whatever the card is doing - the division happens in
            # `usbaudio.py`. This is the number `DecodeRunner` reads to decide
            # whether to resample, and the answer it wants is "no".
            self.audio_rate = DECODE_RATE

    def _start_sound_in(self) -> None:
        """Receive audio through PortAudio: the named device at its own rate
        (a whole multiple of 12 kHz, or refused), divided exactly by Split,
        delivered as the one receiver "mono" from the capture's thread."""
        from .. import sound
        from ..usbaudio import AudioError, MONO
        split = None

        def on_pcm(pcm: bytes) -> None:
            out = split.feed(pcm)[0]
            if out:
                self._on_audio(MONO[0].key, out)

        try:
            self._sound_in = sound.Input(self.audio_device, on_pcm, multiple_of=DECODE_RATE,
                                         backend=self._sound_backend)
        except AudioError as e:
            self.error_audio = str(e)
            log.warning("receive audio: %s", e)
            return
        split = Split(1, self._sound_in.rate)
        self.audio_rate = DECODE_RATE

    def _on_audio(self, rx: str, pcm: bytes) -> None:
        """One block of one receiver, on its way to whatever asked for it.

        Every receiver goes up, named, and nothing is filtered here. Which one
        a caller that did not choose gets is `Radio`'s decision and is made in
        one place; making it here as well would be a second answer to the same
        question, on the transport that is the only one with two.
        """
        if self.on_audio_from is not None:
            self.on_audio_from(rx, pcm)

    def receivers(self) -> tuple:
        """The receivers this radio is actually delivering, newest fact first.

        Empty on a link with no audio, which is the same answer the LAN link
        gives for a radio streaming one channel it has not opened yet. See
        `link.py` for why both transports answer this question at all.
        """
        if self._sound_in is not None:
            from ..usbaudio import MONO
            return MONO
        return self.audio.receivers() if self.audio is not None else ()

    def _find(self) -> tuple[usbprobe.Report, SerialPort | None]:
        """The blocking half of `start`, run off the event loop."""
        cache = usbprobe.Cache(self.cache_path)
        return usbprobe.probe(
            prefer_device=self.device, prefer_baud=self.baud,
            prefer_address=self.address, prefer_model=self.model_key,
            skip=tuple(self._busy_ports()), cache=cache,
        )

    async def stop(self) -> None:
        """Release the port. Safe on a link that never started.

        Both locks go with the descriptor, so this is what lets the next
        connection - or another program - have the radio. Leaving it held is
        the serial version of the LAN radio sulking for a minute after an
        unclean disconnect, except that it lasts as long as the process does.
        """
        # What is queued goes first. The writes a caller makes just before
        # closing are the ones that put the radio back - an unkey, DATA MOD
        # restored - and they were queued, not written: cancelling the writer
        # straight away threw them away with the queue. That is how an
        # IC-9700 was left on DATA MOD = USB after a USB session on
        # 18/10/2026, though the restore had been "sent".
        if self._writer is not None and self.connected:
            await self.drain(DRAIN_S)
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.cancel()
        # Before the port, because this one holds a device another program -
        # or the next connection - will want. An `arecord` left running makes
        # the capture device busy, so the reconnection finds no audio and
        # presents as a radio that has gone deaf, which is the sound-card
        # version of the serial lock left held below.
        audio, self.audio = self.audio, None
        if audio is not None:
            await audio.stop()
        sound_in, self._sound_in = self._sound_in, None
        if sound_in is not None:
            sound_in.close()
        # The transmit sink too: a stopped link is never a transmitting one,
        # and an aplay or a sound stream left open is a device held.
        tx, self._tx = self._tx, None
        if tx is not None:
            tx.close()
        self.audio_rate = 0
        port, self._port = self._port, None
        self.connected = False
        self._outbox = None
        if self._reader_stop is not None:
            self._reader_stop.set()
            self._reader_stop = None
        if port is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(port.fileno())
        except (SerialError, RuntimeError, ValueError, NotImplementedError):
            pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        port.close()

    # -- the CI-V pipe -----------------------------------------------------

    def _readable(self) -> None:
        """Bytes have arrived. Hand them on exactly as they came.

        Not reassembled here. A serial port splits a frame across two reads as
        a matter of course, and `Radio._buf` with `civ.split_frames` is already
        the thing that copes with that - a second buffer in the transport would
        be a second place for the same bug, and only one of them would be
        tested.
        """
        port = self._port
        if port is None:
            return
        try:
            data = port.read()
        except OSError as e:
            self._went_away(port, e)
            return
        self._arrived(data)

    def _went_away(self, port: SerialPort, e: OSError) -> None:
        # The radio was switched off or unplugged. Reported rather than
        # raised: `Radio._supervise` is already watching, and it rebuilds
        # the link on its own timer.
        self.error = f"{port.device} went away: {e.strerror or e}"
        log.warning("%s", self.error)
        self.connected = False

    def _arrived(self, data: bytes) -> None:
        if not data or self._port is None:
            return
        self._bytes += len(data)
        self.last_received = time.monotonic()
        if self._answered is not None:
            # The radio has spoken, so the next question may go. See
            # REPLY_TIMEOUT: this is what makes the pacing cost six
            # milliseconds rather than fifty.
            self._answered.set()
        if self.on_civ is not None:
            self.on_civ(data)

    def send_civ(self, frame: bytes) -> bool:
        """Queue a frame. The writer below decides when it actually goes.

        Queued rather than written, because the radio answers one question at a
        time - see REPLY_TIMEOUT. Everything above this stays exactly as it was
        on the LAN: `Radio._poll` asks its four questions in a burst and does
        not know that on a wire they leave one at a time.

        **An unkey jumps the queue.** It goes into a queue of its own that
        the writer empties first, and a full outbox does not refuse it. In
        one place it was behind up to 32 poll reads at 50 ms each - more than
        a second and a half of transmitting after the station had stopped,
        and the reason a link's unkey could reach a USB radio late
        (shack-packet HANDOVER, 18/10/2026). A second unkey while one is
        still waiting adds nothing: the radio needs telling once.
        """
        if self._port is None or not self.connected or self._outbox is None:
            return False
        if self._is_unkey(frame):
            if frame not in self._urgent:
                self._urgent.append(frame)
            if self._queued is not None:
                self._queued.set()
            return True
        try:
            self._outbox.put_nowait(frame)
        except asyncio.QueueFull:
            self._refused += 1
            return False
        if self._queued is not None:
            self._queued.set()
        return True

    @staticmethod
    def _is_unkey(frame: bytes) -> bool:
        try:
            return civ.parse(frame)[2] == UNKEY
        except civ.CivError:
            return False

    def _next_frame(self) -> bytes | None:
        if self._urgent:
            return self._urgent.popleft()
        if self._outbox is not None and not self._outbox.empty():
            return self._outbox.get_nowait()
        return None

    async def drain(self, timeout: float = DRAIN_S) -> bool:
        """Wait until everything queued has been written (and its answer
        waited for). True if it emptied within `timeout`."""
        deadline = time.monotonic() + timeout
        while self._urgent or self._busy or (self._outbox is not None and not self._outbox.empty()):
            if self._writer is None or self._writer.done() or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.005)
        return True

    def _write(self, port: SerialPort, frame: bytes) -> bool:
        """One frame to the port. `SerialPort.write` says False when not all
        of it went, which was ignored: a refused unkey was then believed sent.
        An unkey is tried again (UNKEY_WRITES); anything else is counted and
        left to the poll, which asks again within a quarter of a second."""
        if port.write(frame):
            return True
        self.write_failures += 1
        log.warning("%s: a CI-V frame was not written (%s)", self.device, frame.hex(" "))
        return False

    async def _write_unkey(self, port: SerialPort, frame: bytes) -> None:
        for attempt in range(UNKEY_WRITES):
            if self._write(port, frame):
                return
            await asyncio.sleep(UNKEY_RETRY_S)
        self.unkeys_lost += 1
        log.error("%s: the unkey could not be written after %d tries; the radio may still be transmitting",
                  self.device, UNKEY_WRITES)

    async def _write_one_at_a_time(self) -> None:
        """Send a frame, wait for the answer, send the next.

        The whole reason this file has a writer task rather than writing in
        `send_civ`. Self-pacing: the wait ends when a reply arrives, which the
        IC-7610 does in about six milliseconds, so four questions cost about
        twenty-five and not four times a timeout. The timeout is only paid by a
        command the radio does not answer.

        It waits for *any* inbound bytes rather than for the matching reply. A
        radio also volunteers frames nobody asked for - turn the dial and one
        arrives - so pairing questions to answers here would need a second
        parser beside the one in `Radio`, and the only thing it would buy is
        being slightly less eager. Being slightly early is harmless; the
        measured floor is five milliseconds and a round trip is six.

        Unkeys come first (`send_civ`), from their own queue.
        """
        while True:
            try:
                frame = self._next_frame()
                if frame is None:
                    self._queued.clear()
                    frame = self._next_frame()
                    if frame is None:
                        await self._queued.wait()
                        continue
                port = self._port
                if port is None:
                    return
                self._busy = True
                try:
                    self._answered.clear()
                    sent = time.monotonic()
                    if self._is_unkey(frame):
                        await self._write_unkey(port, frame)
                    else:
                        self._write(port, frame)
                    try:
                        await asyncio.wait_for(self._answered.wait(), REPLY_TIMEOUT)
                    except asyncio.TimeoutError:
                        pass
                    left = MIN_GAP - (time.monotonic() - sent)
                    if left > 0:
                        await asyncio.sleep(left)
                finally:
                    self._busy = False
            except asyncio.CancelledError:
                raise
            except Exception:                                # pragma: no cover
                log.exception("writing to %s", self.device)
                return

    def poll_error(self) -> str | None:
        return self.error

    # -- audio: receive, from usbaudio.py; transmit, not yet ---------------

    def audio_stats(self) -> dict:
        """What the sound card is doing, in the shape the LAN path publishes.

        The `enabled`/`rate`/`running`/`packets`/`dropped` five are the LAN
        link's, so one page renders either transport without asking which it
        is looking at. Everything beside them is this transport's own and is
        the part that makes a silent radio diagnosable from another building:
        which card was chosen and why, what it said it could do, what the
        levels are on each channel, and whether the two channels are two
        receivers or one receiver sent twice.
        """
        if self._sound_in is not None:
            return {"enabled": True, "rate": DECODE_RATE, "running": True,
                    "packets": self._sound_in.blocks, "dropped": self._sound_in.dropped,
                    "device": self._sound_in.device.name, "capture_rate": self._sound_in.rate}
        if self.audio is None and self.error_audio:
            return {"enabled": self.want_audio, "rate": None, "running": False,
                    "packets": 0, "dropped": 0, "error": self.error_audio}
        if self.audio is None:
            return {"enabled": self.want_audio, "rate": None, "running": False,
                    "packets": 0, "dropped": 0,
                    "error": None if self.want_audio else
                    _("receive audio is switched off for this radio")}
        return self.audio.stats()

    # -- transmit audio: the TxAudioSink contract, forwarded to txaudio.py --
    #
    # Zero and False with no sink, exactly as before stage three, so a radio
    # nobody asked to transmit through still has no transmit panel. The rig
    # above refuses send_audio unless it is keyed under a lease; this only
    # carries the samples.

    @property
    def tx_rate(self) -> int:
        return self._tx.tx_rate if self._tx is not None else 0

    @property
    def tx_audio_ready(self) -> bool:
        return bool(self._tx is not None and self._tx.tx_audio_ready)

    @property
    def tx_drain_s(self) -> float:
        return float(self._tx.tx_drain_s) if self._tx is not None else 0.0

    def send_audio(self, pcm: bytes) -> bool:
        if self._tx is None or len(pcm) % 2:
            return False
        return bool(self._tx.send_audio(pcm))

    def tx_end(self) -> None:
        """The last sample is in; the sink plays out what it has
        (`tx_drain_s` says how long) and lets go of the device."""
        if self._tx is not None:
            self._tx.tx_end()

    def tx_abort(self) -> None:
        """A transmission stopped part way: drop what has not played."""
        if self._tx is not None:
            self._tx.close()

    # -- what a person somewhere else needs to see -------------------------

    def diagnostics(self) -> dict | None:
        """The probe transcript, for reading from another building.

        This is the reason the probe records what it saw rather than only what
        it concluded. An appliance may be deployed with a radio nobody here has
        ever spoken to, in a shack with nobody on site, and "no radio found" is
        not something a person on the telephone can act on. Which ports exist,
        what each announced itself as, which speeds were tried, what came back
        as hex and why each was rejected - all of it is here, and the case it
        exists for is the one where a port answered something that was not
        CI-V.
        """
        if self.report is None:
            return None
        got = self.report.public()
        got["bytes"] = self._bytes
        got["scope_refused"] = self.scope_refusal()
        # The sound card is the other half of a USB radio and it fails in its
        # own ways - not assigned to the module, held by something else, or
        # there and offering nothing this module can divide into 12 kHz. None
        # of those is visible in a CI-V transcript, and all of them present as
        # a radio that controls perfectly and decodes nothing.
        got["audio"] = self.audio.stats() if self.audio is not None else None
        # How many commands were refused because the radio was not draining
        # them. Zero on a link that is working; anything else says the radio
        # has stopped answering rather than that the module has stopped asking.
        got["refused"] = self._refused
        got["write_failures"] = self.write_failures
        got["unkeys_lost"] = self.unkeys_lost
        return got


def _why_not(report: usbprobe.Report) -> str:
    """One sentence saying which of the ways this failed it was.

    `docs/TRANSPORTS.md` §7.4 asks for exactly this: the port would not open,
    the port opened and nothing came back, or something came back that was not
    CI-V. "Not connected" was shown for all three and is the single least
    useful string in the module - and on an appliance it is the difference
    between a telephone call and a drive.
    """
    if not report.findings:
        return ("no serial ports are visible at all. If the radio is plugged "
                "in and switched on, the device has probably not been assigned "
                "to this module - Dock360's module page, Devices.")
    for want in ("foreign", "busy", "denied", "missing"):
        for f in report.findings:
            if f.outcome == want:
                return f.summary
    if report.hit_deadline:
        return (f"the probe ran out of time after {report.seconds:.0f}s with "
                f"ports left untried; what it did see is below")
    return ("every serial port opened and none of them answered, at any speed "
            "or CI-V address. Check that the radio is switched on, that its "
            "CI-V USB Port is set to Unlink, and which of its ports the cable "
            "reaches.")
