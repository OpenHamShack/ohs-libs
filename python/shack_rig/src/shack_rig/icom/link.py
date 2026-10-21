"""The seam under `Radio`: one way of reaching an Icom.

`rig.py` used to construct an Icom LAN control stream itself, in four places,
and the shape of connecting was Icom-LAN-shaped: a handshake happens, it yields
two port numbers, and those port numbers become two more sockets. A serial port
has none of that, and a radio on the end of a cable is the Raspberry Pi
appliance's only path - see `docs/STANDALONE-PI.md` - so it is the main case
rather than a variant.

Everything else in `rig.py` was already transport-independent and stays
exactly where it is: the poll loop, the supervisor, the state cache, the CI-V
reassembly and every `set_*`. `civ.py` needed nothing at all - its own docstring
has said "this module knows nothing about sockets" since before there was a
second transport to prove it, and `ScopeAssembler` already carries
`SCOPE_SEQ_MAX = 11`, the frames-per-spectrum for a serial link, transcribed
from the wfview notes years before this module had one.

**A `Link` is a pipe for CI-V frames and the audio beside them, and nothing
more.** Five things about the contract below are decisions rather than
descriptions, and each was arrived at by considering the alternative:

`send_civ` takes a whole frame, not a command. Framing stays in `civ.py`. A
link that framed would hold the CI-V address itself, and a transport with its
own idea of who it is talking to is a second place for that to be wrong.

`on_civ` delivers bytes, not frames. `Radio._buf` and `civ.split_frames`
already handle a frame straddling two reads - the rare case on UDP, the normal
case on a serial port - and a second reassembly in the link would mean two
buffers, one of which would be the one with the bug.

There is no `connect()` apart from `start()`. The LAN link's connect is a
multi-step handshake with retries of its own; the USB link's is an `open()`.
`Radio._supervise` already treats connecting as "start it and watch
`last_received`", which covers both without either explaining itself.

`reported_civ_address` and `radio_name` are None rather than absent, so that a
reader of `rig.py` can see that a transport may not know them.

**`receivers()` is the fifth, and it is the newest and the least obvious.** A
link used to deliver "the audio", singular, and the whole digital-modes design
rested on that: one decoder per radio, *because a radio has one audio stream*.
That sentence was true of every radio this module had spoken to, and it is not
true of the one it was built for. The IC-7610's ACC/USB output can put Main on
the left channel and Sub on the right, and off air the two were shown to be
genuinely independent - the left channel decoded FT8 from Belize on 14.074 and
the right decoded none, from one antenna, in the same minute. `docs/OFF-AIR.md`
has the measurement.

So a link says how many receivers it is delivering and what they are called,
and the audio callback is `on_audio_from(receiver, pcm)` rather than
`on_audio(pcm)`. One callback and not two: a link that offered both would have
one of them exercised in production and the other exercised only by whichever
test remembered it existed, and the two would drift. "The audio", singular,
goes on meaning something - it is `Radio.listen_audio` with no receiver named,
which is what every caller above here already writes and what `DecodeRunner`
still does today. Deciding which receiver that is belongs to `Radio`, because
it is the layer that knows a radio is one thing.

The LAN link answers "one receiver" rather than not answering: a transport that
cannot be asked is a transport `rig.py` has to special-case, and the entire
value of this seam is that it does not.

---

**The `Rig` surface is the other seam**, one layer up, and it is written down
in `shack_rig/rig.py`: everything above a rig reads only that, and nothing
above `IcomRig` forms a CI-V frame. The test of this whole design is that
`shack_rig.rigctl` needs no edit to drive a radio on a serial port, and
tests/test_usb.py runs it against one. If it ever does need an edit, the seam
is in the wrong place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .lan import AudioStream, ControlStream, CivStream
from ..usbaudio import Receiver

log = logging.getLogger("shack_rig.icom.link")

#: What the LAN protocol delivers, named so that it can be talked about.
#:
#: One mono stream, agreed during the handshake, and there is no second one to
#: ask the radio for - the conninfo packet names one codec and one rate. So
#: this is not "the Main receiver" and is deliberately not called that: it is
#: whichever receiver the radio's own output setting puts on the network, which
#: this end cannot see and has no way to choose between.
LAN_RECEIVER = Receiver("lan", 0, "The radio's network audio stream")


class LanLink:
    """Icom's own LAN protocol: three UDP conversations behind one pipe.

    A thin adapter over `icomlan.py` rather than a rewrite of it - the
    handshake, the token, the retransmit buffer and the two watchdogs are
    unchanged and stay where they are documented. What moved here is only the
    part `rig.py` was doing: build a control stream, and when it reports the
    CI-V and audio ports the radio has allocated, open those too.

    Built fresh for every connection attempt, exactly as the control stream was
    before, so a reconnection starts from nothing rather than from a stream
    whose sequence numbers and retransmit buffer belong to a session the radio
    has forgotten.
    """

    def __init__(self, host: str, port: int, username: str, password: str,
                 comp_name: str = "OpenHamShack", audio_rate: int = 0,
                 tx_rate: int = 0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.comp_name = comp_name
        self.audio_rate = audio_rate
        #: Non-zero only when this radio may be keyed at all. A radio told to
        #: expect a transmit stream waits for one, so asking for it when
        #: transmitting is switched off would be claiming an intention the
        #: module does not have - and it is one more gate between a bug and a
        #: keyed transmitter. Decided by whoever builds the link, because that
        #: is where TX_ENABLED is known.
        self.want_tx_rate = tx_rate

        self.on_civ: Callable[[bytes], None] | None = None
        #: Receive audio, with the name of the receiver it came from. One
        #: receiver here, always: Icom's LAN protocol negotiates a single mono
        #: stream and there is no second one to ask the radio for. Named
        #: anyway, so that `Radio` has one way of subscribing rather than one
        #: per transport.
        self.on_audio_from: Callable[[str, bytes], None] | None = None

        self.connected = False
        self.error: str | None = None
        self.reported_civ_address: int | None = None
        self.radio_name: str | None = None

        self._control: ControlStream | None = None
        self._civ: CivStream | None = None
        self._audio: AudioStream | None = None

    def where(self) -> str:
        return f"{self.host}:{self.port}" if self.host else ""

    def wake_preamble(self) -> bytes:
        """Nothing: the radio's LAN side is awake while the radio sleeps.

        See civ.set_power. wfview sends no preamble on the native LAN link,
        and the CI-V stream stays open (pings, idles, an open request the
        radio accepts) through standby, so the frame is delivered as is.
        """
        return b""

    @property
    def last_received(self) -> float:
        ctl = self._control
        return ctl.last_received if ctl is not None else 0.0

    @property
    def wedged(self) -> bool:
        """The CI-V stream was opened and has never carried a frame.

        See `CivStream.wedged`. A property of this link rather than of every
        link: a serial port has no session on the radio to go stale, so the
        supervisor asks with getattr and the USB link need not answer.
        """
        civ = self._civ
        return bool(civ is not None and civ.wedged)

    async def start(self) -> None:
        log.info("connecting to %s:%d", self.host, self.port)
        self.error = None
        ctl = ControlStream(
            self.host, self.port, self.username, self.password, self.comp_name,
            audio_rate=self.audio_rate, tx_rate=self.want_tx_rate,
        )

        def on_civ_port(port: int) -> None:
            asyncio.create_task(self._open_civ(port))

        def on_audio_port(port: int) -> None:
            asyncio.create_task(self._open_audio(port))

        ctl.on_civ_port = on_civ_port
        ctl.on_audio_port = on_audio_port
        self._control = ctl
        await ctl.start()

    async def stop(self) -> None:
        """Take all three streams down, and say goodbye on the way.

        The audio stream is a UDP conversation of its own - its own socket, its
        own are-you-there, its own keepalive and purge tasks - and it was the
        one `rig.py` never took down. So every reconnection and every
        settings change left the previous one running for as long as the
        process did: a socket still open, two tasks still awake, and packets
        still going to the radio from an object nothing holds any more. It also
        never sent the disconnect, so the radio went on believing that client
        was present. Measured before that was fixed: three datagrams in the
        first second and a half after `stop()` returned, none of them a
        disconnect.
        """
        if self._civ is not None:
            await self._civ.stop()
            self._civ = None
        if self._audio is not None:
            await self._audio.stop()
            self._audio = None
        if self._control is not None:
            await self._control.stop()
            self._control = None
        self.connected = False

    def send_civ(self, frame: bytes) -> bool:
        if self._civ is None or not self.connected:
            return False
        self._civ.send_civ(frame)
        return True

    def poll_error(self) -> str | None:
        """Whatever the control stream is currently unhappy about.

        Read on the supervisor's tick rather than pushed, because the control
        stream sets it from packet handlers that have nowhere to push to. The
        three that matter - a refused login, the radio being in use by somebody
        else, and a radio that wants a power cycle - are all sentences a person
        can act on, which is why they reach the page verbatim.
        """
        ctl = self._control
        self.error = ctl.error if ctl is not None else self.error
        return self.error

    async def _open_civ(self, port: int) -> None:
        ctl = self._control
        if ctl is None:
            return
        if self._civ is not None:
            await self._civ.stop()
        self._civ = CivStream(self.host, port, ctl.civ_local_port,
                              self._deliver_civ)
        await self._civ.start()
        self.radio_name = (ctl.current or {}).get("name")
        # The radio tells us its own CI-V address in the capabilities packet.
        # Trusting that over the configured value means a radio whose address
        # was changed in its menu still works, instead of going quiet.
        self.reported_civ_address = (ctl.current or {}).get("civ") or None
        self.error = None
        self.connected = True

    async def _open_audio(self, port: int) -> None:
        """Start reading the audio the radio has agreed to send."""
        ctl = self._control
        if ctl is None:
            return
        if self._audio is not None:
            await self._audio.stop()
        self._audio = AudioStream(self.host, port, ctl.audio_local_port,
                                  self._deliver_audio, tx_rate=ctl.tx_rate)
        await self._audio.start()

    def _deliver_civ(self, data: bytes) -> None:
        if self.on_civ is not None:
            self.on_civ(data)

    def _deliver_audio(self, pcm: bytes) -> None:
        if self.on_audio_from is not None:
            self.on_audio_from(LAN_RECEIVER.key, pcm)

    def receivers(self) -> tuple:
        """One, once there is a stream, and nothing before there is.

        Nothing before, rather than one unconditionally, because the question
        is what this radio is *delivering* and not what the protocol could
        deliver. A page that offers a receiver on a radio whose audio stream
        was never opened offers a decoder that will sit at zero periods.
        """
        return (LAN_RECEIVER,) if self._audio is not None else ()

    def audio_stats(self) -> dict:
        a = self._audio
        return {
            "enabled": bool(self.audio_rate),
            "rate": self.audio_rate or None,
            "running": a is not None,
            "packets": a.packets if a else 0,
            "dropped": a.dropped if a else 0,
        }

    # -- transmit audio: the TxAudioSink contract, forwarded --------------
    #
    # `digi/transmit.py` wrote this contract down before anything could satisfy
    # it - tx_rate, tx_audio_ready, send_audio, and the two that say when a
    # transmission has drained - and the LAN audio stream has grown it. The
    # forwarding is here rather than in `rig.py` for the same reason
    # everything else in this file is: whether a way of reaching a radio can
    # also carry audio *to* it is a fact about the way, not about the radio.

    @property
    def tx_rate(self) -> int:
        return getattr(self._audio, "tx_rate", 0) if self._audio else 0

    @property
    def tx_audio_ready(self) -> bool:
        return bool(getattr(self._audio, "tx_audio_ready", False)) if self._audio else False

    @property
    def tx_drain_s(self) -> float:
        return float(getattr(self._audio, "tx_drain_s", 0.0)) if self._audio else 0.0

    def send_audio(self, pcm: bytes) -> bool:
        if not self._audio:
            return False
        return bool(self._audio.send_audio(pcm))

    def tx_end(self) -> None:
        if self._audio and hasattr(self._audio, "tx_end"):
            self._audio.tx_end()

    def diagnostics(self) -> dict | None:
        """Nothing to add. A LAN radio either answers on its address or does not.

        The USB link returns a probe transcript here, because on that transport
        the port, the speed and the CI-V address are all discovered and any of
        them can be the reason nothing works. On the LAN there is one address
        and the control stream's own error says what went wrong with it.
        """
        return None
