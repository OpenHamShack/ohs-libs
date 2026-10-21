"""The FBB exchange's pieces, line by line (FORWARDING.md): SIDs as real
software sends them, the line splitter, and two Exchanges talking to each
other with no radio at all."""

from __future__ import annotations

from shack_packet import fbb


def test_sids_from_real_software():
    assert fbb.parse_sid("[BPQ-6.0.24.1-B1FWIHJM$]") == fbb.Sid("BPQ", "6.0.24.1", "B1FWIHJM$")
    assert fbb.parse_sid("[WL2K-5.0-B2FWIHJM$]").forwards
    assert fbb.parse_sid("[FBB-7.00-ABFHM$]").forwards
    assert fbb.parse_sid(fbb.sid("0.3.0")) == fbb.Sid("OHS", "0.3.0", "FHM$")
    assert not fbb.parse_sid("[XYZ-1.0-$]").forwards
    assert fbb.parse_sid("[not a sid") is None and fbb.parse_sid("hello") is None


def test_line_buffer_ends_and_ctrl_z():
    lb = fbb.LineBuffer()
    assert lb.feed(b"one\rtwo\r\nthree\nfo") == ["one", "two", "three"]
    assert lb.feed(b"ur\r\n") == ["four"]
    assert lb.feed(b"last line\x1a\r") == ["last line\x1a"]
    assert lb.feed(b"\x1a\r\nnext\r") == ["\x1a", "next"]
    assert lb.feed("caf\xe9\r".encode("latin-1")) == ["caf\xe9"]


def test_proposals_and_checksum():
    p = fbb.parse_proposal("FB P G0ABC GM4BRI G4XYZ 17_G0ABC 112")
    assert p == fbb.Proposal("P", "G0ABC", "GM4BRI", "G4XYZ", "17_G0ABC", 112)
    assert fbb.parse_proposal(p.line()) == p
    assert fbb.parse_proposal("FB P G0ABC GM4BRI G4XYZ bad/bid 1") is None
    lines = [p.line()]
    total = sum((lines[0] + "\r").encode())
    assert (int(fbb.block_checksum(lines), 16) + total) & 0xFF == 0


class Box:
    def __init__(self, outbox=(), answer="+"):
        self.outbox, self.inbox, self.done, self._answer = list(outbox), [], [], answer

    def outgoing(self, peer):
        return [m for m in self.outbox if m not in self.done]

    def answer(self, peer, p):
        return self._answer

    def received(self, peer, msg):
        self.inbox.append(msg)

    def delivered(self, peer, out, how):
        self.done.append(out)


def pair(a_box, b_box):
    """Two exchanges wired back to back: what one sends, the other reads."""
    wires = {"a": [], "b": []}
    closed = []
    a = fbb.Exchange("caller", "B", a_box, wires["b"].append, lambda: closed.append("a"), lambda: 0.0)
    b = fbb.Exchange("answerer", "A", b_box, wires["a"].append, lambda: closed.append("b"), lambda: 0.0)
    for line in ("[OHS-0.3.0-FHM$]", "Hello", "de B>"):
        a.line(line)

    def pump():
        for _ in range(1000):
            if not wires["a"] and not wires["b"]:
                return
            while wires["b"]:
                b.line(wires["b"].pop(0))
            while wires["a"]:
                a.line(wires["a"].pop(0))
    pump()
    return a, b, closed


def msg(n, subject="s", body="text"):
    return fbb.OutMessage(fbb.Proposal("P", "G0ABC", "GM4BRI", "G4XYZ", f"{n}_G0ABC", len(body)), subject, body)


def test_more_than_a_block_each_way_and_a_clean_end():
    a_box = Box([msg(i, f"a{i}") for i in range(7)])
    b_box = Box([msg(100 + i, f"b{i}") for i in range(2)])
    a, b, closed = pair(a_box, b_box)
    assert a.state == b.state == "done", (a.error, b.error)
    assert [m.subject for m in b_box.inbox] == [f"a{i}" for i in range(7)]
    assert [m.subject for m in a_box.inbox] == ["b0", "b1"]
    assert len(a_box.done) == 7 and len(b_box.done) == 2
    assert closed                                      # FQ's sender closed


def test_deferred_and_refused_are_not_proposed_again_in_one_session():
    a_box = Box([msg(1), msg(2)])
    b_box = Box(answer="=")
    a, b, _ = pair(a_box, b_box)
    assert a.state == "done" and a.result.deferred == 2 and a_box.done == []
    a_box2 = Box([msg(1)])
    a2, _, _ = pair(a_box2, Box(answer="-"))
    assert a2.result.refused == 1 and a_box2.done == a_box2.outbox   # "-" means they have it


def test_a_lost_link_leaves_messages_undelivered():
    a_box = Box([msg(1)])
    sent = []
    a = fbb.Exchange("caller", "B", a_box, sent.append, lambda: None, lambda: 0.0)
    for line in ("[OHS-0.3.0-FHM$]", "de B>", "FS +"):
        a.line(line)
    assert "\x1a" in sent
    a.lost()
    assert a.state == "failed" and a_box.done == []


def test_nonsense_is_a_protocol_error():
    sent, closed = [], []
    b = fbb.Exchange("answerer", "A", Box(), sent.append, lambda: closed.append(1), lambda: 0.0,
                     their_sid=fbb.parse_sid("[OHS-0.3.0-FHM$]"))
    b.line("hello there")
    assert b.state == "failed" and sent[-1] == "*** Protocol error" and closed


def test_idle_time_out():
    now = [0.0]
    b = fbb.Exchange("answerer", "A", Box(), lambda line: None, lambda: None, lambda: now[0],
                     their_sid=fbb.parse_sid("[OHS-0.3.0-FHM$]"))
    now[0] = fbb.IDLE_S + 1
    b.poll(now[0])
    assert b.state == "failed" and "in time" in b.error


def test_a_non_forwarding_bbs_is_left_politely():
    sent = []
    a = fbb.Exchange("caller", "B", Box(), sent.append, lambda: None, lambda: 0.0)
    a.line("[XYZ-1.0-$]")
    a.line("de B>")
    assert sent == ["B"] and a.state == "failed"
