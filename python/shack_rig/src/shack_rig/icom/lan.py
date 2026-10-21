"""Icom's LAN control protocol - the envelope CI-V arrives in.

An IC-9700 on Ethernet does not speak CI-V on a socket. It speaks a protocol of
Icom's own, on three UDP ports: control (50001), CI-V (50002) and audio
(50003). All three are implemented here. The audio port carries both
directions on one socket and one pair of sequence numbers, and each direction
is negotiated separately in the conninfo packet below: a client that wants
neither asks for neither, and the radio then sends nothing and expects
nothing.

The shape of it:

    we -> radio   "are you there"          every 500ms until answered
    radio -> we   "I am here"              and tells us its id
    we -> radio   "are you ready"
    radio -> we   "I am ready"
    we -> radio   login (username+password, obfuscated)
    radio -> we   a token
    we -> radio   token confirm
    radio -> we   capabilities: what radios this box has
    radio -> we   conninfo: whether each is busy
    we -> radio   "open me a CI-V stream, here is the port I am listening on"
    radio -> we   status: the CI-V port to talk to
    then the CI-V stream is opened the same way, and CI-V frames flow

Every packet carries a sequence number and the radio will ask for any it missed
by number, so sent packets are kept for a few seconds in case one is wanted
again. That retransmit buffer is the reason this is not simply a socket.

The packet layouts, the handshake order and the password obfuscation all come
from wfview (GPL-3.0, https://gitlab.com/eliggett/wfview), which is the
reverse-engineering of record for this protocol - Icom document none of it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import struct
import time
from typing import Callable

log = logging.getLogger("shack_rig.icom.lan")

CONTROL_SIZE = 0x10
PING_SIZE = 0x15
OPENCLOSE_SIZE = 0x16
TOKEN_SIZE = 0x40
STATUS_SIZE = 0x50
LOGIN_RESPONSE_SIZE = 0x60
LOGIN_SIZE = 0x80
CONNINFO_SIZE = 0x90
#: The audio header, both directions. Fixed, with the samples after it.
AUDIO_HEADER_SIZE = 0x18
CAPABILITIES_SIZE = 0x42
RADIO_CAP_SIZE = 0x66

PING_PERIOD = 0.5
IDLE_PERIOD = 0.1
AREYOUTHERE_PERIOD = 0.5
TOKEN_RENEWAL = 60.0
PURGE_SECONDS = 10.0
STALE_CONNECTION = 15.0
#: How long a CI-V stream may go without a single frame after it was asked to
#: open before it is called wedged. A healthy IC-9700 answers the open inside a
#: second, and a radio in standby still answers every frame with NG, so ten
#: seconds of nothing at all is neither of those. See CivStream.wedged.
CIV_WEDGED_S = 10.0

#: The control stream's error for a refused login, as the page shows it.
#: Named because the supervisor (rig.py) treats it differently from every
#: other failure: a wrong password does not come right by trying again.
AUTH_REJECTED = "the radio rejected that username and password"

# Packet types, in the `type` field of every header.
T_IDLE = 0x00
T_RETRANSMIT = 0x01
T_AREYOUTHERE = 0x03
T_IAMHERE = 0x04
T_DISCONNECT = 0x05
T_AREYOUREADY = 0x06
T_PING = 0x07

#: The substitution table Icom obfuscate usernames and passwords with. It is
#: not encryption and is not treated as any: the credentials still have to be
#: stored here in a form this can reverse, which is why the module asks for a
#: radio login rather than pretending it is protecting one.
_PASSCODE_TABLE = bytes(
    [0] * 32
    + [
        0x47, 0x5D, 0x4C, 0x42, 0x66, 0x20, 0x23, 0x46, 0x4E, 0x57, 0x45, 0x3D,
        0x67, 0x76, 0x60, 0x41, 0x62, 0x39, 0x59, 0x2D, 0x68, 0x7E, 0x7C, 0x65,
        0x7D, 0x49, 0x29, 0x72, 0x73, 0x78, 0x21, 0x6E, 0x5A, 0x5E, 0x4A, 0x3E,
        0x71, 0x2C, 0x2A, 0x54, 0x3C, 0x3A, 0x63, 0x4F, 0x43, 0x75, 0x27, 0x79,
        0x5B, 0x35, 0x70, 0x48, 0x6B, 0x56, 0x6F, 0x34, 0x32, 0x6C, 0x30, 0x61,
        0x6D, 0x7B, 0x2F, 0x4B, 0x64, 0x38, 0x2B, 0x2E, 0x50, 0x40, 0x3F, 0x55,
        0x33, 0x37, 0x25, 0x77, 0x24, 0x26, 0x74, 0x6A, 0x28, 0x53, 0x4D, 0x69,
        0x22, 0x5C, 0x44, 0x31, 0x36, 0x58, 0x3B, 0x7A, 0x51, 0x5F, 0x52,
    ]
    # Padded to 256. The shift above can carry an index as high as 158, and
    # wfview's own table stops at 158 exactly; padding past that costs nothing
    # and removes the only way this function could raise.
    + [0] * (256 - 127)
)


#: The codec byte in a conninfo packet, as wfview's audioconverter.h lists
#: them. 0x04 is the one worth having: one channel of 16-bit linear PCM is
#: exactly what a decoder wants, with nothing to decompress on the way.
#:
#:     0x01 uLaw 1ch 8bit     0x08 PCM 2ch 8bit
#:     0x02 PCM  1ch 8bit     0x10 PCM 2ch 16bit
#:     0x04 PCM  1ch 16bit    0x20 uLaw 2ch 8bit
#:     0x40 Opus 1ch          0x80 Opus 2ch
CODEC_PCM_MONO_16 = 0x04

#: The rates the radio will send. 12000 is here because it is what jt9 and the
#: rest of the WSJT-X decoders read, so asking the radio for it means no
#: resampling anywhere - and a resampler in the path of every packet is both
#: work and a source of error in a signal that is already near the noise.
AUDIO_RATES = (8000, 12000, 16000, 24000, 48000)
DEFAULT_AUDIO_RATE = 12000

#: The most audio one transmit packet carries. wfview and kappanhang both cut
#: at exactly this, which is where it comes from; it is under the 1472 bytes
#: that fit an Ethernet frame once the IP, UDP and audio headers are paid for,
#: so a packet of transmit audio is never fragmented. A fragmented datagram is
#: one the radio loses whole if either half goes missing, and audio that has
#: gone missing cannot usefully be asked for again.
TX_PACKET_BYTES = 1364

#: What the radio is told to hold before it starts playing what we send, in
#: milliseconds, in the txbuffer field of conninfo. It is the radio's jitter
#: buffer: too small and any late packet is a gap in the transmitted audio,
#: too large and everything we send comes out that much later than we meant
#: it to. 100 ms is wfview's default and what the earlier app used against
#: this same radio.
TX_BUFFER_MS = 100

#: Silence sent after the last real sample, and how long PTT is then held.
#:
#: Audio handed to the radio is not audio that has been transmitted yet - it
#: is in the buffer above. Unkeying the moment the last sample is handed over
#: therefore cuts the end off the transmission, and for FT8 the end is as much
#: of the message as the beginning. The silence gives the buffer something to
#: play out rather than underrunning into a click while still keyed, and the
#: hold gives it time to do so. Both come from the earlier app, which did this
#: against the real radio and had the transmissions decoded by other stations.
TX_TAIL_MS = 100
TX_DRAIN_S = 0.25



def passcode(text: str) -> bytes:
    """Obfuscate a username or password the way the radio expects.

    Each character is shifted by its own position and then substituted, so the
    same password does not produce the same bytes in the username field. Capped
    at 16 characters because that is the size of the field it goes into.
    """
    out = bytearray()
    for i, ch in enumerate(text.encode("latin-1", "replace")[:16]):
        p = ch + i
        if p > 126:
            p = 32 + p % 127
        out.append(_PASSCODE_TABLE[p])
    return bytes(out)


def _cstr(b: bytes) -> str:
    """A NUL-terminated string out of a fixed-width field."""
    end = b.find(b"\x00")
    return b[: end if end >= 0 else len(b)].decode("latin-1", "replace")


class _Stream(asyncio.DatagramProtocol):
    """One UDP conversation with the radio: sequencing, retransmits, keepalive.

    Both the control stream and the CI-V stream are this, differing only in
    what they do with the packets that are not bookkeeping.
    """

    def __init__(self, host: str, port: int, name: str):
        self.host = host
        self.port = port
        self.name = name
        self.transport: asyncio.DatagramTransport | None = None
        self.my_id = 0
        self.remote_id = 0
        self.local_port = 0
        self.send_seq = 1
        # The CI-V stream carries a second, independent sequence inside the
        # payload, counted separately from the outer one.
        self.inner_seq = 0
        self.auth_seq = 0x30
        self.connected = False
        self.last_received = 0.0
        self._tx_buf: dict[int, tuple[float, bytes]] = {}
        self._seen: set[int] = set()
        self._tasks: list[asyncio.Task] = []
        self._areyouthere = True
        #: When a packet that carries the sequence forward last went out; see
        #: _keepalive for why the idle waits on it.
        self.last_tracked = 0.0
        #: Pings are numbered on their own, as wfview and the earlier app
        #: number them, moving on when the radio answers one. They used to
        #: borrow send_seq, so every ping carried the number the next
        #: tracked packet was about to take.
        self.ping_seq = 0
        #: Packets the radio asked for again, and how many of those were no
        #: longer held. On the audio stream mid-over, each is audio the radio
        #: did not have when it wanted it.
        self.retransmits = 0
        self.retransmits_missing = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self, remote_addr=(self.host, self.port)
        )
        sock = self.transport.get_extra_info("socket")
        local_ip, self.local_port = sock.getsockname()[:2]
        addr = struct.unpack(">I", socket.inet_aton(local_ip))[0]
        # The radio identifies us by this, and wfview builds it from the low
        # two octets of our address and our port. It only has to be stable and
        # unlikely to collide with another client of the same radio.
        self.my_id = ((addr >> 8 & 0xFF) << 24) | ((addr & 0xFF) << 16) | (self.local_port & 0xFFFF)
        self.last_received = time.monotonic()
        log.info("%s stream: %s:%d from local port %d", self.name, self.host, self.port, self.local_port)
        self.send_control(T_AREYOUTHERE, tracked=False)
        self._tasks = [
            asyncio.create_task(self._keepalive()),
            asyncio.create_task(self._purge()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self.transport is not None:
            try:
                self.send_control(T_DISCONNECT, tracked=False)
            except Exception:
                pass
            self.transport.close()
            self.transport = None
        self.connected = False

    async def _keepalive(self) -> None:
        """Are-you-there until answered, then ping and idle for as long as we live.

        The radio drops a client it has not heard from, and the idle packet is
        also what carries our sequence number forward so a gap is visible to it.

        **An idle only after IDLE_PERIOD with nothing tracked sent.** Anything
        tracked carries the sequence forward already, so an idle beside it
        does no good, and on the audio stream it does harm. Until 18/10/2026
        an idle went out every 100 ms whatever else was flowing, and on the
        audio stream that put a place in the sequence with no audio in it
        between two packets of transmit audio. The IC-9700 played each one as
        a hole of one packet's length, so a packet station's audio had 15-20
        ms of nothing about every 100 ms and nobody could decode it
        (shack-packet HANDOVER.md). wfview resets its idle timer on every
        tracked send and never idles on the audio stream at all. The earlier
        Icom app, whose LAN transmissions were decoded on air, idled only
        after 100 ms with nothing sent. This is that rule again.
        """
        last_ping = 0.0
        while True:
            try:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                if self._areyouthere:
                    if now - last_ping >= AREYOUTHERE_PERIOD:
                        self.send_control(T_AREYOUTHERE, tracked=False)
                        last_ping = now
                    continue
                if now - last_ping >= PING_PERIOD:
                    self.send_ping()
                    last_ping = now
                if now - self.last_tracked >= IDLE_PERIOD:
                    self.send_control(T_IDLE, tracked=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: keepalive", self.name)

    async def _purge(self) -> None:
        """Forget sent packets too old to be worth retransmitting."""
        while True:
            try:
                await asyncio.sleep(1.0)
                cutoff = time.monotonic() - PURGE_SECONDS
                for seq in [s for s, (t, _) in self._tx_buf.items() if t < cutoff]:
                    del self._tx_buf[seq]
                if len(self._seen) > 4000:
                    self._seen.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: purge", self.name)

    # -- sending -----------------------------------------------------------

    def _header(self, length: int, ptype: int, seq: int = 0) -> bytearray:
        p = bytearray(length)
        struct.pack_into("<IHHII", p, 0, length, ptype, seq, self.my_id, self.remote_id)
        return p

    def send_raw(self, data: bytes) -> None:
        if self.transport is not None:
            self.transport.sendto(bytes(data))

    def send_tracked(self, data: bytearray) -> None:
        """Send with our sequence number, keeping a copy in case it is missed."""
        seq = self.send_seq & 0xFFFF
        struct.pack_into("<H", data, 6, seq)
        self._tx_buf[seq] = (time.monotonic(), bytes(data))
        self.send_seq = (self.send_seq + 1) & 0xFFFF
        if self.send_seq == 0:
            # Sequence has wrapped; the old buffer refers to a previous cycle
            # and answering from it would send the radio a packet from minutes
            # ago under a number it is expecting now.
            self._tx_buf.clear()
        self.last_tracked = time.monotonic()
        self.send_raw(data)

    def send_control(self, ptype: int, tracked: bool = True, seq: int = 0) -> None:
        p = self._header(CONTROL_SIZE, ptype, seq)
        if tracked:
            self.send_tracked(p)
        else:
            self.send_raw(p)

    def send_ping(self) -> None:
        p = self._header(PING_SIZE, T_PING, self.ping_seq)
        # Untracked and with its own counter: a ping is a measurement, and
        # retransmitting a stale one would measure the wrong thing.
        p[0x10] = 0x00
        struct.pack_into("<I", p, 0x11, int(time.monotonic() * 1000) & 0xFFFFFFFF)
        self.send_raw(p)

    # -- receiving ---------------------------------------------------------

    def connection_made(self, transport) -> None:  # noqa: D102
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        self.last_received = time.monotonic()
        try:
            self._handle(data)
        except Exception:
            log.exception("%s: handling %d byte packet", self.name, len(data))

    def error_received(self, exc) -> None:  # noqa: D102
        log.warning("%s: %s", self.name, exc)

    def _handle(self, r: bytes) -> None:
        if len(r) < CONTROL_SIZE:
            return
        length, ptype, seq, sentid, rcvdid = struct.unpack_from("<IHHII", r, 0)

        if len(r) == CONTROL_SIZE:
            if ptype == T_RETRANSMIT and length == CONTROL_SIZE:
                # The radio is missing one of ours and has asked by number.
                self._resend(seq)
                return
            if ptype == T_IAMHERE:
                self.remote_id = sentid
                self._areyouthere = False
                log.info("%s: radio answered, id 0x%08x", self.name, sentid)
                self.send_control(T_AREYOUREADY, tracked=False, seq=0x01)
                return
            if ptype == T_AREYOUREADY:
                self.remote_id = sentid
                self.on_ready()
                return
            if ptype == T_DISCONNECT:
                log.warning("%s: radio disconnected us", self.name)
                self.connected = False
                return
            return

        if ptype == T_RETRANSMIT and len(r) > CONTROL_SIZE:
            # Several missing at once: a list of 16-bit numbers after the
            # header (wfview's "variable length retransmit request"; the
            # earlier app sends them in pairs, so a number can come twice).
            # Until 18/10/2026 these fell through to on_packet and were
            # ignored, so a packet of transmit audio lost on the network
            # stayed lost while the radio waited for it.
            n = (len(r) - CONTROL_SIZE) // 2
            for want in dict.fromkeys(struct.unpack_from(f"<{n}H", r, CONTROL_SIZE)):
                self._resend(want)
            return

        if len(r) == PING_SIZE and ptype == T_PING:
            if r[0x10] == 0x01:
                # Our ping answered: the next one gets the next number.
                if seq == self.ping_seq:
                    self.ping_seq = (self.ping_seq + 1) & 0xFFFF
                return
            if r[0x10] == 0x00:
                # A ping from the radio; answer it or it concludes we are gone.
                p = self._header(PING_SIZE, T_PING, seq)
                p[0x10] = 0x01
                p[0x11:0x15] = r[0x11:0x15]
                self.send_raw(p)
            return

        self.on_packet(r, length, ptype, seq, sentid)

    def _resend(self, seq: int) -> None:
        """Send the radio a packet it asked for again, as it was sent.

        One no longer held is answered with an untracked idle carrying that
        number, as wfview and the earlier app do: the radio has its number
        and stops waiting for it, rather than holding the stream for a packet
        that is never coming.
        """
        held = self._tx_buf.get(seq)
        self.retransmits += 1
        if held:
            self.send_raw(held[1])
        else:
            self.retransmits_missing += 1
            log.debug("%s: asked for seq 0x%04x, no longer held", self.name, seq)
            self.send_control(T_IDLE, tracked=False, seq=seq)

    # -- for subclasses ----------------------------------------------------

    def on_ready(self) -> None:
        """The radio said "I am ready"."""

    def on_packet(self, r: bytes, length: int, ptype: int, seq: int, sentid: int) -> None:
        """Anything that is not stream bookkeeping."""


class ControlStream(_Stream):
    """Port 50001: log in, and find out where the CI-V stream lives."""

    def __init__(self, host: str, port: int, username: str, password: str, comp_name: str,
                 audio_rate: int = 0, audio_codec: int = CODEC_PCM_MONO_16,
                 tx_rate: int = 0):
        super().__init__(host, port, "control")
        self.username = username
        self.password = password
        self.comp_name = comp_name
        self.token = 0
        self.tok_request = 0
        self.authenticated = False
        self.radios: list[dict] = []
        self.current: dict | None = None
        self.civ_port = 0
        self.civ_local_port = 0
        # Zero means no audio was asked for, which is the default: the module
        # is a control surface first and audio costs the radio bandwidth it
        # need not spend.
        self.audio_rate = audio_rate if audio_rate in AUDIO_RATES else 0
        self.audio_codec = audio_codec
        # Zero means the radio is told not to expect any transmit audio, which
        # is the default and is a refusal of its own: a radio that has been
        # told a transmit stream is coming waits for one, and nothing above
        # this can key it by accident if it was never offered one. It is only
        # non-zero when something above has decided the module may transmit at
        # all - see TX_ENABLED and `allow_ptt` - so the four gates in
        # digi/transmit.py are joined here by the plainest one of the lot:
        # there is no transmit stream to send into.
        self.tx_rate = tx_rate if tx_rate in AUDIO_RATES else 0
        # ...and transmit audio needs the audio socket, which only exists when
        # receive audio was asked for. Asking for one direction without the
        # other is a shape this protocol allows and nothing here needs.
        if not self.audio_rate:
            self.tx_rate = 0
        self.audio_port = 0
        self.audio_local_port = 0
        self.on_audio_port: Callable[[int], None] | None = None
        self.stream_open = False
        self.error: str | None = None
        self.on_civ_port: Callable[[int], None] | None = None
        self._said_stale = False
        self._token_task: asyncio.Task | None = None

    def on_ready(self) -> None:
        self.send_login()

    async def stop(self) -> None:
        """Give the token back, and stop renewing it, before the socket goes.

        Two things were missing here and both leave the radio believing this
        client is still present. The renewal task is started when the login
        succeeds rather than when the stream starts, so it was never in
        `self._tasks` and the base class's stop never cancelled it: one task
        per connection attempt, awake once a minute, writing to a transport
        that had been closed - for the life of the process. And the token was
        never handed back, so the radio held the session it is the key to for
        about a minute after we left. That minute is the "refuses the next
        connection for about a minute" the registry, the settings form and the
        switch between radios all work around, and it is what makes a manual
        reconnect look like a wrong password.

        Order matters: the release goes out while we are still logged in and
        before the disconnect the base class sends, which is the order wfview
        uses and the order the app this one replaced used
        (`docs/prior-art/app/icomlan.py`, on_close).
        """
        task, self._token_task = self._token_task, None
        if task is not None:
            task.cancel()
        if self.authenticated and self.transport is not None:
            try:
                self.send_token(0x01)
            except Exception:
                log.exception("control: releasing the token")
        self.authenticated = False
        await super().stop()

    # -- outgoing auth packets --------------------------------------------

    def send_login(self) -> None:
        self.tok_request = random.getrandbits(16)
        p = self._header(LOGIN_SIZE, 0x00)
        struct.pack_into(">I", p, 0x10, LOGIN_SIZE - 0x10)  # payloadsize: big-endian
        p[0x14] = 0x01  # requestreply: a request
        p[0x15] = 0x00  # requesttype: login
        struct.pack_into(">H", p, 0x16, self.auth_seq)
        self.auth_seq += 1
        struct.pack_into("<H", p, 0x1A, self.tok_request)
        u, pw = passcode(self.username), passcode(self.password)
        p[0x40 : 0x40 + len(u)] = u
        p[0x50 : 0x50 + len(pw)] = pw
        name = self.comp_name.encode("latin-1", "replace")[:16]
        p[0x60 : 0x60 + len(name)] = name
        log.info("control: logging in as %r", self.username)
        self.send_tracked(p)

    def send_token(self, magic: int) -> None:
        """magic 0x02 confirms a token, 0x05 renews it, 0x01 gives it back.

        The three wfview sends, and the three this sends. The comment here
        used to say 0x01 renewed and 0x00 released, which was wrong in both
        halves and survived because nothing ever sent either of them: the
        renewal below has always sent 0x05, and there was no teardown to send
        a release at all. `docs/prior-art/app/icomlan.py` has the same three
        with the same meanings, against the same radio.
        """
        p = self._header(TOKEN_SIZE, 0x00)
        struct.pack_into(">I", p, 0x10, TOKEN_SIZE - 0x10)
        p[0x14] = 0x01
        p[0x15] = magic
        struct.pack_into(">H", p, 0x16, self.auth_seq)
        self.auth_seq += 1
        struct.pack_into("<H", p, 0x1A, self.tok_request)
        struct.pack_into("<I", p, 0x1C, self.token)
        struct.pack_into(">H", p, 0x24, 0x0798)  # resetcap, as wfview sends it
        self.send_tracked(p)

    def send_request_stream(self) -> None:
        """Ask for a CI-V stream, and tell the radio the port to send it to."""
        radio = self.current
        if radio is None:
            return
        # Audio needs a port of its own, taken the same way and for the same
        # reason: the radio has to be told where to send it before the socket
        # that receives it can exist.
        if self.audio_rate and not self.audio_local_port:
            s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s_.bind(("0.0.0.0", 0))
            self.audio_local_port = s_.getsockname()[1]
            s_.close()
        if not self.civ_local_port:
            # The radio has to be told a port before we can bind the socket
            # that will use it, so take one from the kernel and let go of it.
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("0.0.0.0", 0))
            self.civ_local_port = s.getsockname()[1]
            s.close()

        p = self._header(CONNINFO_SIZE, 0x00)
        struct.pack_into(">I", p, 0x10, CONNINFO_SIZE - 0x10)
        p[0x14] = 0x01
        p[0x15] = 0x03  # requesttype: open a stream
        struct.pack_into(">H", p, 0x16, self.auth_seq)
        self.auth_seq += 1
        struct.pack_into("<H", p, 0x1A, self.tok_request)
        struct.pack_into("<I", p, 0x1C, self.token)
        if radio["use_guid"]:
            p[0x20:0x30] = radio["guid"]
        else:
            struct.pack_into("<H", p, 0x27, 0x8010)
            p[0x2A:0x30] = radio["mac"]
        name = radio["name"].encode("latin-1", "replace")[:32]
        p[0x40 : 0x40 + len(name)] = name
        u = passcode(self.username)
        p[0x60 : 0x60 + len(u)] = u
        # Each direction is asked for only when something is going to use it.
        # A radio told to expect transmit audio waits for it, so this is not a
        # flag to set speculatively.
        want_audio = bool(self.audio_rate)
        # The radio's own word on whether it can be transmitted into. wfview
        # refuses transmit audio when this is below 2 and so does this; the
        # field is what the radio said it supports, and overriding it is how a
        # client ends up keying a radio that then modulates nothing.
        if self.tx_rate and radio.get("tx_sample", 0) < 2:
            log.warning("control: the radio reports no transmit audio "
                        "capability (txsample %d), so none will be sent",
                        radio.get("tx_sample", 0))
            self.tx_rate = 0
        want_tx = bool(want_audio and self.tx_rate)
        p[0x70] = 0x01 if want_audio else 0x00   # rxenable
        p[0x71] = 0x01 if want_tx else 0x00      # txenable
        p[0x72] = self.audio_codec if want_audio else 0x00
        p[0x73] = self.audio_codec if want_tx else 0x00   # txcodec
        # Plain rates, big-endian. The bitmask values (0x8000 and friends) in
        # this protocol are what a *radio* reports as its capabilities; a
        # client sends the rate it wants.
        struct.pack_into(">I", p, 0x74, self.audio_rate if want_audio else 0)
        struct.pack_into(">I", p, 0x78, self.tx_rate if want_tx else 0)
        struct.pack_into(">I", p, 0x7C, self.civ_local_port)
        struct.pack_into(">I", p, 0x80, self.audio_local_port if want_audio else 0)
        struct.pack_into(">I", p, 0x84, TX_BUFFER_MS if want_tx else 0)
        p[0x88] = 0x01
        if want_tx:
            log.warning("control: asking for CI-V on %d, %d Hz audio on %d, "
                        "and a %d Hz TRANSMIT stream - the radio will be able "
                        "to be modulated from here",
                        self.civ_local_port, self.audio_rate,
                        self.audio_local_port, self.tx_rate)
        elif want_audio:
            log.info("control: asking for CI-V on %d and %d Hz audio on %d",
                     self.civ_local_port, self.audio_rate, self.audio_local_port)
        else:
            log.info("control: asking for a CI-V stream to local port %d", self.civ_local_port)
        self.send_tracked(p)

    # -- incoming ----------------------------------------------------------

    def on_packet(self, r: bytes, length: int, ptype: int, seq: int, sentid: int) -> None:
        n = len(r)
        if n == TOKEN_SIZE:
            self._on_token(r, ptype)
        elif n == STATUS_SIZE:
            self._on_status(r, ptype)
        elif n == LOGIN_RESPONSE_SIZE:
            self._on_login_response(r, ptype)
        elif n == CONNINFO_SIZE:
            self._on_conninfo(r, ptype)
        elif n > CAPABILITIES_SIZE and (n - CAPABILITIES_SIZE) % RADIO_CAP_SIZE == 0:
            self._on_capabilities(r)

    def _on_login_response(self, r: bytes, ptype: int) -> None:
        if ptype == T_RETRANSMIT:
            return
        tok_request = struct.unpack_from("<H", r, 0x1A)[0]
        token = struct.unpack_from("<I", r, 0x1C)[0]
        error = struct.unpack_from("<I", r, 0x30)[0]
        conn = _cstr(r[0x40:0x50])
        if error == 0xFEFFFFFF:
            self.error = AUTH_REJECTED
            log.error("control: %s", self.error)
            return
        if self.authenticated:
            return
        if tok_request != self.tok_request:
            log.warning("control: token response was for someone else's request")
            return
        self.token = token
        self.authenticated = True
        self.error = None
        log.info("control: logged in (connection type %s)", conn or "unknown")
        self.send_token(0x02)
        if self._token_task is None:
            self._token_task = asyncio.create_task(self._renew_token())

    async def _renew_token(self) -> None:
        while True:
            try:
                await asyncio.sleep(TOKEN_RENEWAL)
                self.send_token(0x05)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("control: token renewal")

    def _on_token(self, r: bytes, ptype: int) -> None:
        if ptype == T_RETRANSMIT:
            return
        requestreply = r[0x14]
        requesttype = r[0x15]
        response = struct.unpack_from("<I", r, 0x30)[0]
        if requesttype == 0x05 and requestreply == 0x02:
            if response == 0x00000000:
                if not self.stream_open and self.current is not None:
                    self.send_request_stream()
            elif response == 0xFFFFFFFF:
                log.warning("control: token refused, logging in again")
                self.token = struct.unpack_from("<I", r, 0x1C)[0]
                self.tok_request = struct.unpack_from("<H", r, 0x1A)[0]
                self.stream_open = False
                self.send_request_stream()

    def _on_capabilities(self, r: bytes) -> None:
        self.radios = []
        for off in range(CAPABILITIES_SIZE, len(r), RADIO_CAP_SIZE):
            cap = r[off : off + RADIO_CAP_SIZE]
            commoncap = struct.unpack_from("<H", cap, 0x07)[0]
            radio = {
                "name": _cstr(cap[0x10:0x30]),
                "audio": _cstr(cap[0x30:0x50]),
                "civ": cap[0x52],
                "mac": bytes(cap[0x0A:0x10]),
                "guid": bytes(cap[0x00:0x10]),
                # 0x8010 means this radio is identified by MAC address; anything
                # else means it uses a GUID, and sending the wrong one gets the
                # stream request ignored rather than refused.
                "use_guid": commoncap != 0x8010,
                "baudrate": struct.unpack_from(">I", cap, 0x5A)[0],
                # What the radio says it will send and take. These are counts
                # of supported rates rather than rates, and the only thing
                # done with them is wfview's test: below 2 means this radio
                # has no audio in that direction. Unaligned on purpose - the
                # field really does start at an odd offset.
                "rx_sample": struct.unpack_from("<H", cap, 0x53)[0],
                "tx_sample": struct.unpack_from("<H", cap, 0x55)[0],
            }
            self.radios.append(radio)
            log.info(
                "control: radio %r, CI-V address 0x%02x, %s",
                radio["name"], radio["civ"],
                "GUID" if radio["use_guid"] else "MAC " + radio["mac"].hex(":"),
            )
        if len(self.radios) == 1 and self.current is None:
            self.current = self.radios[0]

    def _on_conninfo(self, r: bytes, ptype: int) -> None:
        if ptype == T_RETRANSMIT:
            return
        busy = struct.unpack_from("<I", r, 0x60)[0]
        computer = _cstr(r[0x64:0x74])
        ip = struct.unpack_from("<I", r, 0x84)[0]
        if self.stream_open:
            return
        if not self.radios:
            return
        if self.current is None:
            self.current = self.radios[0]
        if busy and computer == self.comp_name and not self._said_stale:
            # Ourselves, as far as the radio can tell: an earlier session
            # under this name that never logged out (a process killed rather
            # than stopped). The radio lets go of it on its own timer - 35
            # and 81 seconds the two times it was measured - and only then
            # answers the stream request; said here, once, so that wait has a
            # reason in the log.
            self._said_stale = True
            log.warning("control: the radio still holds an earlier session "
                        "from %s; it lets go of it by itself, usually within "
                        "a minute and a half", computer)
        if busy and computer and computer != self.comp_name:
            self.error = f"the radio is in use by {computer}"
            log.warning("control: %s", self.error)
            # An idle in reply, so the radio does not take our silence for a
            # dead client while somebody else has it.
            self.send_control(T_IDLE, tracked=False)
            return
        self.error = None
        self.send_request_stream()

    def _on_status(self, r: bytes, ptype: int) -> None:
        if ptype == T_RETRANSMIT:
            return
        error = struct.unpack_from("<I", r, 0x30)[0]
        disc = r[0x40]
        if error == 0xFFFFFFFF and not self.stream_open:
            self.error = "the radio refused the connection - it may need a power cycle"
            log.error("control: %s", self.error)
            return
        if error == 0x00000000 and disc == 0x01:
            log.warning("control: radio closed the stream")
            self.stream_open = False
            return
        # civport and audioport are the two fields in this protocol that are
        # sent big-endian, and reading them as little-endian yields a plausible
        # wrong port rather than an obvious error.
        civ_port = struct.unpack_from(">H", r, 0x42)[0]
        audio_port = struct.unpack_from(">H", r, 0x46)[0]
        if civ_port and not self.stream_open:
            self.civ_port = civ_port
            self.stream_open = True
            self.error = None
            log.info("control: CI-V stream is on port %d", civ_port)
            if self.on_civ_port:
                self.on_civ_port(civ_port)
            # The radio answers with an audio port whether or not one was
            # asked for, so it is only acted on when audio was requested.
            if audio_port and self.audio_rate:
                self.audio_port = audio_port
                log.info("control: audio stream is on port %d", audio_port)
                if self.on_audio_port:
                    self.on_audio_port(audio_port)


class AudioStream(_Stream):
    """Port 50003: the audio, as PCM, in both directions.

    The same stream machinery as the others - its own are-you-there, its own
    ping, its own watchdog - because on this protocol each port is a separate
    conversation with its own sequence numbers and its own idea of whether it
    is alive.

    What is different is that there is nothing to open. The CI-V stream has to
    ask for the stream to be opened; audio simply flows once the control
    stream's conninfo said where to send it, in which directions and at what
    rates.

    **A packet is audio if it is not a control packet.** wfview's test is
    `type != 0x01 && len >= 0x20`, and the payload is everything from byte
    0x18 - the 24-byte header is fixed and the data runs to the end of the
    datagram. The `datalen` field in the header is big-endian here, while the
    same field on a CI-V packet is not; it is not used for anything, and this
    note is here so nobody has to find that out twice.

    **The transmit half is the `TxAudioSink` in `digi/transmit.py`.** That file
    wrote down what it needed before anything could provide it - `tx_rate`,
    `tx_audio_ready`, `send_audio`, and the two below that say when a
    transmission has ended - and this is the thing that provides it over the
    LAN. It is deliberately not a buffer: what is handed to `send_audio` goes
    out of the socket immediately, because the caller is pacing against the
    period boundary and audio held here is audio that arrives late. Late is the
    one thing a slotted mode cannot tolerate.

    A transmit packet is the same 0x18-byte header with the *ident* field set
    to 0x0080, its own sequence number at 0x12 counted separately from the
    outer one, and the sample count in bytes at 0x16 - both big-endian, unlike
    their neighbours. The layout is wfview's `audio_packet`; the commented-out
    fragment beside it in `packettypes.h` is the only place the transmit
    direction is written down at all.
    """

    def __init__(self, host: str, port: int, local_port: int,
                 on_audio: Callable[[bytes], None], tx_rate: int = 0):
        super().__init__(host, port, "audio")
        self._bind_port = local_port
        self.on_audio = on_audio
        self.packets = 0
        self.bytes = 0
        self.dropped = 0
        self._last_seq: int | None = None
        #: The rate the radio agreed to be transmitted into at, or zero. Zero
        #: is the default and means this stream will not send a sample even if
        #: it is asked to: the radio was never told to expect any.
        self.tx_rate = tx_rate if tx_rate in AUDIO_RATES else 0
        self.tx_drain_s = TX_DRAIN_S
        self.tx_seq = 0
        self.tx_packets = 0
        self.tx_bytes = 0
        #: Pieces of transmit audio that came later than the radio's buffer
        #: could cover, over this stream's life, and the current (or last)
        #: over's own account; see _account.
        self.tx_underruns = 0
        self.tx_over: dict | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=("0.0.0.0", self._bind_port),
            remote_addr=(self.host, self.port),
        )
        sock = self.transport.get_extra_info("socket")
        local_ip, self.local_port = sock.getsockname()[:2]
        addr = struct.unpack(">I", socket.inet_aton(local_ip))[0]
        self.my_id = ((addr >> 8 & 0xFF) << 24) | ((addr & 0xFF) << 16) | (self.local_port & 0xFFFF)
        self.last_received = time.monotonic()
        log.info("audio stream: %s:%d from local port %d", self.host, self.port, self.local_port)
        self.send_control(T_AREYOUTHERE, tracked=False)
        # No watchdog here. The CI-V stream has one because its open request is
        # the packet the radio may ignore, and the symptom is a connection that
        # looks established and never delivers a frame. Audio has nothing to
        # open - it simply arrives once the conninfo said where to send it - so
        # there is nothing for a watchdog to re-ask for. Copying CivStream's
        # task list wholesale is exactly what put one here, and it crashed the
        # stream on startup while the packets kept arriving anyway.
        self._tasks = [
            asyncio.create_task(self._keepalive()),
            asyncio.create_task(self._purge()),
        ]

    def on_packet(self, r: bytes, length: int, ptype: int, seq: int, sentid: int) -> None:
        # Control traffic on this port is handled by the base class; anything
        # else of a reasonable size is audio.
        if ptype == 0x01 or len(r) < 0x20:
            return
        payload = r[0x18:]
        if not payload:
            return
        self.packets += 1
        self.bytes += len(payload)
        # Sequence gaps are counted but nothing is asked for again. A
        # retransmitted packet of audio arrives too late to be worth putting
        # back in the stream, and a decoder handles a gap far better than it
        # handles samples out of order.
        if self._last_seq is not None:
            expected = (self._last_seq + 1) & 0xFFFF
            if seq != expected:
                self.dropped += (seq - expected) & 0xFFFF
        self._last_seq = seq
        try:
            self.on_audio(payload)
        except Exception:
            log.exception("audio listener")

    # -- the transmit half: the TxAudioSink in digi/transmit.py -------------

    @property
    def tx_audio_ready(self) -> bool:
        """Whether there is really a transmit stream, not whether one was asked for.

        Three things, and the third is the one worth having: the socket exists,
        the radio has answered on this port, and audio has actually arrived
        from it. Receive and transmit are the same socket and the same agreed
        session, so audio coming out of it is the only evidence available here
        that audio put into it goes anywhere. Without that test the module
        would arm, key the radio, and modulate nothing - which is the failure
        the operator is least able to see and most able to annoy a band with.
        """
        return bool(self.tx_rate and self.transport is not None
                    and self.remote_id and self.packets)

    def send_audio(self, pcm: bytes) -> bool:
        """Hand 16-bit signed mono samples at `tx_rate` to the radio.

        Sent, not queued. One call becomes one or more datagrams on the wire
        before it returns, and nothing here waits for anything: pacing belongs
        to the caller, which is the only thing that knows where the period
        boundary is.

        False rather than an exception for the ordinary refusals, because the
        caller's response to all of them is the same - stop and unkey - and it
        is already holding a keyed transmitter while it asks.
        """
        if not self.tx_audio_ready:
            return False
        if len(pcm) % 2:
            # Half a sample makes every sample after it noise, and the radio
            # has no way to tell. Refusing loses a transmission; taking it
            # transmits a burst of white noise on somebody else's band.
            log.error("audio: refusing %d bytes of transmit audio - "
                      "16-bit samples come in pairs", len(pcm))
            return False
        self._account(len(pcm))
        for off in range(0, len(pcm), TX_PACKET_BYTES):
            chunk = pcm[off:off + TX_PACKET_BYTES]
            p = self._header(AUDIO_HEADER_SIZE, 0x00)
            struct.pack_into("<I", p, 0x00, AUDIO_HEADER_SIZE + len(chunk))
            struct.pack_into("<H", p, 0x10, 0x0080)      # ident: this is transmit audio
            struct.pack_into(">H", p, 0x12, self.tx_seq)  # big-endian, unlike the outer seq
            struct.pack_into(">H", p, 0x16, len(chunk))   # and so is this
            self.tx_seq = (self.tx_seq + 1) & 0xFFFF
            self.tx_packets += 1
            self.tx_bytes += len(chunk)
            # Tracked, like everything else the radio can ask for again. A
            # retransmitted packet of audio is usually too late to be worth
            # having - but the radio asks by number, and a number it is
            # refused is a gap it may wait for rather than play through.
            self.send_tracked(p + chunk)
        return True

    def tx_end(self) -> None:
        """The last sample of a transmission has been handed over.

        See TX_TAIL_MS: the radio is still playing what it has, so it is given
        silence to finish on rather than an underrun, and the caller holds PTT
        for `tx_drain_s` afterwards. Called only when a transmission ran to its
        end - a transmission that was stopped wants the key dropped now, and
        flushing a buffer nobody is going to transmit is the opposite of that.
        """
        if not self.tx_audio_ready:
            return
        self.send_audio(b"\x00" * (int(self.tx_rate * TX_TAIL_MS / 1000) * 2))
        over = self.tx_over
        if over is not None and not over["ended"]:
            over["ended"] = True
            over["retransmits"] = self.retransmits - over.pop("retransmits_before")
            bad = over["underruns"] or over["retransmits"]
            log.log(logging.WARNING if bad else logging.INFO,
                    "audio: transmitted %.2f s of audio; %d piece(s) later than the radio's %d ms buffer "
                    "covers (worst %d ms), %d packet(s) asked for again",
                    over["sent_s"], over["underruns"], TX_BUFFER_MS, over["late_ms"], over["retransmits"])

    def _account(self, nbytes: int) -> None:
        """Keep the over's books: is this piece later than the radio can wait?

        The sender cannot see the radio's buffer, but it knows when the first
        piece of the over went and how much audio has gone since. The radio
        holds TX_BUFFER_MS before it plays, so a piece arriving after
        start + TX_BUFFER_MS + audio already sent is a piece the radio wanted
        and did not have. That is an underrun, a hole on the air. It is
        counted, the first in an over is logged at once, and tx_end logs the
        over's totals, so a run against a real radio says whether the sender
        kept up without anybody having to demodulate the capture.
        """
        now = time.monotonic()
        over = self.tx_over
        if over is None or over["ended"] or now - over["last"] > 1.0:
            over = self.tx_over = {"start": now, "last": now, "sent_s": 0.0, "underruns": 0, "late_ms": 0,
                                   "ended": False, "retransmits_before": self.retransmits}
        else:
            behind = now - (over["start"] + TX_BUFFER_MS / 1000 + over["sent_s"])
            if behind > 0.002:
                over["underruns"] += 1
                self.tx_underruns += 1
                over["late_ms"] = max(over["late_ms"], round(behind * 1000))
                # The radio starts again from this piece, so the next one is
                # measured from here rather than counted late as well.
                over["start"] += behind
                if over["underruns"] == 1:
                    log.warning("audio: transmit audio %d ms later than the radio's %d ms buffer covers, "
                                "%.2f s into the over: the radio ran dry and transmitted a hole",
                                round(behind * 1000), TX_BUFFER_MS, over["sent_s"])
        over["last"] = now
        over["sent_s"] += nbytes / 2 / self.tx_rate

    def tx_stats(self) -> dict:
        over = self.tx_over or {}
        return {"rate": self.tx_rate or None, "ready": self.tx_audio_ready,
                "packets": self.tx_packets, "bytes": self.tx_bytes,
                "underruns": self.tx_underruns, "retransmits": self.retransmits,
                "last_over": {k: over[k] for k in ("sent_s", "underruns", "late_ms") if k in over}}


class CivStream(_Stream):
    """Port 50002 (or wherever the control stream said): CI-V frames, wrapped."""

    def __init__(self, host: str, port: int, local_port: int, on_civ: Callable[[bytes], None]):
        super().__init__(host, port, "civ")
        self._bind_port = local_port
        self.on_civ = on_civ
        self.opened = False
        self._seen_data = False
        # Separate from last_received on purpose. The radio pings and idles
        # constantly on this port - 95 pings and 258 idles in eight seconds -
        # so last_received is never stale and a watchdog watching it never
        # fires. What matters is whether any *data* has arrived.
        self.last_data = 0.0
        #: Frames in and out, so a caller can tell "the link is up but the
        #: radio is saying nothing" from "nothing has been asked".
        self.frames = 0
        self.sent = 0
        self._buf = b""
        self._open_task: asyncio.Task | None = None
        #: When the first open request of this stream went out, and how many
        #: have gone since. Both are for `wedged`, and the count also keeps
        #: the log to a line every ten seconds rather than two a second.
        self._open_since = 0.0
        self._opens = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=("0.0.0.0", self._bind_port),
            remote_addr=(self.host, self.port),
        )
        # Bound to 0.0.0.0 but connected to the radio, so the kernel has
        # already picked the source address by route and getsockname reports
        # it - which is the address the radio will see and the one our id has
        # to be built from.
        sock = self.transport.get_extra_info("socket")
        local_ip, self.local_port = sock.getsockname()[:2]
        addr = struct.unpack(">I", socket.inet_aton(local_ip))[0]
        self.my_id = ((addr >> 8 & 0xFF) << 24) | ((addr & 0xFF) << 16) | (self.local_port & 0xFFFF)
        self.last_received = time.monotonic()
        self.last_data = time.monotonic()
        log.info("civ stream: %s:%d from local port %d", self.host, self.port, self.local_port)
        self.send_control(T_AREYOUTHERE, tracked=False)
        self._tasks = [
            asyncio.create_task(self._keepalive()),
            asyncio.create_task(self._purge()),
            asyncio.create_task(self._watchdog()),
        ]

    @property
    def wedged(self) -> bool:
        """Asked to open, accepted, and never sent a single frame.

        Seen on Brian's IC-9700 on 10/10/2026, twice in a row: a module was
        SIGKILLed without logging out, its successor logged in, the radio
        handed out the CI-V and audio ports once the stale session had timed
        out, audio flowed - and the CI-V stream delivered nothing for as long
        as anyone waited (minutes), through hundreds of open requests. Pings
        and idles kept arriving, so nothing that watches `last_received`
        could see it. A fresh login, after a clean logout, got data at once.

        So this is the one condition the open-request watchdog cannot fix by
        asking again, and the answer is to log out and in again (rig.py's
        supervisor does that). It is not standby: a 9700 in standby answers
        every frame with NG, which is data.
        """
        return bool(not self._seen_data and self._open_since
                    and time.monotonic() - self._open_since > CIV_WEDGED_S)

    async def stop(self) -> None:
        # A close goes out for a stream we asked to open even if it never
        # delivered: the radio accepted the open, so it may well think the
        # stream is open, and a wedged stream is the one most worth closing.
        if (self.opened or self._open_since) and self.transport is not None:
            try:
                self.send_openclose(close=True)
            except Exception:
                pass
        await super().stop()

    def on_ready(self) -> None:
        self.send_openclose(close=False)

    async def _watchdog(self) -> None:
        """Ask again for CI-V data until some arrives.

        The open request is the one packet the radio will silently ignore if it
        arrives at the wrong moment in its own startup, and the symptom is a
        connection that looks established and never delivers a frame. wfview
        re-sends it on a two second timer for exactly this reason.

        It must watch the time of the last *data*, not the last packet of any
        kind. Measured on a real IC-9700: in eight seconds of a stream that
        delivered nothing, the radio sent 95 pings and 258 idles - so a
        watchdog keyed on last_received never fired once, and the stream sat
        open and empty indefinitely while a hundred perfectly good CI-V reads
        went out into it.
        """
        while True:
            try:
                await asyncio.sleep(0.5)
                if self.remote_id and time.monotonic() - self.last_data > 2.0:
                    self.send_openclose(close=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("civ: watchdog")

    def send_openclose(self, close: bool) -> None:
        if not close and not self._seen_data:
            if not self._open_since:
                self._open_since = time.monotonic()
            if self._opens % 20 == 0:
                log.info("civ: opening the data stream%s",
                         f" (asked {self._opens} times)" if self._opens else "")
            self._opens += 1
        elif close:
            log.info("civ: closing the data stream")
        p = self._header(OPENCLOSE_SIZE, 0x00)
        struct.pack_into("<H", p, 0x10, 0x01C0)
        struct.pack_into(">H", p, 0x13, self.inner_seq)  # big-endian, unlike the outer seq
        p[0x15] = 0x00 if close else 0x04
        self.inner_seq = (self.inner_seq + 1) & 0xFFFF
        self.send_tracked(p)

    def send_civ(self, data: bytes) -> None:
        """Wrap one CI-V frame and send it.

        The header is PING_SIZE long and the frame follows it, so the datagram
        is PING_SIZE + len(data) - but the *header* is only PING_SIZE bytes and
        must be built at that size. Building it at the full datagram length and
        then appending the frame is the mistake that made this module send 109
        perfectly well-formed CI-V reads that the radio never answered: the
        length field said 0x15 + n, the datalen field said n, and the n bytes at
        offset 0x15 where the radio reads the frame were all zero, because the
        real frame was sitting n bytes further on, past the end of the packet
        the header described. A frame of NULs is not a frame, so the radio
        discarded every one of them in silence. wfview builds the header at
        sizeof(data_packet) and appends; so does this now.
        """
        p = self._header(PING_SIZE, 0x00)
        struct.pack_into("<I", p, 0x00, PING_SIZE + len(data))
        p[0x10] = 0xC1
        struct.pack_into("<H", p, 0x11, len(data))
        struct.pack_into(">H", p, 0x13, self.inner_seq)  # big-endian
        self.inner_seq = (self.inner_seq + 1) & 0xFFFF
        self.sent += 1
        self.send_tracked(p + data)

    def on_packet(self, r: bytes, length: int, ptype: int, seq: int, sentid: int) -> None:
        if ptype == T_RETRANSMIT or len(r) <= PING_SIZE:
            return
        datalen = struct.unpack_from("<H", r, 0x11)[0]
        if datalen + PING_SIZE != length:
            return
        # A packet the radio sent twice must not be handed on twice: CI-V is
        # not idempotent at this layer and a repeated meter reading is harmless
        # but a repeated transceive frame is not.
        if seq in self._seen:
            return
        self._seen.add(seq)
        self.opened = True
        # The watchdog below keys on this, and nothing else sets it. Leaving it
        # at the time the stream started means the watchdog re-sends the open
        # request every two seconds for as long as the module runs, even while
        # data is arriving perfectly well - work the radio has to answer and
        # noise in the log that hides a stream which really has gone quiet.
        self.last_data = time.monotonic()
        self._seen_data = True
        self.frames += 1
        payload = r[PING_SIZE : PING_SIZE + datalen]
        self.on_civ(payload)
