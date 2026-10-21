"""Winlink B2F and its LZHUF: the compression byte for byte against
wl2k-go's own test vectors, the message format against a real message
Pat received in 2016, the secure login against wl2k-go's answers, and a
whole session - a station sending and collecting email at a pretend RMS
gateway - over the simulated channel."""

from __future__ import annotations

from pathlib import Path

import pytest
from stations import World

from shack_packet import b2f, lzhuf
from shack_packet.datalink import State

DATA = Path(__file__).parent / "data" / "lzhuf"

# From wl2k-go lzhuf/lzhuf_test.go (MIT): what Winlink's LZHUF makes of
# these, CRC and length header included.
SAMPLES = [
    (b"\n", bytes([0xE, 0x8F, 0x1, 0x0, 0x0, 0x0, 0xCB, 0x0])),
    (b"foo", bytes([0xB6, 0x47, 0x3, 0x0, 0x0, 0x0, 0xF9, 0x7E, 0xF1, 0x0])),
    (b"bar", bytes([0xC7, 0xEF, 0x03, 0x00, 0x00, 0x00, 0xF7, 0x7B, 0x7F, 0xC0])),
]


def test_lzhuf_matches_winlinks_bytes():
    for plain, packed in SAMPLES:
        assert lzhuf.compress(plain) == packed
        assert lzhuf.decompress(packed) == plain
    for name in ("gettysburg.txt", "LPE5NXDVLVSQ.b2f"):
        plain = (DATA / name).read_bytes()
        packed = (DATA / (name + ".lzh")).read_bytes()
        assert lzhuf.compress(plain) == packed
        assert lzhuf.decompress(packed) == plain


def test_lzhuf_refuses_damage():
    plain, packed = SAMPLES[1]
    bad = bytearray(packed)
    bad[-1] ^= 0x40
    with pytest.raises(lzhuf.ChecksumError):
        lzhuf.decompress(bytes(bad))
    with pytest.raises(lzhuf.ChecksumError):
        lzhuf.decompress(packed[:7])
    # Without the CRC (FBB's B1), a damaged length is still caught.
    assert lzhuf.decompress(lzhuf.compress(b"hello", crc=False), crc=False) == b"hello"
    with pytest.raises(lzhuf.ChecksumError):
        lzhuf.decompress(b"\xff\xff\xff\x7f" + b"\0" * 8, crc=False, max_size=1000)


def test_secure_login_matches_wl2k_go():
    # wl2k-go fbb/secure_test.go: the password is case-sensitive.
    assert b2f.login_response("23753528", "FOOBAR") == "72768415"
    assert b2f.login_response("23753528", "FooBar") == "95074758"


def test_a_real_message_parses_and_writes_back():
    raw = (DATA / "LPE5NXDVLVSQ.b2f").read_bytes()
    msg = b2f.WinlinkMessage.from_bytes(raw)
    assert msg.mid == "LPE5NXDVLVSQ" and msg.sender == "LA5NTA" and msg.to == ["LA4TTA"]
    assert msg.subject == "73 fra Brekke" and msg.body.startswith("Hei!")
    assert "prøver" in msg.body            # ISO-8859-1, as its Content-Type says
    assert [f.name for f in msg.files] == ["1469042410710.jpg"] and len(msg.files[0].data) == 31028
    assert msg.files[0].data[:2] == b"\xff\xd8"
    # Written again, Winlink's way (Mid first, then names in order), it is
    # byte for byte what Winlink Express made.
    assert msg.to_bytes() == raw


def test_addresses():
    assert b2f.address("g0abc") == "G0ABC"
    assert b2f.address("G0ABC@winlink.org") == "G0ABC"
    assert b2f.address("someone@example.com") == "SMTP:someone@example.com"
    assert b2f.is_internet("someone@example.com") and not b2f.is_internet("GM4BRI")
    assert b2f.plain("SMTP:a@b.c") == "a@b.c"


def test_non_ascii_subject_and_body_round_trip():
    m = b2f.WinlinkMessage("ABCDEFGHIJKL", "GM4BRI", ["a@example.com"], "Café at 1900", "Où?\nÀ bientôt",
                           date=1_790_000_000.0, files=[b2f.Attachment("notes.txt", b"one\r\ntwo")])
    data = m.to_bytes()
    assert b"Subject: =?" in data and data.isascii() is False
    back = b2f.WinlinkMessage.from_bytes(data)
    assert back.subject == "Café at 1900" and back.body == "Où?\nÀ bientôt"
    assert back.to == ["SMTP:a@example.com"] and back.files[0].data == b"one\r\ntwo"
    assert back.date == 1_790_000_000.0 - 1_790_000_000.0 % 60


class Station:
    """The station's side of the exchange: an outbox, and what arrived."""

    def __init__(self, outbox=()):
        self.outbox = list(outbox)
        self.inbox: list[b2f.WinlinkMessage] = []
        self.delivered_list: list[tuple[str, str]] = []

    def outgoing(self):
        done = {mid for mid, _ in self.delivered_list}
        return [m for m in self.outbox if m.mid not in done]

    def answer(self, mid, title, size):
        return "-" if any(m.mid == mid for m in self.inbox) else "+"

    def received(self, msg):
        self.inbox.append(msg)

    def delivered(self, mid, how):
        self.delivered_list.append((mid, how))


def message(n, to="someone@example.com", size=0):
    return b2f.WinlinkMessage(f"MID{n:09d}", "GM4BRI", [to], f"Test {n}",
                              "Hello from the simulator.\n" + "x" * size, date=1_790_000_000.0)


def session(world, box, *, password="secret", accounts=None, via=()):
    rms = world.station("GB7RMS")
    gw = b2f.Gateway(rms, ssid=10, accounts=accounts if accounts is not None else {"GM4BRI": "secret"},
                     challenge=lambda: "23753528")
    me = world.station("GM4BRI")
    ended = []
    sess = b2f.call(me, "GB7RMS-10", box, mycall="GM4BRI", password=lambda: password, locator="IO75RL",
                    on_done=ended.append)
    return gw, me, sess, ended


def test_send_and_collect_through_a_gateway():
    world = World()
    box = Station([message(1), message(2, to="G4XYZ", size=600)])
    rms_waiting = message(9, to="GM4BRI")
    rms_waiting.sender = "SMTP:friend@example.com"
    gw, me, sess, ended = session(world, box)
    gw.waiting["GM4BRI"] = [rms_waiting]
    assert world.run(600, lambda: ended)
    assert sess.state == "done", (sess.error, sess.remote_error)
    assert [m.mid for m in gw.received] == ["MID000000001", "MID000000002"]
    assert gw.received[1].to == ["G4XYZ"] and gw.received[1].body.endswith("x" * 600)
    assert [m.mid for m in box.inbox] == ["MID000000009"]
    assert box.inbox[0].sender == "SMTP:friend@example.com"
    assert sorted(box.delivered_list) == [("MID000000001", "sent"), ("MID000000002", "sent")]
    assert gw.waiting["GM4BRI"] == []
    assert gw.sessions[0].their_calls == ["GM4BRI"]
    assert world.run(60, lambda: sess.connection.state is State.DISCONNECTED)


def test_wrong_password_ends_with_login_failed_and_sends_nothing():
    world = World()
    box = Station([message(1)])
    gw, me, sess, ended = session(world, box, password="wrong")
    assert world.run(300, lambda: ended)
    assert sess.state == "failed" and sess.error == "login-failed"
    assert gw.received == [] and box.delivered_list == []


def test_no_password_stops_before_answering():
    world = World()
    gw, me, sess, ended = session(world, Station(), password="")
    assert world.run(300, lambda: ended)
    assert sess.error == "no-password"


def test_the_password_never_crosses_the_air():
    world = World()
    heard = bytearray()
    gw, me, sess, ended = session(world, Station([message(1)]), password="Sup3rSecret")
    gw.engine.monitors.append(lambda frame: heard.extend(bytes(frame.info or b"")))
    assert world.run(600, lambda: ended)
    assert sess.state == "failed"      # the gateway knows "secret", not this
    assert b"Sup3rSecret" not in heard and b";PR: " in heard


def test_a_gateway_that_already_has_a_message_says_so():
    world = World()
    box = Station([message(1)])
    gw, me, sess, ended = session(world, box)
    gw.received.append(message(1))
    assert world.run(600, lambda: ended)
    assert sess.state == "done" and box.delivered_list == [("MID000000001", "had-it")]
    assert len(gw.received) == 1


def test_damaged_transfer_is_a_protocol_failure_and_nothing_counts_as_received():
    box = Station()
    out = []
    s = b2f.Session("client", box, out.append, lambda: None, lambda: 0.0, mycall="GM4BRI", target="GB7RMS-10",
                    password=lambda: "secret")
    s.feed(b"[WL2K-5.0-B2FWIHJM$]\r;PQ: 23753528\rCMS via GB7RMS >\r")
    assert s.state == "their-turn" and out[-1] == b"FF\r"
    o = b2f.offer(message(5, to="GM4BRI"))
    s.feed((o.line() + "\r").encode() + f"F> {b2f.checksum([o.line()])}\r".encode())
    assert out[-1] == b"FS +\r" and s.state == "receiving"
    head = b"Test 5\x000\x00"
    blob = bytearray([b2f.SOH, len(head)]) + head + bytes([b2f.STX, len(o.data)]) + o.data
    blob += bytes([b2f.EOT, 0x00])          # the wrong checksum
    s.feed(bytes(blob))
    assert s.state == "failed" and s.error == "protocol" and box.inbox == []


def test_a_gateway_without_b2_is_left_politely():
    out = []
    s = b2f.Session("client", Station(), out.append, lambda: None, lambda: 0.0, mycall="GM4BRI",
                    target="GB7BBS", password=lambda: "x")
    s.feed(b"[FBB-7.0-FHM$]\rGB7BBS>\r")
    assert s.state == "failed" and s.error == "no-b2" and out == [b"FQ\r"]


def test_idle_gateway_times_out_but_not_while_we_are_still_sending():
    out, now = [], [0.0]
    s = b2f.Session("client", Station(), out.append, lambda: None, lambda: now[0], mycall="GM4BRI",
                    target="GB7RMS-10", password=lambda: "x")
    now[0] = b2f.HELLO_S + 1
    s.poll(now[0], sending=True)
    assert s.state == "hello"
    now[0] += b2f.IDLE_S + 1
    s.poll(now[0])
    assert s.state == "failed" and s.error == "timeout"


def test_the_simulators_gateway_takes_any_password_and_has_mail_waiting():
    world = World()
    box = Station()
    hello = message(7, to="GM4BRI")
    rms = world.station("GB7RMS")
    gw = b2f.Gateway(rms, ssid=10, accounts=None, welcome=lambda call: [hello])
    me = world.station("GM4BRI")
    ended = []
    b2f.call(me, "GB7RMS-10", box, mycall="GM4BRI", password=lambda: "anything", on_done=ended.append)
    assert world.run(600, lambda: ended)
    assert ended[0].state == "done" and [m.mid for m in box.inbox] == [hello.mid]
    assert gw.waiting["GM4BRI"] == []
