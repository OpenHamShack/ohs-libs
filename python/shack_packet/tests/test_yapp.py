"""YAPP and YAPP-C on the simulated channel: between two OHS stations, and
against a classic YAPP station played by this test from the frame bytes
in FBB's yapp.c (not from yapp.py), both ways."""

from __future__ import annotations

import random

from stations import World, accept_all, collect

from shack_packet.filexfer import FileService, Inbox
from shack_packet.yapp import ACK, CAN, ENQ, EOT, ETX, NAK, SOH, STX, YappHandler, resume_key


def _pair(tmp_path, world=None):
    world = world or World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    ev_a, ev_b = [], []
    fa = FileService(a, Inbox(tmp_path / "a"), on_event=lambda t, k, d: ev_a.append((k, t)))
    fb = FileService(b, Inbox(tmp_path / "b"), on_event=lambda t, k, d: ev_b.append((k, t)))
    YappHandler(fa)
    YappHandler(fb)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    got_a, _ = collect(conn)
    assert world.run(60, lambda: conn.connected and accepted)
    fa.attach(conn)
    fb.attach(accepted[0][0])
    fb.accepted = accepted                                      # for a second connection
    return world, fa, fb, conn, accepted[0], ev_a, ev_b, got_a


def _offers(ev):
    return [t for k, t in ev if k == "offer"]


def test_yapp_c_between_two_ohs_stations(tmp_path):
    world, fa, fb, conn, (far, got_b, _), ev_a, ev_b, _ = _pair(tmp_path)
    data = random.Random(1).randbytes(3000)
    t = fa.send_file(conn, tmp_path / "prog.zip", data=data, protocol="yapp")
    assert world.run(120, lambda: _offers(ev_b))
    inc = _offers(ev_b)[0]
    assert (inc.name, inc.size, inc.peer, inc.protocol) == ("prog.zip", 3000, "G0ABC", "yapp")
    fb.accept(inc)
    assert world.run(900, lambda: t.state == "done" and inc.state == "done")
    assert inc.path.read_bytes() == data
    assert fb.yapp.runs == {} and fa.yapp.runs == {}           # both streams are text again
    assert bytes(got_b) == b""
    # Chat after the transfer is plain text again at both ends.
    conn.send(b"got it?\r")
    assert world.run(60, lambda: bytes(got_b) == b"got it?\r")


def test_declined_with_nr_and_the_stream_is_text_again(tmp_path):
    world, fa, fb, conn, (far, got_b, _), ev_a, ev_b, got_a = _pair(tmp_path)
    t = fa.send_file(conn, tmp_path / "x.bin", data=b"abc" * 100, protocol="yapp")
    assert world.run(120, lambda: _offers(ev_b))
    fb.decline(_offers(ev_b)[0])
    assert world.run(120, lambda: t.state == "declined")
    assert world.run(60, lambda: not fb.yapp.runs)
    far.send(b"no thanks\r")
    assert world.run(60, lambda: bytes(got_a) == b"no thanks\r")


def test_a_dropped_yapp_transfer_resumes_with_re(tmp_path):
    world, fa, fb, conn, (far, _, _), ev_a, ev_b, _ = _pair(tmp_path)
    data = random.Random(2).randbytes(12_000)
    t = fa.send_file(conn, tmp_path / "pic.jpg", data=data, protocol="yapp")
    assert world.run(120, lambda: _offers(ev_b))
    inc = _offers(ev_b)[0]
    fb.accept(inc)
    assert world.run(900, lambda: inc.done > 4000)
    world.medium.hear("G0ABC", "GM4BRI", False)
    world.medium.hear("GM4BRI", "G0ABC", False)
    assert world.run(900, lambda: t.state == "interrupted" and inc.state == "interrupted")
    held = fb.inbox.partial(resume_key("G0ABC", "pic.jpg", 12_000), 12_000)[1]
    assert held >= 4000
    world.medium.hear("G0ABC", "GM4BRI", True)
    world.medium.hear("GM4BRI", "G0ABC", True)
    conn2 = fa.engine.connect("GM4BRI")
    assert world.run(60, lambda: conn2.connected and len(fb.accepted) == 2)
    fa.attach(conn2)
    fb.attach(fb.accepted[1][0])
    assert fa.resume(t)
    assert world.run(900, lambda: t.state == "done")
    done = [x for k, x in ev_b if k == "done"][-1]
    assert done.start_at == held and done.path.read_bytes() == data
    assert len(_offers(ev_b)) == 1                              # resumed without asking again


class ClassicPeer:
    """A station running other packet software, speaking YAPP from the
    bytes alone: what it receives is collected, what it sends is typed."""

    def __init__(self, world, conn):
        self.world = world
        self.conn = conn
        self.got = bytearray()
        conn.on_data = self.got.extend

    def expect(self, frame: bytes, seconds=120):
        ok = self.world.run(seconds, lambda: frame in self.got)
        assert ok, bytes(self.got)
        at = self.got.index(frame)
        del self.got[:at + len(frame)]

    def send(self, data: bytes):
        self.conn.send(data)


def test_receiving_plain_yapp_from_other_software(tmp_path):
    world = World()
    a = world.station("G0ABC")                  # the classic station
    b = world.station("GM4BRI")
    ev = []
    fb = FileService(b, Inbox(tmp_path / "b"), on_event=lambda t, k, d: ev.append((k, t)))
    YappHandler(fb)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected and accepted)
    fb.attach(accepted[0][0])
    peer = ClassicPeer(world, conn)
    data = random.Random(3).randbytes(600)
    peer.send(b"hello, file coming\r")
    peer.send(bytes([ENQ, 1]))
    peer.expect(bytes([ACK, 1]))                                # RR
    hd = b"FILE.BIN\x00" + b"600" + b"\x00"                     # no date: plain YAPP
    peer.send(bytes([SOH, len(hd)]) + hd)
    assert world.run(60, lambda: [t for k, t in ev if k == "offer"])
    inc = [t for k, t in ev if k == "offer"][0]
    fb.accept(inc)
    peer.expect(bytes([ACK, 2]))                                # RF: no checksums
    for at in range(0, 600, 256):
        chunk = data[at:at + 256]
        peer.send(bytes([STX, len(chunk) & 0xFF]) + chunk)       # 256 goes as length 0
    peer.send(bytes([ETX, 1]))
    peer.expect(bytes([ACK, 3]))                                # AF
    peer.send(bytes([EOT, 1]))
    peer.expect(bytes([ACK, 4]))                                # AT
    assert inc.state == "done" and inc.path.read_bytes() == data and inc.path.name == "FILE.BIN"
    assert inc.sha256                                           # worked out on arrival, for display


def test_sending_to_other_software_offers_yapp_c_and_finishes_on_rt(tmp_path):
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")                 # the classic station
    fa = FileService(a, Inbox(tmp_path / "a"))
    YappHandler(fa)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected and accepted)
    fa.attach(conn)
    peer = ClassicPeer(world, accepted[0][0])
    t = fa.send_file(conn, tmp_path / "log.adi", data=random.Random(4).randbytes(700), protocol="yapp")
    peer.expect(bytes([ENQ, 1]))
    peer.send(bytes([ACK, 1]))
    assert world.run(60, lambda: bytes([SOH]) in peer.got and len(peer.got) >= 2 + peer.got[1])
    fields = bytes(peer.got[2:2 + peer.got[1]]).split(b"\x00")
    assert fields[0] == b"log.adi" and int(fields[1]) == 700
    assert len(fields[2]) == 8 and int(fields[2], 16) >= 0         # the DOS date and time: YAPP-C capable
    peer.got.clear()
    peer.send(bytes([ACK, ACK]))                                # RT
    peer.expect(bytes([ETX, 1]), seconds=300)
    peer.send(bytes([ACK, 3]))
    peer.expect(bytes([EOT, 1]))
    peer.send(bytes([ACK, 4]))
    assert world.run(60, lambda: t.state == "done")
    assert not fa.yapp.runs


def test_a_sender_hearing_text_instead_of_at_is_done_and_text_again(tmp_path):
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    fa = FileService(a, Inbox(tmp_path / "a"))
    YappHandler(fa)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    got_a, _ = collect(conn)
    assert world.run(60, lambda: conn.connected and accepted)
    fa.attach(conn)
    peer = ClassicPeer(world, accepted[0][0])
    t = fa.send_file(conn, tmp_path / "x.txt", data=b"12345", protocol="yapp")
    peer.expect(bytes([ENQ, 1]))
    peer.send(bytes([ACK, 1]))
    assert world.run(60, lambda: bytes([SOH]) in peer.got)
    peer.send(bytes([ACK, 2]))
    peer.expect(bytes([ETX, 1]))
    peer.send(bytes([ACK, 3]))
    peer.expect(bytes([EOT, 1]))
    peer.send(b"thanks!\r")
    assert world.run(60, lambda: t.state == "done" and bytes(got_a) == b"thanks!\r")


def test_dt_frames_carry_the_yapp_c_sum(tmp_path):
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    fa = FileService(a, Inbox(tmp_path / "a"))
    YappHandler(fa)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected and accepted)
    fa.attach(conn)
    peer = ClassicPeer(world, accepted[0][0])
    data = random.Random(6).randbytes(520)
    t = fa.send_file(conn, tmp_path / "f.bin", data=data, protocol="yapp")
    peer.expect(bytes([ENQ, 1]))
    peer.send(bytes([ACK, 1]))
    assert world.run(60, lambda: bytes([SOH]) in peer.got and len(peer.got) >= 2 + peer.got[1])
    del peer.got[:2 + peer.got[1]]
    peer.send(bytes([NAK, 8]) + b"R\x00" + b"100\x00" + b"C\x00")   # resume at 100, YAPP-C
    assert world.run(300, lambda: bytes([ETX, 1]) in peer.got)
    stream = bytes(peer.got[:peer.got.index(bytes([ETX, 1]))])
    out = bytearray()
    i = 0
    while i < len(stream):
        assert stream[i] == STX
        n = stream[i + 1] or 256
        chunk = stream[i + 2:i + 2 + n]
        assert stream[i + 2 + n] == sum(chunk) & 0xFF
        out += chunk
        i += 3 + n
    assert bytes(out) == data[100:]
    peer.got.clear()
    peer.send(bytes([ACK, 3]))
    peer.expect(bytes([EOT, 1]))
    peer.send(bytes([ACK, 4]))
    assert world.run(60, lambda: t.state == "done")


def test_a_bad_checksum_cancels(tmp_path):
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    ev = []
    fb = FileService(b, Inbox(tmp_path / "b"), on_event=lambda t, k, d: ev.append((k, t)))
    YappHandler(fb)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected and accepted)
    fb.attach(accepted[0][0])
    peer = ClassicPeer(world, conn)
    peer.send(bytes([ENQ, 1]))
    peer.expect(bytes([ACK, 1]))
    hd = b"a.txt\x00" + b"10\x00" + b"5A3C6B21\x00"           # with a date: YAPP-C
    peer.send(bytes([SOH, len(hd)]) + hd)
    assert world.run(60, lambda: [t for k, t in ev if k == "offer"])
    inc = [t for k, t in ev if k == "offer"][0]
    fb.accept(inc)
    peer.expect(bytes([ACK, ACK]))                              # RT
    peer.send(bytes([STX, 10]) + b"0123456789" + bytes([0]))    # wrong sum
    peer.expect(bytes([CAN]))
    assert world.run(30, lambda: inc.state == "failed") and inc.reason == "protocol"
    peer.send(bytes([ACK, 5]))
    assert world.run(30, lambda: not fb.yapp.runs)


def test_text_after_a_file_without_et_is_text_again(tmp_path):
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    ev = []
    fb = FileService(b, Inbox(tmp_path / "b"), on_event=lambda t, k, d: ev.append((k, t)))
    YappHandler(fb)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected and accepted)
    far, got_b, _ = accepted[0]
    fb.attach(far)
    peer = ClassicPeer(world, conn)
    peer.send(bytes([ENQ, 1]))
    peer.expect(bytes([ACK, 1]))
    hd = b"n.txt\x00" + b"5\x00"
    peer.send(bytes([SOH, len(hd)]) + hd)
    assert world.run(60, lambda: [t for k, t in ev if k == "offer"])
    fb.accept([t for k, t in ev if k == "offer"][0])
    peer.expect(bytes([ACK, 2]))
    peer.send(bytes([STX, 5]) + b"hello" + bytes([ETX, 1]))
    peer.expect(bytes([ACK, 3]))
    peer.send(b"did it arrive?\r")                              # no ET: straight back to typing
    assert world.run(60, lambda: bytes(got_b) == b"did it arrive?\r")
    assert not fb.yapp.runs
