"""A pretend IC-9700 on the loopback address, speaking Icom's LAN protocol.

    from shack_rig.icom.fake import FakeIcom
    radio = FakeIcom(); await radio.start()      # radio.control_port, ...

Moved from OpenHamShack Icom's `tools/fakeicom.py` into the package, so an app
adopting shack_rig can run its own tests against the same radio this
library's tests use. It binds 127.0.0.1 only and never sends anywhere else.

There is a real IC-9700 on this network. It accepts one client at a time, it
is connected to an antenna, and a second client wedges it for a minute - so
nothing that tests the receive audio or the scope end to end may go near it.
This stands in for it: the three UDP conversations (control, CI-V, audio), the
are-you-there / are-you-ready handshake on each, the login, the capabilities
and conninfo packets, the status packet that hands out the CI-V and audio
ports, CI-V answers to the poll loop, receive audio at the rate the client
asked for, and the spectrum as LAN wave-data frames for as long as the scope's
CI-V output is switched on.

It is written from the *radio's* side of the same layouts `lan.py`
uses from the client's - which are wfview's - and it deliberately does not
import the client's packet builders, so a mistake in one is not silently
shared by the other. What it cannot be is the radio's firmware. Things it
does not model, and so things a test against it does not prove:

- retransmit requests (it never loses a packet, and never asks for one);
- the radio's own timing quirks - the CI-V open request the real radio
  ignores during its own start-up, a token that expires, a busy radio -
  except the one `wedge_civ_sessions` stands in for: a CI-V stream accepted
  after an unclean logout that then never answers;
- real audio and real spectra: the audio is a sample counter, the
  spectrum a flat floor with a peak that moves each sweep;
- whether the real radio couples the preamp and the attenuator (one
  switching the other off). The guide does not say, so here they are
  independent, each remembered per band as the manual does say.

What it records, so a test can ask: every CI-V command it was sent, every
conninfo request (and so whether transmit audio was asked for), and every
audio packet that arrived *from* the client - which must be none at all for a
receive-only client - and, on the audio port, every packet that took a place
in the client's sequence, so `tx_playout` can say where the radio would have
transmitted holes (a model of the 18/10/2026 capture, not the firmware).
"""

from __future__ import annotations

import asyncio
import random
import struct
import time

# Packet types, the radio's side of the same table icomlan.py has.
T_IDLE, T_RETRANSMIT, T_AREYOUTHERE, T_IAMHERE = 0x00, 0x01, 0x03, 0x04
T_DISCONNECT, T_AREYOUREADY, T_PING = 0x05, 0x06, 0x07

RADIO_CIV = 0xA2
CTRL_CIV = 0xE0
SPECTRUM_POINTS = 475
SPECTRUM_MAX = 160


def _bcd_freq(hz: int) -> bytes:
    """Five bytes, least significant pair first - CI-V's frequency format."""
    digits = f"{hz:010d}"
    out = bytearray()
    for i in range(10, 0, -2):
        out.append((int(digits[i - 2]) << 4) | int(digits[i - 1]))
    return bytes(out)


def _bcd_meter(n: int) -> bytes:
    s = f"{n:04d}"
    return bytes([(int(s[0]) << 4) | int(s[1]), (int(s[2]) << 4) | int(s[3])])


class _Port(asyncio.DatagramProtocol):
    """One of the radio's three UDP conversations: handshake and keepalive."""

    def __init__(self, radio: "FakeIcom", name: str):
        self.radio = radio
        self.name = name
        self.my_id = random.getrandbits(32)
        self.client_id = 0
        self.client: tuple | None = None
        self.transport = None
        self.seq = 0
        self.ready = False
        self.disconnected = False

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    def connection_made(self, transport) -> None:
        self.transport = transport

    def header(self, length: int, ptype: int, seq: int | None = None) -> bytearray:
        p = bytearray(length)
        if seq is None:
            seq = self.seq
            self.seq = (self.seq + 1) & 0xFFFF
        struct.pack_into("<IHHII", p, 0, length, ptype, seq, self.my_id, self.client_id)
        return p

    def send(self, data: bytes) -> None:
        if self.transport is not None and self.client is not None:
            self.transport.sendto(bytes(data), self.client)

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 0x10:
            return
        length, ptype, seq, sentid, _ = struct.unpack_from("<IHHII", data, 0)
        self.client = addr
        if len(data) == 0x10:
            if ptype == T_AREYOUTHERE:
                self.client_id = sentid
                self.send(self.header(0x10, T_IAMHERE, seq=0))
            elif ptype == T_AREYOUREADY:
                self.client_id = sentid
                self.send(self.header(0x10, T_AREYOUREADY, seq=1))
                self.ready = True
                self.on_ready()
            elif ptype == T_DISCONNECT:
                self.disconnected = True
                self.ready = False
                self.on_disconnect()
            return
        if len(data) == 0x15 and ptype == T_PING:
            if data[0x10] == 0x00:
                p = self.header(0x15, T_PING, seq=seq)
                p[0x10] = 0x01
                p[0x11:0x15] = data[0x11:0x15]
                self.send(p)
            return
        self.on_packet(data)

    def on_ready(self) -> None:
        pass

    def on_disconnect(self) -> None:
        pass

    def on_packet(self, data: bytes) -> None:
        pass


class _Control(_Port):
    def __init__(self, radio):
        super().__init__(radio, "control")
        self.tok_request = 0
        self.token = random.getrandbits(32)

    def on_disconnect(self) -> None:
        self.radio.logouts += 1

    def _payload_header(self, length: int, reply: int, rtype: int) -> bytearray:
        p = self.header(length, 0x00)
        struct.pack_into(">I", p, 0x10, length - 0x10)
        p[0x14] = reply
        p[0x15] = rtype
        struct.pack_into("<H", p, 0x1A, self.tok_request)
        struct.pack_into("<I", p, 0x1C, self.token)
        return p

    def on_packet(self, data: bytes) -> None:
        n = len(data)
        if n == 0x80:                                    # login
            self.tok_request = struct.unpack_from("<H", data, 0x1A)[0]
            r = self._payload_header(0x60, 0x02, 0x00)
            ok = self.radio.accepts(data[0x40:0x50], data[0x50:0x60])
            struct.pack_into("<I", r, 0x30, 0 if ok else 0xFEFFFFFF)
            r[0x40:0x44] = b"FTTH"
            self.send(r)
        elif n == 0x40:                                  # token
            magic = data[0x15]
            self.radio.tokens.append(magic)
            if magic == 0x02:
                self._send_capabilities()
                self._send_conninfo()
            elif magic == 0x05:
                r = self._payload_header(0x40, 0x02, 0x05)
                self.send(r)
        elif n == 0x90 and data[0x15] == 0x03:           # open my streams
            req = {
                "rx_enable": data[0x70], "tx_enable": data[0x71],
                "rx_codec": data[0x72], "tx_codec": data[0x73],
                "rx_rate": struct.unpack_from(">I", data, 0x74)[0],
                "tx_rate": struct.unpack_from(">I", data, 0x78)[0],
                "civ_port": struct.unpack_from(">I", data, 0x7C)[0],
                "audio_port": struct.unpack_from(">I", data, 0x80)[0],
                "tx_buffer": struct.unpack_from(">I", data, 0x84)[0],
            }
            self.radio.stream_requests.append(req)
            r = self._payload_header(0x50, 0x02, 0x03)
            struct.pack_into(">H", r, 0x42, self.radio.civ.port)
            struct.pack_into(">H", r, 0x46, self.radio.audio.port)
            self.send(r)

    def _send_capabilities(self) -> None:
        cap = bytearray(0x66)
        struct.pack_into("<H", cap, 0x07, 0x8010)        # identified by MAC
        cap[0x0A:0x10] = bytes.fromhex("0090c7123456")
        name = b"IC-9700"
        cap[0x10:0x10 + len(name)] = name
        cap[0x30:0x30 + 9] = b"ICOM_VAUDIO"[:9]
        cap[0x52] = RADIO_CIV
        struct.pack_into("<H", cap, 0x53, 0x3F)          # rx sample rates offered
        struct.pack_into("<H", cap, 0x55, 0x3F)          # tx sample rates offered
        struct.pack_into(">I", cap, 0x5A, 115200)
        p = self.header(0x42 + 0x66, 0x00)
        p[0x42:] = cap
        self.send(p)

    def _send_conninfo(self) -> None:
        p = self._payload_header(0x90, 0x00, 0x00)
        struct.pack_into("<I", p, 0x60, 0)               # not busy
        self.send(p)


class _Civ(_Port):
    def __init__(self, radio):
        super().__init__(radio, "civ")
        self.inner = 0
        self.open = False
        self.wedged = False

    def on_ready(self) -> None:
        # Each are-you-ready is a new CI-V session. A wedged one answers the
        # handshake, the pings and the idles - all of that is _Port's - and
        # nothing else, which is what Brian's 9700 did (lan.CivStream.wedged).
        self.radio.civ_sessions += 1
        self.open = False
        self.wedged = self.radio.wedge_civ_sessions > 0
        if self.wedged:
            self.radio.wedge_civ_sessions -= 1

    def on_packet(self, data: bytes) -> None:
        if self.wedged:
            return
        if len(data) == 0x16:
            self.open = data[0x15] == 0x04
            return
        if len(data) <= 0x15 or data[0x10] != 0xC1:
            return
        datalen = struct.unpack_from("<H", data, 0x11)[0]
        self.radio.civ_in(bytes(data[0x15:0x15 + datalen]))

    def send_frame(self, payload: bytes, to: int = CTRL_CIV) -> None:
        frame = bytes([0xFE, 0xFE, to, RADIO_CIV]) + payload + bytes([0xFD])
        p = self.header(0x15, 0x00)
        struct.pack_into("<I", p, 0, 0x15 + len(frame))
        p[0x10] = 0xC1
        struct.pack_into("<H", p, 0x11, len(frame))
        struct.pack_into(">H", p, 0x13, self.inner)
        self.inner = (self.inner + 1) & 0xFFFF
        self.send(p + frame)


class _Audio(_Port):
    def __init__(self, radio):
        super().__init__(radio, "audio")

    def on_ready(self) -> None:
        self.radio.audio_ready()

    def on_disconnect(self) -> None:
        if self.radio._player is not None:
            self.radio._player.cancel()
            self.radio._player = None

    def datagram_received(self, data: bytes, addr) -> None:
        # Every packet that takes a place in the client's sequence - audio
        # and its idles alike - is noted with its sequence number and when it
        # came, before the base class sorts bookkeeping from audio: the
        # playout model (FakeIcom.tx_playout) plays them in that order.
        if len(data) >= 0x10:
            length, ptype, seq = struct.unpack_from("<IHH", data, 0)
            if ptype == T_IDLE and len(data) != 0x15:
                audio = len(data) >= 0x18 and struct.unpack_from("<H", data, 0x10)[0] in (0x0080, 0x9781)
                self.radio.tx_stream.append((time.monotonic(), seq, len(data) - 0x18 if audio else None))
        super().datagram_received(data, addr)

    def on_packet(self, data: bytes) -> None:
        # Anything here that is not stream bookkeeping is audio *from* the
        # client - transmit audio. A receive-only client sends none.
        if len(data) >= 0x18:
            self.radio.tx_audio_packets += 1
            self.radio.tx_audio_in(bytes(data[0x18:]))

    def send_pcm(self, pcm: bytes) -> None:
        p = self.header(0x18, 0x00)
        struct.pack_into("<I", p, 0, 0x18 + len(pcm))
        struct.pack_into("<H", p, 0x10, 0x9781)          # ident, as a 9700 sends
        struct.pack_into(">H", p, 0x16, len(pcm))
        self.send(p + pcm)


class FakeIcom:
    """The pretend radio. `await start()`, point the module at `control_port`."""

    #: How much audio one packet carries, as the real radio does it.
    PACKET_MS = 20
    #: Sweeps a second, roughly a real 9700's.
    SWEEPS_PER_S = 10
    #: The 9700's three bands, for per-band memory (its coverage, models.py).
    BANDS = ((144_000_000, 148_000_000), (430_000_000, 450_000_000),
             (1_240_000_000, 1_300_000_000))
    PREAMP_VALUES = (0x00, 0x01, 0x02, 0x03)
    ATT_VALUES = (0x00, 0x10)

    def __init__(self, username: str = "user", password: str = "pass",
                 freq: int = 144_174_000, powered: bool = True):
        self.username = username
        self.password = password
        self.freq = freq
        self.mode = 0x01                                 # USB
        self.data = 0x01
        #: CI-V Transceive (the radio volunteers dial and mode changes,
        #: `turn_dial`); on, as the radio ships.
        self.transceive = True
        self.filter = 0x01
        #: Whether the pretend transmitter is keyed, and every key/unkey it
        #: was sent in order (True/False), so a test can prove the library
        #: unkeyed - or never keyed at all.
        self.ptt = False
        self.ptt_commands: list[bool] = []
        #: Whether the pretend radio is on. Off is standby as a real 9700
        #: does it with network power-on enabled: the LAN side logs in, pings
        #: and opens the CI-V stream, and every CI-V frame is answered with a
        #: bare NG (FA) - measured on Brian's 9700, 10/10/2026 - except 18 01,
        #: which wakes it. Every power command, in order.
        self.powered = powered
        self.power_commands: list[bool] = []
        #: The receive front end, per band as the radio keeps it ("Each band
        #: memorizes the Preamplifier setting", Basic Manual p. 4-1): band
        #: start -> [16 02 byte, 11 byte]. The preamp byte is the 9700's,
        #: internal in bit 0 and external in bit 1 (CI-V guide p. 5); the
        #: attenuator byte is BCD dB, 00 or 10 (p. 4). Anything else is NG.
        self.front_end: dict[int, list[int]] = {lo: [0x00, 0x00] for lo, _ in self.BANDS}
        #: SET > Connectors > External P.AMP per band, 1A 05 0093/0094/0095,
        #: keyed by the item number as decimal. Off as the radio ships. A test
        #: switches one on here, as the operator would in the menu. Every
        #: 1A 05 write that arrives is recorded in `menu_writes` and changes
        #: nothing: the library must never send one (it is the mast-head
        #: preamp's DC supply), and a test asserts the list stays empty.
        self.ext_preamp_menu: dict[int, int] = {93: 0x00, 94: 0x00, 95: 0x00}
        self.menu_writes: list[bytes] = []
        #: The CW pitch as 14 09 answers it, 0-255 (128 is 600 Hz, as the
        #: radio ships). A test turns it as the operator would the knob.
        self.cw_pitch_level = 128
        #: What the receive audio carries: None for the counter (see _play),
        #: or a callable (rate, samples) -> that many signed 16-bit samples
        #: as bytes, called once per 20 ms packet in order - a test's CW
        #: signal, say. Read at every packet, so it can be swapped mid-stream.
        self.audio_source = None
        self.civ_commands: list[bytes] = []
        self.stream_requests: list[dict] = []
        self.tokens: list[int] = []
        #: How many of the next CI-V sessions are wedged: accepted, pinged,
        #: and never answered (see _Civ.on_ready). A test sets it; each
        #: session that starts wedged counts it down.
        self.wedge_civ_sessions = 0
        self.civ_sessions = 0
        #: Disconnects received on the control port - a client logging out.
        self.logouts = 0
        self.tx_audio_packets = 0
        #: Transmit audio as it arrived (16-bit PCM at the agreed rate), and
        #: how many bytes of it arrived while the pretend transmitter was not
        #: keyed - which a library that keys first and unkeys last never
        #: sends. `tx_log` is (time.monotonic(), keyed, bytes) per packet, for
        #: a test that wants to see audio and PTT on one time line.
        self.tx_pcm = bytearray()
        self.tx_pcm_unkeyed = 0
        self.tx_log: list[tuple[float, bool, int]] = []
        #: Every packet on the audio port that took a place in the client's
        #: sequence: (time.monotonic(), sequence number, bytes of audio), the
        #: bytes None for one that carried none (an idle). For tx_playout.
        self.tx_stream: list[tuple[float, int, int | None]] = []
        #: The squelch as 15 01 answers it; a test opens it as a signal would.
        self.squelch_open = False
        #: Duplex (0F 1x: 0x10 simplex, 0x11 DUP-, 0x12 DUP+) and the tone
        #: functions (16 42/43/4B), as a radio left on a repeater channel has
        #: them; a packet link must leave them simplex and off. A bare 0F is
        #: answered as the guide says: the shift while there is one, else
        #: the split state. Every 0F write is in `duplex_writes`.
        self.duplex = 0x12
        self.split = False
        self.duplex_writes: list[int] = []
        self.tone_functions = {0x42: 0x01, 0x43: 0x00, 0x4B: 0x00}
        #: The rest of the 16 xx switches a data set-up turns off (CI-V
        #: guide p. 5), as Brian's radio had them on 18/10/2026: NR on - the
        #: setting that broke 2 m AFSK receive. A test turns others on as an
        #: operator would. A sub-command in `missing_functions` is answered
        #: NG, as a radio without that switch answers it. Every write, in
        #: order, is (sub, value) in `function_writes`.
        self.functions = {0x22: 0x00, 0x40: 0x01, 0x41: 0x00, 0x44: 0x00, 0x45: 0x00, 0x46: 0x00, 0x4A: 0x00}
        self.missing_functions: set[int] = set()
        self.function_writes: list[tuple[int, int]] = []
        #: 14 03, the squelch level, per band (the 9700 keeps one for MAIN
        #: and one for SUB; this pretend radio has one band at a time).
        self.squelch_level_by_band: dict[int, int] = {lo: 0x40 for lo, _ in self.BANDS}
        #: The data set-up's 1A 05 items (CI-V guide p. 7) as the radio
        #: ships them, item -> value: USB and LAN AF SQL off (0107, 0111),
        #: ACC/USB/LAN MOD Level 50% (0112-0114, 0128), DATA OFF MOD
        #: MIC,ACC (0115, 02 - Basic Manual p. 8-15's default). Two-byte
        #: levels for 0112-0114. Writes go to `setup_menu_writes` as (item,
        #: value), apart from `menu_writes`, the list nobody may add to.
        self.setup_menu = {107: 0x00, 111: 0x00, 112: 128, 113: 128, 114: 128, 115: 0x02}
        self.setup_menu_writes: list[tuple[int, int]] = []
        #: DATA MOD (1A 05 0116) as the radio ships it: MIC (00). Writes to
        #: that one item are kept here and in `data_mod_writes`, apart from
        #: `menu_writes`, which stays the list of writes nobody may make.
        self.data_mod = 0x00
        self.data_mod_writes: list[int] = []
        #: Every key and unkey with the time it arrived, for turnaround tests,
        #: and how long the pretend transmitter takes to reach power after a
        #: key and to fall after an unkey, as the Po meter (15 11) shows it.
        self.ptt_times: list[tuple[float, bool]] = []
        self.rf_rise_s = 0.0
        self.rf_fall_s = 0.0
        #: RF POWER (14 0A), 0-255, per band as the radio keeps it: on
        #: Brian's 9700 the 70 cm level was found at 30 while 2 m was
        #: elsewhere (18/10/2026), so a level read on one band and put back
        #: after a retune lands on the wrong one. Full output elsewhere, as a
        #: 9700 ships. `rf_power_level` is the tuned band's. Every write that
        #: arrives is recorded, so a test can see the level the library asked
        #: for in order (shack_packet's minimum-power test step sets this to
        #: 0 before its first key).
        self.rf_power_by_band: dict[int, int] = {lo: 255 for lo, _ in self.BANDS}
        self.rf_power_by_band[430_000_000] = 30
        self.rf_power_writes: list[int] = []
        #: A test's way of modelling a radio that acknowledges RF POWER's
        #: write but does not act on it - acked (FB), level unchanged. The
        #: real failure this stands in for is never proven on this fake; it
        #: exists so shack_packet's minimum-power refusal path has something
        #: to refuse against besides an outright timeout.
        self.ignore_rf_power_writes = False
        #: What the Po meter reads when the radio is not transmitting: the
        #: real 9700 is not a clean 0 (18/10 real-radio finding, GUIDE §5),
        #: so a test that wants that modelled sets this to a short list of
        #: raw meter values; `power_now` cycles through it on every idle
        #: read. The default [0] keeps every existing test's "unkeyed is
        #: 0" assumption exactly as it was.
        self.unkeyed_noise: list[int] = [0]
        #: RF GAIN (14 02), 0-255, and the SWR (15 12) and ALC (15 13)
        #: meters' raw values, for the control strip's tests (0.3.0).
        self.rf_gain = 255
        self.swr_raw = 0
        self.alc_raw = 0
        self._unkeyed_reads = 0
        self.scope_display = False
        self.scope_output = False
        self.sweeps_sent = 0
        #: The scope's own settings (CI-V guide p. 24-25), as the radio keeps
        #: them and answers 27 14-1D reads with: centre mode at +-25 kHz,
        #: edge 1, reference 0.0 dB, mid speed, narrow VBW; and SCOPE >
        #: Averaging (1A 05 0192) off. The sweeps follow mode and span.
        self.scope_mode = 0x00
        self.scope_half_span = 25_000
        self.scope_edge = 1
        self.scope_ref = b"\x00\x00\x00"
        self.scope_speed = 0x01
        self.scope_vbw = 0x00
        self.scope_averaging = 0x00
        #: D-STAR as the radio reports it (20 00/01/02, guide p. 20): None is
        #: "nothing heard since power-on" (answered FF). `dv_call` puts a
        #: call on the air; `dv_end` ends it. Every write to the radio's
        #: automatic-output settings (20 0x 00) is recorded and changes
        #: nothing: the library reads, and must leave those settings alone.
        self.dv_callsigns: bytes | None = None
        self.dv_message: bytes | None = None
        self.dv_status = 0x00
        self.dv_output_writes: list[bytes] = []
        #: Every receive-audio byte sent, in order, so a test can check that
        #: what a listener got is exactly a run of it.
        #: Packet Radio 0.3.0's satellite and DD commands (civ.py's notes):
        #: the unselected VFO (25 01) - VFO B, or SUB in satellite mode - and
        #: satellite mode (16 5A). Every 25 write is (unselected, Hz) in
        #: `vfo_writes`, every 16 5A write in `satellite_writes`; every 06
        #: mode write (the DD switch) in `mode_writes`.
        self.freq_unselected = 145_825_000
        self.satellite = False
        self.vfo_writes: list[tuple[bool, int]] = []
        self.satellite_writes: list[bool] = []
        self.mode_writes: list[int] = []
        #: DD: TX INHIBIT as the front-panel TRANSMIT key toggles it (on as
        #: a 9700 normally powers up; `press_transmit()` is the operator's
        #: press), the two read-only DD menu items, and the radio's own
        #: transmissions of Ethernet frames reaching its [LAN] port while in
        #: DD with TX INHIBIT off (`lan_traffic`). Whether a real 9700 sends
        #: home-LAN chatter like this is exactly what tools/dd-chatter-test.py
        #: (shack-packet) is for; this models the worse answer.
        self.tx_inhibit = True
        self.dd_menu = {87: 0x01, 88: 0x00}
        self.dd_on_air_until = 0.0
        self.dd_frames_sent = 0
        self.pcm_sent = bytearray()
        self._tasks: list[asyncio.Task] = []
        self._player: asyncio.Task | None = None
        self.control = _Control(self)
        self.civ = _Civ(self)
        self.audio = _Audio(self)

    @property
    def control_port(self) -> int:
        return self.control.port

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for port in (self.control, self.civ, self.audio):
            await loop.create_datagram_endpoint(lambda p=port: p,
                                                local_addr=("127.0.0.1", 0))
        self._tasks.append(asyncio.create_task(self._sweep()))

    async def stop(self) -> None:
        for t in self._tasks + ([self._player] if self._player else []):
            t.cancel()
        for port in (self.control, self.civ, self.audio):
            if port.transport is not None:
                port.transport.close()

    def accepts(self, user: bytes, password: bytes) -> bool:
        """The login check. The obfuscation is decoded by encoding ours."""
        from .lan import passcode
        pad = lambda b: (b + bytes(16))[:16]             # noqa: E731
        return (pad(passcode(self.username)) == pad(bytes(user).rstrip(b"\0"))
                and pad(passcode(self.password)) == pad(bytes(password).rstrip(b"\0")))

    # -- the receive front end ---------------------------------------------

    def _band(self) -> list[int]:
        for lo, hi in self.BANDS:
            if lo <= self.freq <= hi:
                return self.front_end[lo]
        # Off every band: a scratch slot, so a read still has an answer.
        return self.front_end.setdefault(0, [0x00, 0x00])

    @property
    def preamp(self) -> int:
        """The 16 02 byte on the band the radio is tuned to."""
        return self._band()[0]

    @preamp.setter
    def preamp(self, value: int) -> None:
        self._band()[0] = value

    @property
    def att(self) -> int:
        """The attenuator in dB on the band the radio is tuned to."""
        b = self._band()[1]
        return (b >> 4) * 10 + (b & 0x0F)

    @att.setter
    def att(self, db: int) -> None:
        self._band()[1] = int(f"{db:02d}", 16)

    # -- D-STAR --------------------------------------------------------------

    def dv_call(self, caller: str, suffix: str = "", ur: str = "CQCQCQ", rpt1: str = "",
                rpt2: str = "", message: str = "") -> None:
        """A D-STAR station starts an over: the blocks the radio would report."""
        pad = lambda s, n: s.encode("ascii")[:n].ljust(n, b" ")   # noqa: E731
        self.dv_callsigns = (b"\x00\x00" + pad(caller, 8) + pad(suffix, 4) + pad(ur, 8)
                             + pad(rpt1, 8) + pad(rpt2, 8))
        self.dv_message = pad(message, 20) + pad(caller, 8) + pad(suffix, 4)
        self.dv_status = 0x50                     # receiving a voice call, signal

    def dv_end(self) -> None:
        self.dv_status = 0x00

    # -- CI-V --------------------------------------------------------------

    def civ_in(self, data: bytes) -> None:
        i = 0
        while True:
            start = data.find(b"\xfe\xfe", i)
            end = data.find(b"\xfd", start + 2) if start >= 0 else -1
            if start < 0 or end < 0:
                return
            frame = data[start:end + 1]
            i = end + 1
            if len(frame) < 6 or frame[2] != RADIO_CIV:
                continue
            body = bytes(frame[4:-1])
            self.civ_commands.append(body)
            self._answer(body)

    def _answer(self, body: bytes) -> None:
        cmd, rest = body[0], body[1:]
        send = self.civ.send_frame
        if cmd == 0x18 and len(rest) == 1:
            self.power_commands.append(bool(rest[0]))
            self.powered = bool(rest[0])
            if self.powered:
                send(b"\xfb")
            return
        if not self.powered:
            # Asleep: NG to everything, which is exactly what a module
            # polling a real 9700 in standby over the LAN gets back.
            send(b"\xfa")
            return
        if self._answer_sat_dd(cmd, rest):
            return
        if cmd == 0x03:
            send(b"\x03" + _bcd_freq(self.freq))
        elif cmd == 0x05 and len(rest) >= 5:
            self.freq = int("".join(f"{b:02x}" for b in reversed(rest[:5])))
            send(b"\xfb")
        elif cmd == 0x26 and len(rest) >= 1 and len(rest) < 3:
            send(bytes([0x26, 0x00, self.mode, self.data, self.filter]))
        elif cmd == 0x04:
            send(bytes([0x04, self.mode, self.filter]))
        elif cmd == 0x15 and rest[:1] == b"\x02":
            send(b"\x15\x02" + _bcd_meter(64))
        elif cmd == 0x15 and rest == b"\x11":
            send(b"\x15\x11" + _bcd_meter(self.power_now()))
        elif cmd == 0x15 and rest == b"\x12":
            send(b"\x15\x12" + _bcd_meter(self.swr_raw))
        elif cmd == 0x15 and rest == b"\x13":
            send(b"\x15\x13" + _bcd_meter(self.alc_raw))
        elif cmd == 0x14 and rest == b"\x02":
            send(b"\x14\x02" + _bcd_meter(self.rf_gain))
        elif cmd == 0x14 and len(rest) == 3 and rest[0] == 0x02:
            digits = f"{rest[1]:02x}{rest[2]:02x}"
            if not digits.isdigit() or int(digits) > 255:
                send(b"\xfa")
            else:
                self.rf_gain = int(digits)
                send(b"\xfb")
        elif cmd == 0x1C and rest[:1] == b"\x00" and len(rest) == 1:
            send(b"\x1c\x00" + (b"\x01" if self.ptt else b"\x00"))
        elif cmd == 0x1C and rest[:1] == b"\x00" and len(rest) == 2:
            self.ptt = bool(rest[1])
            self.ptt_commands.append(self.ptt)
            self.ptt_times.append((time.monotonic(), self.ptt))
            send(b"\xfb")
        elif cmd == 0x15 and rest == b"\x01":
            send(b"\x15\x01" + (b"\x01" if self.squelch_open else b"\x00"))
        elif cmd == 0x0F and len(rest) == 1 and rest[0] in (0x10, 0x11, 0x12):
            self.duplex = rest[0]
            self.duplex_writes.append(rest[0])
            send(b"\xfb")
        elif cmd == 0x0F and len(rest) == 1 and rest[0] in (0x00, 0x01):
            self.split = bool(rest[0])
            self.duplex_writes.append(rest[0])
            send(b"\xfb")
        elif cmd == 0x16 and len(rest) == 2 and rest[0] in self.tone_functions:
            self.tone_functions[rest[0]] = rest[1]
            send(b"\xfb")
        elif cmd == 0x16 and len(rest) == 1 and rest[0] in self.tone_functions:
            send(bytes([0x16, rest[0], self.tone_functions[rest[0]]]))
        elif cmd == 0x16 and rest[:1] and rest[0] in self.functions:
            sub = rest[0]
            if sub in self.missing_functions or (len(rest) == 2 and rest[1] > 0x01) or len(rest) > 2:
                send(b"\xfa")
            elif len(rest) == 1:
                send(bytes([0x16, sub, self.functions[sub]]))
            else:
                self.functions[sub] = rest[1]
                self.function_writes.append((sub, rest[1]))
                send(b"\xfb")
        elif cmd == 0x14 and rest == b"\x03":
            send(b"\x14\x03" + _bcd_meter(self.squelch_level))
        elif cmd == 0x14 and len(rest) == 3 and rest[0] == 0x03:
            digits = f"{rest[1]:02x}{rest[2]:02x}"
            if not digits.isdigit() or int(digits) > 255:
                send(b"\xfa")
            else:
                self.squelch_level = int(digits)
                send(b"\xfb")
        elif cmd == 0x26 and len(rest) == 4:
            # A set: VFO, mode, data, filter. The selected VFO only, which is
            # the only one this pretend radio has.
            self.mode, self.data, self.filter = rest[1], rest[2], rest[3]
            send(b"\xfb")
        elif cmd == 0x16 and rest[:1] == b"\x02" and len(rest) == 1:
            send(bytes([0x16, 0x02, self._band()[0]]))
        elif cmd == 0x16 and rest[:1] == b"\x02" and len(rest) == 2:
            if rest[1] not in self.PREAMP_VALUES:
                send(b"\xfa")
            else:
                self._band()[0] = rest[1]
                send(b"\xfb")
        elif cmd == 0x11 and not rest:
            send(bytes([0x11, self._band()[1]]))
        elif cmd == 0x11 and len(rest) == 1:
            if rest[0] not in self.ATT_VALUES:
                send(b"\xfa")
            else:
                self._band()[1] = rest[0]
                send(b"\xfb")
        elif cmd == 0x1A and rest[:1] == b"\x05" and len(rest) >= 3:
            digits = f"{rest[1]:02x}{rest[2]:02x}"           # BCD item number
            item = int(digits) if digits.isdigit() else -1
            if item == 116:
                # DATA MOD: 00 MIC .. 05 LAN (CI-V guide p. 7).
                if len(rest) > 3:
                    if rest[3] > 0x05:
                        send(b"\xfa")
                    else:
                        self.data_mod = rest[3]
                        self.data_mod_writes.append(rest[3])
                        send(b"\xfb")
                else:
                    send(bytes([0x1A, 0x05, rest[1], rest[2], self.data_mod]))
            elif item in self.setup_menu:
                two = item in (112, 113, 114)
                if len(rest) == 3:
                    v = self.setup_menu[item]
                    send(bytes([0x1A, 0x05, rest[1], rest[2]]) + (_bcd_meter(v) if two else bytes([v])))
                else:
                    if two:
                        digits = "".join(f"{b:02x}" for b in rest[3:5])
                        ok = len(rest) == 5 and digits.isdigit() and int(digits) <= 255
                        value = int(digits) if ok else None
                    else:
                        ok = len(rest) == 4 and rest[3] <= (0x05 if item == 115 else 0x01)
                        value = rest[3] if ok else None
                    if not ok:
                        send(b"\xfa")
                    else:
                        self.setup_menu[item] = value
                        self.setup_menu_writes.append((item, value))
                        send(b"\xfb")
            elif item == 192:
                # SCOPE > Averaging: the one menu item a client may write.
                if len(rest) > 3:
                    if rest[3] > 0x03:
                        send(b"\xfa")
                    else:
                        self.scope_averaging = rest[3]
                        send(b"\xfb")
                else:
                    send(bytes([0x1A, 0x05, rest[1], rest[2], self.scope_averaging]))
            elif len(rest) > 3:
                self.menu_writes.append(bytes(body))
                send(b"\xfb")
            elif item in self.ext_preamp_menu:
                send(bytes([0x1A, 0x05, rest[1], rest[2], self.ext_preamp_menu[item]]))
            else:
                # Not a menu item this pretend radio models: NG rather than a
                # made-up value, so a test cannot lean on an invented answer.
                send(b"\xfa")
        elif cmd == 0x14 and rest == b"\x09":
            send(b"\x14\x09" + _bcd_meter(self.cw_pitch_level))
        elif cmd == 0x14 and rest == b"\x0a":
            send(b"\x14\x0a" + _bcd_meter(self.rf_power_level))
        elif cmd == 0x14 and len(rest) == 3 and rest[0] == 0x0a:
            digits = f"{rest[1]:02x}{rest[2]:02x}"
            if not digits.isdigit():
                send(b"\xfa")
            elif self.ignore_rf_power_writes:
                send(b"\xfb")
            else:
                self.rf_power_level = int(digits)
                self.rf_power_writes.append(self.rf_power_level)
                send(b"\xfb")
        elif cmd == 0x0F and not rest:
            send(bytes([0x0F, self.duplex if self.duplex != 0x10 else int(self.split)]))
        elif cmd == 0x27 and rest[:1] == b"\x10" and len(rest) == 2:
            self.scope_display = bool(rest[1])
            send(b"\xfb")
        elif cmd == 0x27 and rest[:1] == b"\x11" and len(rest) == 2:
            self.scope_output = bool(rest[1])
            send(b"\xfb")
        elif cmd == 0x27 and len(rest) >= 2 and rest[0] in (0x14, 0x15, 0x16, 0x19, 0x1A, 0x1D):
            self._scope_setting(rest)
        elif cmd == 0x20 and len(rest) >= 2 and rest[0] in (0x00, 0x01, 0x02):
            if rest[1] == 0x00:
                self.dv_output_writes.append(bytes(body))
                send(b"\xfb")
            elif rest[1] == 0x02:
                block = {0: self.dv_callsigns, 1: self.dv_message,
                         2: bytes([self.dv_status]) if self.dv_callsigns else None}[rest[0]]
                send(bytes([0x20, rest[0], 0x02]) + (block if block is not None else b"\xff"))
            else:
                send(b"\xfa")
        else:
            send(b"\xfb")

    #: DD is 23 cm only (Advanced Manual section 13); the band's edges as
    #: BANDS has them.
    DD_BAND = (1_240_000_000, 1_300_000_000)
    #: How long one Ethernet frame keeps the pretend DD transmitter on.
    DD_FRAME_S = 0.05

    def _answer_sat_dd(self, cmd: int, rest: bytes) -> bool:
        """25, 16 5A, 06 (mode set), 0F 13 and the DD menu items; the radio
        on the air in DD as 1C 00 and 15 11 read it. True when answered."""
        send = self.civ.send_frame
        if cmd == 0x25 and rest[:1] in (b"\x00", b"\x01"):
            other = rest[0] == 0x01
            if len(rest) == 1:
                send(bytes([0x25, rest[0]]) + _bcd_freq(self.freq_unselected if other else self.freq))
            elif len(rest) == 6:
                hz = int("".join(f"{b:02x}" for b in reversed(rest[1:6])))
                if other:
                    self.freq_unselected = hz
                else:
                    self.freq = hz
                self.vfo_writes.append((other, hz))
                send(b"\xfb")
            else:
                send(b"\xfa")
            return True
        if cmd == 0x16 and rest[:1] == b"\x5a":
            if len(rest) == 1:
                send(bytes([0x16, 0x5A, int(self.satellite)]))
            elif len(rest) == 2 and rest[1] <= 1:
                self.satellite = bool(rest[1])
                self.satellite_writes.append(self.satellite)
                send(b"\xfb")
            else:
                send(b"\xfa")
            return True
        if cmd == 0x06 and rest:
            lo, hi = self.DD_BAND
            if rest[0] == 0x22 and not lo <= self.freq <= hi:
                send(b"\xfa")               # DD outside 23 cm
                return True
            self.mode = rest[0]
            if len(rest) > 1:
                self.filter = rest[1]
            self.mode_writes.append(rest[0])
            send(b"\xfb")
            return True
        if cmd == 0x0F and rest == b"\x13":
            self.duplex = 0x13
            self.duplex_writes.append(0x13)
            send(b"\xfb")
            return True
        if cmd == 0x1A and rest[:1] == b"\x05" and len(rest) == 3:
            digits = f"{rest[1]:02x}{rest[2]:02x}"
            if digits.isdigit() and int(digits) in self.dd_menu:
                send(bytes([0x1A, 0x05, rest[1], rest[2], self.dd_menu[int(digits)]]))
                return True
        if self.dd_on_air():
            if cmd == 0x1C and rest == b"\x00":
                send(b"\x1c\x00\x01")
                return True
            if cmd == 0x15 and rest == b"\x11":
                send(b"\x15\x11" + _bcd_meter(self.rf_power))
                return True
        return False

    def turn_dial(self, hz: int | None = None, mode: int | None = None) -> None:
        """The operator turning the VFO knob (or the mode knob) on the
        radio. With CI-V Transceive on, as a 9700 ships, the radio tells
        every controller at once: 00 (the frequency) or 01 (the mode, no
        data flag) to the broadcast address 00, unasked (CI-V reference,
        "Transceive"). Call on the fake's own loop."""
        if hz is not None:
            self.freq = int(hz)
            if self.transceive and self.civ.open:
                self.civ.send_frame(b"\x00" + _bcd_freq(self.freq), to=0x00)
        if mode is not None:
            self.mode = int(mode)
            if mode not in (0x00, 0x01, 0x05, 0x02):
                self.data = 0x00
            if self.transceive and self.civ.open:
                self.civ.send_frame(bytes([0x01, self.mode, self.filter]), to=0x00)

    def press_transmit(self) -> None:
        """The operator pressing TRANSMIT: TX INHIBIT toggles."""
        self.tx_inhibit = not self.tx_inhibit

    def lan_traffic(self, frames: int = 1) -> bool:
        """Ethernet frames reaching the radio's [LAN] port. In DD with TX
        INHIBIT off, the radio sends them: the transmitter is on for
        DD_FRAME_S a frame. True when they went to air."""
        if self.mode != 0x22 or self.tx_inhibit or not self.powered:
            return False
        start = max(time.monotonic(), self.dd_on_air_until)
        self.dd_on_air_until = start + self.DD_FRAME_S * frames
        self.dd_frames_sent += frames
        return True

    def dd_on_air(self) -> bool:
        return self.mode == 0x22 and time.monotonic() < self.dd_on_air_until

    def _scope_setting(self, rest: bytes) -> None:
        """27 14/15/16/19/1A/1D: a read (sub-command and receiver only) or a
        write. Out-of-range values are NG, as the radio answers them."""
        send = self.civ.send_frame
        sub, rx, value = rest[0], rest[1], bytes(rest[2:])
        if not value:
            current = {0x14: bytes([self.scope_mode]), 0x15: _bcd_freq(self.scope_half_span),
                       0x16: bytes([self.scope_edge]), 0x19: self.scope_ref,
                       0x1A: bytes([self.scope_speed]), 0x1D: bytes([self.scope_vbw])}[sub]
            send(bytes([0x27, sub, rx]) + current)
            return
        ok = True
        if sub == 0x14 and value[0] in (0, 1):
            self.scope_mode = value[0]
        elif sub == 0x15 and len(value) == 5:
            half = int("".join(f"{b:02x}" for b in reversed(value)))
            ok = half in (2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000)
            if ok:
                self.scope_half_span = half
        elif sub == 0x16 and 1 <= value[0] <= 3:
            self.scope_edge = value[0]
        elif sub == 0x19 and len(value) == 3:
            self.scope_ref = value
        elif sub == 0x1A and value[0] <= 2:
            self.scope_speed = value[0]
        elif sub == 0x1D and value[0] <= 1:
            self.scope_vbw = value[0]
        else:
            ok = False
        send(b"\xfb" if ok else b"\xfa")

    async def _sweep(self) -> None:
        """Wave data while the scope and its CI-V output are both on.

        One frame per sweep, seq 1 of 1, span and all 475 levels together -
        the LAN shape (the serial shape is eleven pieces). Centre mode, so the
        frame carries the centre and the half-span and the module has to turn
        that into edges. The peak moves one point a sweep so a test can tell
        sweeps apart.
        """
        period = 1.0 / self.SWEEPS_PER_S
        while True:
            await asyncio.sleep(period)
            if not (self.powered and self.scope_display and self.scope_output
                    and self.civ.open):
                continue
            peak = self.sweeps_sent % SPECTRUM_POINTS
            levels = bytes(min(SPECTRUM_MAX, 20 + (100 if i == peak else i % 7))
                           for i in range(SPECTRUM_POINTS))
            if self.scope_mode == 0x00:
                head = b"\x00" + _bcd_freq(self.freq) + _bcd_freq(self.scope_half_span)
            else:
                # Fixed mode sends the two edges; the edge sets are not
                # modelled, so it is the band's first 100 kHz.
                lo = (self.freq // 100_000) * 100_000
                head = b"\x01" + _bcd_freq(lo) + _bcd_freq(lo + 100_000)
            payload = b"\x27\x00\x00\x01\x01" + head + b"\x00" + levels
            self.civ.send_frame(payload)
            self.sweeps_sent += 1

    #: The Po meter's floor at RF POWER 0000 - never nought, as the real
    #: radio's isn't (civ.py's note on RF_POWER). A small raw value rather
    #: than 0 means a test cannot mistake "minimum" for "the transmitter is
    #: off", which is exactly the distinction the real-radio test cares about.
    RF_POWER_FLOOR = 2

    def _band_lo(self) -> int:
        for lo, hi in self.BANDS:
            if lo <= self.freq <= hi:
                return lo
        return 0

    @property
    def rf_power_level(self) -> int:
        """RF POWER (14 0A), 0-255, on the band the radio is tuned to."""
        return self.rf_power_by_band.setdefault(self._band_lo(), 255)

    @rf_power_level.setter
    def rf_power_level(self, level: int) -> None:
        self.rf_power_by_band[self._band_lo()] = level

    @property
    def squelch_level(self) -> int:
        """14 03 on the band the radio is tuned to."""
        return self.squelch_level_by_band.setdefault(self._band_lo(), 0x40)

    @squelch_level.setter
    def squelch_level(self, level: int) -> None:
        self.squelch_level_by_band[self._band_lo()] = level

    @property
    def rf_power(self) -> int:
        """The Po meter's ceiling once keyed, from the RF POWER level (14
        0A): a straight line from RF_POWER_FLOOR at level 0 to 255 at 255."""
        return self.RF_POWER_FLOOR + round(
            (255 - self.RF_POWER_FLOOR) * self.rf_power_level / 255)

    def _noise(self) -> int:
        """The next value of `unkeyed_noise`, cycling; a single list index
        rather than `random`, so a test that sets it gets the same answers
        every run."""
        v = self.unkeyed_noise[self._unkeyed_reads % len(self.unkeyed_noise)]
        self._unkeyed_reads += 1
        return v

    def power_now(self) -> int:
        """The Po meter now: up rf_rise_s after the last key, down
        rf_fall_s after the last unkey - `_noise()` otherwise, which is 0
        unless a test asked for something closer to the real radio's idle
        reading."""
        if not self.ptt_times:
            return self._noise()
        t, keyed = self.ptt_times[-1]
        age = time.monotonic() - t
        if keyed:
            return self.rf_power if age >= self.rf_rise_s else self._noise()
        return self.rf_power if age < self.rf_fall_s else self._noise()

    # -- audio -------------------------------------------------------------

    def tx_audio_in(self, pcm: bytes) -> None:
        """Transmit audio from the client: kept, and counted against PTT."""
        self.tx_pcm += pcm
        if not self.ptt:
            self.tx_pcm_unkeyed += len(pcm)
        self.tx_log.append((time.monotonic(), self.ptt, len(pcm)))

    def tx_playout(self, buffer_ms: int | None = None) -> list[tuple[float, float]]:
        """The holes the radio would have transmitted in the audio it was sent.

        Returns (where, how long) in seconds of the transmission, for every
        stretch with no audio between the first sample and the last. Not the
        firmware - nobody outside Icom has that - but a model of what the
        IC-9700 did on 18/10/2026 (shack-packet HANDOVER.md), built on two
        rules:

        - **It plays the sequence, slot by slot.** A packet that takes a
          place in the client's sequence and carries no audio (an idle sent
          mid-over) is a hole as long as the audio packets around it. That
          is what the capture showed: holes of 15-20 ms - one 20 ms packet's
          worth, fixed, not the varying length lateness would give - about
          every 100 ms, which is how often the library sent an idle on the
          audio stream. wfview never sends one there, and the earlier app
          that was decoded on air sent one only after 100 ms of silence.
        - **It holds `buffer_ms` before it plays** (the txbuffer the client
          asked for in conninfo, 100 ms from lan.py) and a packet that comes
          after its turn is a hole until it comes: an underrun.
        """
        req = self.stream_requests[-1] if self.stream_requests else {}
        rate = req.get("tx_rate") or 12000
        if buffer_ms is None:
            buffer_ms = req.get("tx_buffer") or 100
        stream = sorted(self.tx_stream, key=lambda e: e[0])
        audio_at = [i for i, e in enumerate(stream) if e[2]]
        if not audio_at:
            return []
        stream = stream[audio_at[0]:audio_at[-1] + 1]
        first = stream[0][0]
        play_at = first + buffer_ms / 1000
        start = play_at
        holes = []
        last = stream[0][2] / 2 / rate
        for t, _seq, n in stream:
            if t > play_at:
                holes.append((play_at - start, t - play_at))
                play_at = t
            if n:
                last = n / 2 / rate
                play_at += last
            else:
                holes.append((play_at - start, last))
                play_at += last
        return holes

    def audio_ready(self) -> None:
        req = self.stream_requests[-1] if self.stream_requests else {}
        # One player at a time: a client that reconnects gets a fresh stream,
        # not a second one interleaved with the first.
        if self._player is not None:
            self._player.cancel()
            self._player = None
        if req.get("rx_enable") and req.get("rx_rate"):
            self._player = asyncio.create_task(self._play(req["rx_rate"]))

    async def _play(self, rate: int) -> None:
        """A counter as audio, at the agreed rate, in 20 ms packets, on time.

        Each sample is its own index (wrapping at 16 bits), so any run of a
        few samples says exactly where in the stream it came from: a test can
        tell a gap, a repeat or a reordering apart, which a tone cannot - a
        700 Hz tone at 12 kHz repeats every 120 samples. Unless `audio_source`
        is set, when the packets are whatever it makes.
        """
        n = rate * self.PACKET_MS // 1000
        start = time.monotonic()
        k = 0
        while True:
            source = self.audio_source
            if source is not None:
                pcm = bytes(source(rate, n))
            else:
                pcm = struct.pack(f"<{n}H", *(((k * n + i) & 0xFFFF) for i in range(n)))
            self.audio.send_pcm(pcm)
            self.pcm_sent += pcm
            k += 1
            await asyncio.sleep(max(0.0, start + k * self.PACKET_MS / 1000
                                    - time.monotonic()))
