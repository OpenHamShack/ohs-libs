"""Icom's LAN protocol at the bytes: login obfuscation, the stream request,
audio and CI-V packets, and the goodbye.

Ported from OpenHamShack Icom's tools/selftest.py (test_passcode, the packet
half of test_audio_path, test_civ_on_the_wire, test_the_lan_goodbye). Two of
the originals read lan.py's source for big-endian port reads; they are not
ported, because the FakeIcom end-to-end tests (test_icomrig.py, test_ptt.py)
cannot connect at all unless those ports are read correctly. The FT8 decode
at the end of test_audio_path needs the app's decoders and stays there.

Every check here is of something that fails silently: a codec byte that means
stereo, a length field one byte long, a port read little-endian. None of those
raise; they produce a radio that seems switched off.
"""

import asyncio
import struct

from shack_rig.icom import civ, lan


class Catcher:
    """A datagram transport that keeps what was sent instead of sending it."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr=None):
        self.sent.append(bytes(data))

    def close(self):
        self.closed = True

    def get_extra_info(self, *a, **kw):
        return None


def test_passcode():
    assert len(lan.passcode("x" * 16)) == 16
    assert len(lan.passcode("y" * 40)) == 16, "truncated to the field"
    assert len(lan.passcode(chr(126) * 8)) == 8, "high characters do not raise"
    assert lan.passcode("aa")[0] != lan.passcode("aa")[1], "position changes the output"


def test_what_the_radio_is_asked_for():
    assert lan.CODEC_PCM_MONO_16 == 0x04
    assert 12000 in lan.AUDIO_RATES and lan.DEFAULT_AUDIO_RATE == 12000
    assert lan.ControlStream("h", 1, "u", "p", "c", audio_rate=44100).audio_rate == 0
    assert lan.ControlStream("h", 1, "u", "p", "c", audio_rate=12000).audio_rate == 12000
    assert lan.ControlStream("h", 1, "u", "p", "c").audio_rate == 0


def conninfo(tx_sample: int = 0x3F3F, **kw) -> bytes:
    """The stream request really put on the wire, built rather than read."""
    st = lan.ControlStream("127.0.0.1", 50001, "u", "p", "comp", **kw)
    st.current = {"name": "IC-9700", "use_guid": False, "guid": bytes(16),
                  "mac": bytes.fromhex("00908c123456"), "tx_sample": tx_sample}
    st.transport = Catcher()
    st.send_request_stream()
    return st.transport.sent[0]


def test_what_the_radio_is_told_about_transmitting():
    quiet = conninfo()
    assert quiet[0x70] == 0 and quiet[0x71] == 0
    rx = conninfo(audio_rate=12000)
    assert rx[0x70] == 1 and rx[0x71] == 0 and rx[0x73] == 0, "receive alone leaves transmit off"
    assert struct.unpack_from(">I", rx, 0x78)[0] == 0 and struct.unpack_from(">I", rx, 0x84)[0] == 0
    tx = conninfo(audio_rate=12000, tx_rate=12000)
    assert tx[0x71] == 1 and tx[0x73] == lan.CODEC_PCM_MONO_16
    assert struct.unpack_from(">I", tx, 0x78)[0] == 12000
    assert struct.unpack_from(">I", tx, 0x84)[0] == lan.TX_BUFFER_MS
    assert tx[0x70] == 1 and struct.unpack_from(">I", tx, 0x74)[0] == 12000
    # A radio whose capabilities say it cannot be transmitted into is not.
    refused = conninfo(tx_sample=1, audio_rate=12000, tx_rate=12000)
    assert refused[0x71] == 0 and struct.unpack_from(">I", refused, 0x78)[0] == 0
    assert lan.ControlStream("h", 1, "u", "p", "c", tx_rate=12000).tx_rate == 0, \
        "transmit audio without receive audio is refused outright"
    assert lan.ControlStream("h", 1, "u", "p", "c", audio_rate=12000, tx_rate=44100).tx_rate == 0
    assert lan.ControlStream("h", 1, "u", "p", "c", audio_rate=12000).tx_rate == 0


def test_a_packet_of_transmit_audio_byte_by_byte():
    out = lan.AudioStream("127.0.0.1", 1, 0, lambda d: None, tx_rate=12000)
    out.transport = Catcher()
    assert not out.tx_audio_ready
    assert out.send_audio(b"\x00" * 480) is False and not out.transport.sent
    out.remote_id = 0x12345678
    out.packets = 1
    assert out.tx_audio_ready

    pcm = bytes(range(240)) * 2
    assert out.send_audio(pcm) is True and len(out.transport.sent) == 1
    p = out.transport.sent[0]
    assert struct.unpack_from("<I", p, 0x00)[0] == len(p) == 0x18 + len(pcm)
    assert struct.unpack_from("<H", p, 0x04)[0] == 0x0000
    assert struct.unpack_from("<H", p, 0x10)[0] == 0x0080
    assert struct.unpack_from(">H", p, 0x12)[0] == 0, "the audio sequence is big-endian"
    assert struct.unpack_from(">H", p, 0x16)[0] == len(pcm), "so is the sample count"
    assert p[0x18:] == pcm

    out.transport.sent.clear()
    big = bytes(3000)
    out.send_audio(big)
    sizes = [len(q) - 0x18 for q in out.transport.sent]
    assert sum(sizes) == len(big) and max(sizes) <= lan.TX_PACKET_BYTES
    seqs = [struct.unpack_from(">H", q, 0x12)[0] for q in out.transport.sent]
    assert seqs == list(range(1, 1 + len(seqs)))
    assert b"".join(q[0x18:] for q in out.transport.sent) == big

    out.transport.sent.clear()
    assert out.send_audio(b"\x00" * 481) is False and not out.transport.sent, "half a sample"

    out.transport.sent.clear()
    out.tx_end()
    tail = sum(len(q) - 0x18 for q in out.transport.sent)
    assert out.transport.sent and all(q[0x18:] == bytes(len(q) - 0x18) for q in out.transport.sent)
    assert tail == int(12000 * lan.TX_TAIL_MS / 1000) * 2
    assert out.tx_drain_s >= lan.TX_BUFFER_MS / 1000.0


def test_a_packet_of_receive_audio_taken_apart():
    got = bytearray()
    stream = lan.AudioStream("127.0.0.1", 1, 0, got.extend)
    header = bytearray(0x18)
    payload = bytes(range(16))
    stream.on_packet(bytes(header) + payload, 0x18 + len(payload), 0x00, 0, 0)
    assert bytes(got) == payload
    got.clear()
    stream.on_packet(bytes(header) + b"\x01\x02", 0x1A, 0x01, 1, 0)
    assert not got, "a control packet is not audio"
    stream.on_packet(bytes(bytearray(0x1C)), 0x10, 0x00, 2, 0)
    assert not got, "nor is a runt"


def test_a_civ_frame_in_its_envelope():
    sent = []
    st = lan.CivStream("192.0.2.1", 50002, 0, lambda d: None)
    st.send_raw = lambda d: sent.append(bytes(d))
    st.my_id, st.remote_id = 0x11223344, 0x55667788
    frame = civ.frame(civ.read_freq(), to=0xA2, frm=0xE0)
    st.send_civ(frame)
    p = sent[-1]
    # The fault this guards: the header was built at PING_SIZE + n and the
    # frame appended after it, so 109 well-formed reads reached the radio as
    # frames of NULs and nothing came back.
    assert len(p) == lan.PING_SIZE + len(frame)
    assert struct.unpack_from("<I", p, 0)[0] == len(p)
    assert p[0x10] == 0xC1
    assert struct.unpack_from("<H", p, 0x11)[0] == len(frame)
    assert struct.unpack_from(">H", p, 0x13)[0] == 0
    assert p[0x15:] == frame
    st.send_civ(frame)
    assert struct.unpack_from(">H", sent[-1], 0x13)[0] == 1


def test_a_civ_frame_coming_back():
    got = []
    st = lan.CivStream("192.0.2.1", 50002, 0, got.append)
    reply = civ.frame(b"\x03" + civ.encode_freq(144_800_000), to=0xE0, frm=0xA2)
    r = bytearray(lan.PING_SIZE + len(reply))
    struct.pack_into("<I", r, 0, len(r))
    struct.pack_into("<H", r, 0x11, len(reply))
    r[lan.PING_SIZE:] = reply
    st.on_packet(bytes(r), len(r), 0x00, 1, 0)
    assert got == [reply]
    assert st.last_data > 0 and st._seen_data
    st.on_packet(bytes(r), len(r), 0x00, 1, 0)
    assert len(got) == 1, "a packet the radio sent twice is delivered once"


def kinds(sock):
    out = []
    for p in sock.sent:
        out.append((int.from_bytes(p[0:4], "little"), int.from_bytes(p[4:6], "little"),
                    p[0x15] if len(p) > 0x15 else None))
    return out


def test_the_lan_goodbye_gives_the_token_back_before_disconnecting():
    async def run():
        ctl = lan.ControlStream("192.0.2.1", 50001, "u", "p", "test")
        sock = Catcher()
        ctl.transport = sock
        ctl.authenticated = True
        ctl.token = 0x11223344
        ctl._token_task = asyncio.create_task(ctl._renew_token())
        await asyncio.sleep(0)
        task = ctl._token_task
        await ctl.stop()
        seen = kinds(sock)
        release = [i for i, (n, _t, req) in enumerate(seen) if n == lan.TOKEN_SIZE and req == 0x01]
        bye = [i for i, (n, t, _r) in enumerate(seen) if n == lan.CONTROL_SIZE and t == lan.T_DISCONNECT]
        # Without the release the IC-9700 holds the session for about a
        # minute, which is the minute every reconnect waited out.
        assert len(release) == 1 and bye and release[0] < bye[0], seen
        await asyncio.sleep(0.01)
        assert task.done() and sock.closed

        cold = lan.ControlStream("192.0.2.1", 50001, "u", "p", "test")
        cold_sock = Catcher()
        cold.transport = cold_sock
        await cold.stop()
        seen = kinds(cold_sock)
        assert not any(req == 0x01 and n == lan.TOKEN_SIZE for n, _t, req in seen)
        assert any(n == lan.CONTROL_SIZE and t == lan.T_DISCONNECT for n, t, _r in seen)

    asyncio.run(run())


def _stream():
    s = lan._Stream("127.0.0.1", 1, "test")
    s.transport = Catcher()
    s.remote_id = 0x12345678
    return s


def test_several_packets_asked_for_at_once_are_sent_again():
    # The radio's list form of a retransmit request: numbers after the
    # header, here in the earlier app's pairs. A held packet goes again as it
    # was; one no longer held is answered with an idle carrying its number,
    # so the radio stops waiting for it.
    s = _stream()
    for _ in range(3):
        s.send_control(lan.T_IDLE)
    held = list(s.transport.sent)
    seqs = [struct.unpack_from("<H", p, 6)[0] for p in held]
    s.transport.sent.clear()
    req = bytearray(0x10) + struct.pack("<4H", seqs[0], seqs[0], seqs[2], 0x4000)
    struct.pack_into("<IHHII", req, 0, len(req), lan.T_RETRANSMIT, 0, 0x12345678, s.my_id)
    s._handle(bytes(req))
    assert s.transport.sent[:2] == [held[0], held[2]]
    gone = s.transport.sent[2]
    assert len(gone) == 0x10 and struct.unpack_from("<HH", gone, 4) == (lan.T_IDLE, 0x4000)
    assert (s.retransmits, s.retransmits_missing) == (3, 1)


def test_pings_have_their_own_numbers():
    # Not the tracked sequence: a ping used to carry the number the next
    # tracked packet was about to take. It moves on when the radio answers.
    s = _stream()
    s.send_control(lan.T_IDLE)
    s.send_ping()
    s.send_ping()
    pings = [p for p in s.transport.sent if len(p) == lan.PING_SIZE]
    assert [struct.unpack_from("<H", p, 6)[0] for p in pings] == [0, 0], "unanswered: asked again"
    reply = bytearray(pings[0])
    reply[0x10] = 0x01
    s._handle(bytes(reply))
    s.send_ping()
    assert struct.unpack_from("<H", s.transport.sent[-1], 6)[0] == 1
    assert s.send_seq == 2, "the tracked sequence is untouched by pings"
