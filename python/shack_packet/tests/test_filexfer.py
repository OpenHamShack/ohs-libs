"""File transfer between OHS stations over connected mode, on the simulated
channel: offers, compression, cancel, a dropped link resumed, a damaged
part caught by the hash, loss and latency, a fast mode with a Pluto's
timing, and the text that shares the session."""

from __future__ import annotations

import hashlib
import random

import pytest
from stations import World, accept_all, collect

from shack_packet import modem
from shack_packet import ohssession as oh
from shack_packet.channel import ChannelConfig
from shack_packet.datalink import LinkConfig
from shack_packet.filexfer import FilePolicy, FileService, Inbox, encrypted_reason, safe_name, worth_compressing
from shack_packet.sim import PLUTO


def _text(size, seed=1):
    rng = random.Random(seed)
    words = ["CQ", "de", "GM4BRI", "the", "packet", "station", "is", "QRV", "on", "2m", "and", "70cm", "73",
             "rig", "antenna", "report", "readable", "59", "thanks", "for", "the", "contact"]
    out = []
    n = 0
    while n < size:
        w = rng.choice(words)
        out.append(w)
        n += len(w) + 1
    return (" ".join(out)[:size]).encode()


class Pair:
    """Two OHS stations, connected, each with a file service and an inbox."""

    def __init__(self, tmp_path, world=None, *, policy_b=None, a_call="G0ABC", b_call="GM4BRI", **station):
        self.world = world or World()
        self.a = self.world.station(a_call, **station)
        self.b = self.world.station(b_call, **station)
        self.events_a, self.events_b = [], []
        #: (event, bytes done then): the transfers themselves move on.
        self.done_a, self.done_b = [], []

        def heard(events, done):
            def on_event(t, kind, detail):
                events.append((kind, t))
                done.append((kind, t.done if t is not None else None))
            return on_event
        self.fa = FileService(self.a, Inbox(tmp_path / "a"), app="test A", on_event=heard(self.events_a, self.done_a))
        self.fb = FileService(self.b, Inbox(tmp_path / "b"), app="test B", policy=policy_b,
                              on_event=heard(self.events_b, self.done_b))
        self.accepted = accept_all(self.b)
        self.b_call = b_call
        self.connect()

    def connect(self):
        self.conn = self.a.connect(self.b_call)
        self.got_a, _ = collect(self.conn)
        before = len(self.accepted)
        assert self.world.run(60, lambda: self.conn.connected and len(self.accepted) > before)
        self.far, self.got_b, _ = self.accepted[-1]
        self.fa.attach(self.conn)
        self.fb.attach(self.far)

    def run(self, seconds, until):
        return self.world.run(seconds, until)

    def offers(self):
        return [t for k, t in self.events_b if k == "offer"]


def test_a_text_file_goes_compressed_and_arrives_checked(tmp_path):
    p = Pair(tmp_path)
    src = tmp_path / "log.txt"
    data = _text(20_000)
    src.write_bytes(data)
    t = p.fa.send_file(p.conn, src)
    assert p.run(60, lambda: p.offers())
    offer = p.offers()[0]
    assert (offer.name, offer.size, offer.peer, offer.mime) == ("log.txt", 20_000, "G0ABC", "text/plain")
    assert offer.sha256 == hashlib.sha256(data).hexdigest()
    assert t.state == "offering" and not p.events_b[-1][1].path        # nothing written before the answer
    p.fb.accept(offer)
    assert p.run(600, lambda: t.state == "done")
    assert offer.state == "done"
    assert offer.path.read_bytes() == data and offer.path.parent == tmp_path / "b"
    assert t.compress and t.wire_bytes < len(data) * 0.6
    # Progress was seen at both ends, and the sender's came from the ACKs.
    assert sum(1 for k, done in p.done_a if k == "progress" and 0 < done < len(data)) >= 1
    assert sum(1 for k, done in p.done_b if k == "progress" and 0 < done < len(data)) > 5
    assert not (tmp_path / "b" / ".partial").exists() or not any((tmp_path / "b" / ".partial").iterdir())


def test_a_packed_file_is_not_compressed_and_still_arrives(tmp_path):
    p = Pair(tmp_path)
    data = b"\xff\xd8\xff\xe0" + random.Random(3).randbytes(6000)
    t = p.fa.send_file(p.conn, tmp_path / "photo.jpg", data=data)
    assert not t.compress
    assert p.run(60, lambda: p.offers())
    p.fb.accept(p.offers()[0])
    assert p.run(600, lambda: t.state == "done")
    assert p.offers()[0].path.read_bytes() == data
    assert p.offers()[0].mime == "image/jpeg"


def test_decline_and_too_big(tmp_path):
    p = Pair(tmp_path, policy_b=FilePolicy(max_size=1000))
    small = p.fa.send_file(p.conn, tmp_path / "a.txt", data=b"x" * 100)
    assert p.run(60, lambda: p.offers())
    p.fb.decline(p.offers()[0])
    assert p.run(60, lambda: small.state == "declined") and small.reason == "declined"
    big = p.fa.send_file(p.conn, tmp_path / "b.bin", data=b"y" * 5000)
    assert p.run(60, lambda: big.state == "declined") and big.reason == "too-big"
    assert not list((tmp_path / "b").glob("*.*"))


def test_an_encrypted_file_is_refused_before_anything_is_sent(tmp_path):
    p = Pair(tmp_path)
    sent = p.conn.sent_bytes
    for data in (b"Salted__" + bytes(40), b"-----BEGIN PGP MESSAGE-----\n\nhQEMA...",
                 b"age-encryption.org/v1\n-> X25519 abc"):
        with pytest.raises(ValueError, match="encrypted"):
            p.fa.send_file(p.conn, tmp_path / "secret.bin", data=data)
    assert p.conn.sent_bytes == sent and not p.fa.transfers


def test_encrypted_zip_entries_are_recognised():
    def local(flags, name=b"a.txt", body=b"hello"):
        return (b"PK\x03\x04" + (20).to_bytes(2, "little") + flags.to_bytes(2, "little") + bytes(10)
                + len(body).to_bytes(4, "little") + len(body).to_bytes(4, "little")
                + len(name).to_bytes(2, "little") + bytes(2) + name + body)
    assert encrypted_reason(local(0) + local(0, b"b.txt")) is None
    assert encrypted_reason(local(0) + local(1, b"b.txt")) == "encrypted"
    assert encrypted_reason(b"plain text, nothing hidden") is None


def test_the_receiver_cancels_and_the_sender_hears_it(tmp_path):
    p = Pair(tmp_path)
    t = p.fa.send_file(p.conn, tmp_path / "r.bin", data=random.Random(1).randbytes(40_000))
    assert p.run(60, lambda: p.offers())
    inc = p.offers()[0]
    p.fb.accept(inc)
    assert p.run(600, lambda: inc.done > 4000)
    p.fb.cancel(inc)
    assert p.run(60, lambda: t.state == "cancelled") and t.reason == "peer-cancelled"
    assert inc.state == "cancelled"
    # What arrived is kept for a resume, not moved into the folder.
    assert Inbox(tmp_path / "b").partial(inc.sha256, inc.size)[1] == inc.done


def _deafen(world, a, b, deaf=True):
    world.medium.hear(a, b, not deaf)
    world.medium.hear(b, a, not deaf)


def test_a_dropped_link_resumes_where_it_stopped_and_the_hash_checks_it_all(tmp_path):
    p = Pair(tmp_path)
    data = random.Random(7).randbytes(30_000)
    t = p.fa.send_file(p.conn, tmp_path / "big.bin", data=data)
    assert p.run(60, lambda: p.offers())
    first = p.offers()[0]
    p.fb.accept(first)
    assert p.run(900, lambda: first.done > 10_000)
    _deafen(p.world, "G0ABC", "GM4BRI")
    assert p.run(900, lambda: t.state == "interrupted" and first.state == "interrupted")
    assert t.reason == "link-lost" and t.resumable
    held = Inbox(tmp_path / "b").partial(first.sha256, first.size)[1]
    assert 10_000 <= held < len(data)
    _deafen(p.world, "G0ABC", "GM4BRI", deaf=False)
    # Connecting again is all it takes: both ends know each other as OHS
    # stations now, the HELLO goes out by itself, the sender offers again
    # and the receiver, holding part of that file, takes it at its offset
    # without asking its operator a second time.
    p.connect()
    assert p.run(900, lambda: t.state == "done")
    second = [x for k, x in p.events_b if k == "done"][-1]
    assert second.start_at == held and second.state == "done"
    assert second.path.read_bytes() == data
    assert sum(1 for k, _ in p.events_b if k == "offer") == 1       # asked once, resumed silently


def test_a_damaged_part_is_caught_by_the_hash_and_thrown_away(tmp_path):
    p = Pair(tmp_path)
    data = _text(12_000, seed=4)
    t = p.fa.send_file(p.conn, tmp_path / "notes.txt", data=data)
    assert p.run(60, lambda: p.offers())
    inc = p.offers()[0]
    p.fb.accept(inc)
    assert p.run(600, lambda: inc.done > 3000)
    p.fb.cancel(inc)
    assert p.run(60, lambda: t.state == "cancelled")
    part, have = Inbox(tmp_path / "b").partial(inc.sha256, inc.size)
    raw = bytearray(part.read_bytes())
    raw[100] ^= 0xFF
    part.write_bytes(bytes(raw))
    assert p.fa.resume(t)
    assert p.run(600, lambda: t.state == "failed")
    assert t.reason == "hash-mismatch"
    assert Inbox(tmp_path / "b").partial(inc.sha256, inc.size) is None
    # Sending again starts from nothing and arrives whole.
    assert p.fa.resume(t)
    assert p.run(60, lambda: len(p.offers()) > 1)
    again = p.offers()[-1]
    assert again.start_at == 0
    p.fb.accept(again)
    assert p.run(600, lambda: t.state == "done") and again.path.read_bytes() == data


def test_through_loss_and_latency(tmp_path):
    world = World(loss=0.1, seed=11, delay_s=0.4)
    p = Pair(tmp_path, world)
    data = random.Random(2).randbytes(8000)
    t = p.fa.send_file(p.conn, tmp_path / "lossy.bin", data=data)
    assert p.run(300, lambda: p.offers())
    p.fb.accept(p.offers()[0])
    assert p.run(3600, lambda: t.state == "done")
    assert p.offers()[0].path.read_bytes() == data


def test_a_fast_mode_with_a_plutos_timing_moves_a_picture_quickly(tmp_path):
    m = modem.mode("gfsk153600")
    world = World(baud=m.bitrate, dcd_lag_s=0.01, min_lock_s=m.lock_bits / m.bitrate)
    channel = ChannelConfig(txdelay_s=0.03, txtail_s=0.01, persist=255, slottime_s=0.05, max_frames=33,
                            max_tx_s=6.0, mode=m.name)
    a = world.medium.radio("G0ABC", timing=PLUTO, turnaround_s=0.03)
    b = world.medium.radio("GM4BRI", timing=PLUTO, turnaround_s=0.03)
    from shack_packet.engine import Engine
    from shack_packet.identity import Station
    link = LinkConfig(k=32, n1=512, modulo=128, srej=True)
    ea = Engine(Station.of("G0ABC", tx_enabled=True), a, channel=channel, link=link, clock=world.clock,
                rng=random.Random(1))
    eb = Engine(Station.of("GM4BRI", tx_enabled=True), b, channel=channel, link=link, clock=world.clock,
                rng=random.Random(2))
    world.sim.add(ea)
    world.sim.add(eb)
    events = []
    fa = FileService(ea, Inbox(tmp_path / "a"))
    fb = FileService(eb, Inbox(tmp_path / "b"), on_event=lambda t, k, d: events.append((k, t)))
    accepted = accept_all(eb)
    conn = ea.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected and accepted)
    fa.attach(conn)
    fb.attach(accepted[0][0])
    data = b"\xff\xd8\xff\xe0" + random.Random(5).randbytes(200_000)
    t = fa.send_file(conn, tmp_path / "shack.jpg", data=data)
    assert world.run(30, lambda: any(k == "offer" for k, _ in events))
    inc = [x for k, x in events if k == "offer"][0]
    fb.accept(inc)
    assert world.run(120, lambda: t.state == "done")
    assert inc.path.read_bytes() == data
    rate = inc.rate_bps(world.clock.now)
    # 200 KB at 153.6 kbit/s: tens of kbit/s of file data even with the
    # Pluto's turnaround, against about 0.8 kbit/s at 1200 baud.
    assert rate > 40_000, rate


def test_chat_text_shares_the_session_and_long_text_is_compressed_once_both_say_so(tmp_path):
    p = Pair(tmp_path)
    p.conn.send(b"hello before anything\r")
    assert p.run(30, lambda: bytes(p.got_b).endswith(b"anything\r"))
    long = _text(400, seed=9) + b"\r"
    p.conn.send(long)
    assert p.run(30, lambda: bytes(p.got_b).endswith(long))
    assert p.conn.ohs.text_sent_bytes == p.conn.ohs.text_raw_bytes     # no HELLO yet: plain text
    t = p.fa.send_file(p.conn, tmp_path / "f.txt", data=_text(5000))
    assert p.run(60, lambda: p.offers() and p.conn.ohs.peer_is_ohs)
    p.fb.accept(p.offers()[0])
    p.got_b.clear()
    p.far.send(b"a line while the file goes\r")
    p.conn.send(long)
    assert p.run(600, lambda: t.state == "done" and bytes(p.got_b).endswith(long))
    assert bytes(p.got_b) == long                                      # no frame leaked into the text
    assert bytes(p.got_a).endswith(b"a line while the file goes\r")
    assert p.conn.ohs.text_sent_bytes < p.conn.ohs.text_raw_bytes


def test_offering_to_a_station_without_ohs_gives_up_and_says_try_yapp(tmp_path):
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    fa = FileService(a, Inbox(tmp_path / "a"))
    accepted = accept_all(b)                                   # a plain station: no file service
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected and accepted)
    t = fa.send_file(conn, tmp_path / "x.txt", data=b"hello")
    assert world.run(300, lambda: t.state == "failed")
    assert t.reason == "no-answer"


def test_the_demux_finds_frames_cut_anywhere_and_leaves_text_alone():
    stream = (b"text one\r\x01 not a frame\r" + oh.jframe(oh.HELLO, {"v": 1}) + b"between\r"
              + oh.frame(oh.DATA, bytes(range(256)) * 3) + b"tail \x01O")
    for cut in (1, 3, 7, 50):
        d = oh.Demux()
        items = []
        for at in range(0, len(stream), cut):
            items += d.feed(stream[at:at + cut])
        text = b"".join(i[1] for i in items if i[0] == "text")
        frames = [(i[1], i[2]) for i in items if i[0] == "frame"]
        assert text == b"text one\r\x01 not a frame\rbetween\rtail "
        assert [k for k, _ in frames] == [oh.HELLO, oh.DATA]
        assert frames[1][1] == bytes(range(256)) * 3


@pytest.mark.parametrize("name,safe", [
    ("../../etc/passwd", "passwd"), ("C:\\Windows\\evil.exe", "evil.exe"), (".bashrc", "bashrc"),
    ("CON.txt", "_CON.txt"), ("a\x00b\x1fc.txt", "a_b_c.txt"), ("", "received"), ("..", "received"),
    ("photo of the shack.jpg", "photo of the shack.jpg"),
])
def test_received_names_are_made_safe(name, safe):
    assert safe_name(name) == safe


def test_a_name_already_taken_gets_a_number(tmp_path):
    p = Pair(tmp_path)
    for body in (b"first", b"second"):
        t = p.fa.send_file(p.conn, tmp_path / "same.txt", data=body)
        assert p.run(60, lambda: p.offers() and p.offers()[-1].state == "offered")
        p.fb.accept(p.offers()[-1])
        assert p.run(300, lambda: t.state == "done")
    assert (tmp_path / "b" / "same.txt").read_bytes() == b"first"
    assert (tmp_path / "b" / "same (2).txt").read_bytes() == b"second"


def test_compression_is_skipped_for_what_will_not_shrink():
    assert worth_compressing("log.txt", _text(3000))
    assert not worth_compressing("x.bin", random.Random(1).randbytes(3000))
    assert not worth_compressing("archive.zip", _text(3000))


def test_disconnecting_stops_the_file_rather_than_waiting_for_it(tmp_path):
    p = Pair(tmp_path)
    data = random.Random(8).randbytes(40_000)
    t = p.fa.send_file(p.conn, tmp_path / "long.bin", data=data)
    assert p.run(60, lambda: p.offers())
    p.fb.accept(p.offers()[0])
    assert p.run(900, lambda: t.sent > 5000)
    p.conn.close()
    assert p.run(300, lambda: t.state == "interrupted")
    assert t.resumable and t.sent < len(data)


def test_a_window_of_two_overs_at_9600_is_not_talked_over(tmp_path):
    """k 63, PACLEN 512 at 9600 (Packet Radio 0.3.0-0.3.1's settings): each
    window is two 10 s overs back to back. The receiver used to take that
    for a stuck squelch at 15 s and transmit into the second over; now it
    waits out its link partner's over (channel.PARTNER_MAX_WAIT_S). 100 KB
    took 178 s simulated before, about 104 s now."""
    world = World(modem_mode="g3ruh9600")
    m = modem.mode("g3ruh9600")
    channel = ChannelConfig(txdelay_s=0.0, mode=m.name, max_frames=64)
    link = LinkConfig(k=63, n1=512, modulo=128, srej=True)
    engines = []
    from shack_packet.engine import Engine
    from shack_packet.identity import Station
    for call, seed in (("G0ABC", 1), ("GM4BRI", 2)):
        radio = world.medium.radio(call, timing=PLUTO, turnaround_s=0.1)
        e = Engine(Station.of(call, tx_enabled=True), radio, channel=channel, link=link, clock=world.clock,
                   rng=random.Random(seed))
        world.sim.add(e)
        engines.append(e)
    ea, eb = engines
    events = []
    fa = FileService(ea, Inbox(tmp_path / "a"))
    fb = FileService(eb, Inbox(tmp_path / "b"), on_event=lambda t, k, d: events.append((k, t)))
    accepted = accept_all(eb)
    conn = ea.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected and accepted)
    fa.attach(conn)
    fb.attach(accepted[0][0])
    data = b"\xff\xd8\xff\xe0" + random.Random(5).randbytes(100 * 1024 - 4)
    t = fa.send_file(conn, tmp_path / "shack.jpg", data=data)
    assert world.run(30, lambda: any(k == "offer" for k, _ in events))
    start = world.clock.now
    fb.accept([x for k, x in events if k == "offer"][0])
    assert world.run(600, lambda: t.state == "done")
    assert ea.channel.stats.busy_overrides == eb.channel.stats.busy_overrides == 0
    assert world.clock.now - start < 125, world.clock.now - start
