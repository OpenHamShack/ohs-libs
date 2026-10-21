"""The BBS, used over the simulated channel as callers would: two stations
connect to it through their own engines, send and read mail, meet in the
conference room, and an OpenHamShack station delivers and collects mail
by FBB forwarding. Frames mode, so minutes of traffic run in seconds."""

from __future__ import annotations

from stations import World

from shack_packet import fbb
from shack_packet.bbs import Bbs, BbsConfig, Partner
from shack_packet.bbsstore import DAY, BbsStore, Retention
from shack_packet.conference import Room, RoomConfig, RoomService
from shack_packet.datalink import State
from shack_packet.idbeacon import IdBeacon

T0 = 1_790_000_000.0


class Caller:
    """A station typing at the BBS and reading what comes back."""

    def __init__(self, world, engine, target):
        self.world = world
        self.conn = engine.connect(target)
        self.text = bytearray()
        self.conn.on_data = self.text.extend

    def wait_for(self, words, seconds=120):
        ok = self.world.run(seconds, lambda: words.encode() in self.text)
        assert ok, bytes(self.text).decode(errors="replace")[-600:]

    def type(self, line, then=None):
        self.text.clear()
        self.conn.send((line + "\r").encode())
        if then is not None:
            self.wait_for(then)

    def screen(self):
        return bytes(self.text).decode()


def make(partners=(), **config):
    world = World()
    box = world.station("GM4BRI")
    wall = [T0]
    store = BbsStore(home="GM4BRI", clock=lambda: wall[0])
    room = Room("GM4BRI-2", clock=box.clock)
    bbs = Bbs(box, store, BbsConfig(ssid=1, partners=list(partners), **config), room=room,
              wall_clock=lambda: wall[0])
    RoomService(box, room, 2)
    return world, box, store, bbs, room, wall


PROMPT = "de GM4BRI-1>"


def test_two_stations_leave_and_read_mail():
    world, box, store, bbs, room, wall = make()
    a = Caller(world, world.station("G0ABC-9"), "GM4BRI-1")
    a.wait_for(PROMPT)
    assert "[OHS-0.3.0-FHM$]" in a.screen()
    assert "You have 0 new messages" in a.screen()
    a.type("N Alan", then=PROMPT)
    a.type("SP G4XYZ", then="Subject:")
    a.type("Sked", then="/EX")
    a.type("2 m at 2000?")
    a.type("/ex", then=PROMPT)
    assert "saved, for G4XYZ @ GM4BRI" in a.screen()
    a.type("SB ALL @ GBR", then="Subject:")
    a.type("Net tonight", then="/EX")
    a.type("Sunday 1900 on GB3XX\x1a", then=PROMPT)
    a.type("B", then="goodbye")
    assert world.run(60, lambda: a.conn.state is State.DISCONNECTED)

    b = Caller(world, world.station("G4XYZ"), "GM4BRI-1")
    b.wait_for(PROMPT)
    assert "You have 1 new messages" in b.screen()
    b.type("L", then=PROMPT)
    assert "Sked" in b.screen() and "Net tonight" in b.screen()
    b.type("L", then=PROMPT)
    assert "No new messages" in b.screen()
    b.type("LM", then=PROMPT)
    assert "PN" in b.screen() and "G0ABC" in b.screen()
    b.type("R 1", then=PROMPT)
    assert "Title: Sked" in b.screen() and "2 m at 2000?" in b.screen() and "From: G0ABC" in b.screen()
    assert store.get(1).state == "read"
    b.type("LB", then=PROMPT)
    assert "Net tonight" in b.screen() and "Sked" not in b.screen()
    b.type("I G0ABC", then=PROMPT)
    assert "G0ABC: Alan" in b.screen()
    b.type("K 2", then=PROMPT)
    assert "Only its sender can kill" in b.screen()
    b.type("KM", then=PROMPT)
    assert "1 read messages killed" in b.screen() and store.get(1) is None
    b.type("BYE", then="goodbye")


def test_personal_mail_is_private_and_limits_hold():
    world, box, store, bbs, room, wall = make(max_per_day=1, max_message_bytes=40)
    store.post("P", "G4XYZ", "M0DEF", "GM4BRI", "Private", "for M0DEF only")
    a = Caller(world, world.station("G0ABC"), "GM4BRI-1")
    a.wait_for(PROMPT)
    a.type("R 1", then=PROMPT)
    assert "There is no message 1 for you" in a.screen()
    a.type("LL 5", then=PROMPT)
    assert "Private" not in a.screen()
    a.type("S G4XYZ", then="Subject:")
    a.type("Long", then="/EX")
    a.type("x" * 60, then=PROMPT)
    assert "too long" in a.screen()
    a.type("S G4XYZ", then="Subject:")
    a.type("Short", then="/EX")
    a.type("hi")
    a.type("/EX", then=PROMPT)
    a.type("S G4XYZ", then=PROMPT)
    assert "as many messages today" in a.screen()
    a.type("SB TOOLONGTOPIC", then=PROMPT)
    assert "six letters" in a.screen()


def test_a_barred_call_is_refused_and_an_idle_one_dropped():
    world, box, store, bbs, room, wall = make(idle_timeout_s=60)
    store.update_user("G0BAD", barred=True)
    bad = Caller(world, world.station("G0BAD-1"), "GM4BRI-1")
    assert world.run(60, lambda: bad.conn.state is State.DISCONNECTED)
    assert PROMPT not in bad.screen()
    a = Caller(world, world.station("G0ABC"), "GM4BRI-1")
    a.wait_for(PROMPT)
    assert world.run(200, lambda: a.conn.state is State.DISCONNECTED)
    assert "No input for a while" in a.screen()


def test_conference_from_the_bbs_and_by_its_own_ssid():
    world, box, store, bbs, room, wall = make()
    a = Caller(world, world.station("G0ABC"), "GM4BRI-1")
    a.wait_for(PROMPT)
    a.type("C", then="/help")
    assert "Welcome to the GM4BRI-2 conference. 1 here: G0ABC" in a.screen()
    b = Caller(world, world.station("G4XYZ-7"), "GM4BRI-2")
    b.wait_for("/help")
    assert "2 here" in b.screen()
    a.wait_for("*** G4XYZ-7 joined")
    a.type("anyone on 23 cm tonight?")
    b.wait_for("G0ABC: anyone on 23 cm tonight?")
    assert "anyone on 23 cm" not in a.screen()       # own lines are not echoed
    b.type("/me waves", then=None)
    a.wait_for("* G4XYZ-7 waves")
    b.type("/topic Sunday net")
    a.wait_for("set the topic: Sunday net")
    a.type("/who", then="G0ABC G4XYZ-7")
    room.say("hello from the sysop")
    b.wait_for("SYSOP: hello from the sysop")
    a.type("/quit", then=PROMPT)
    assert "Back at the BBS" in a.screen()
    b.wait_for("*** G0ABC left")
    b.type("/q")
    assert world.run(60, lambda: b.conn.state is State.DISCONNECTED)
    assert room.members == []


def test_room_flood_and_history():
    world, box, store, bbs, room, wall = make()
    room.config = RoomConfig(flood_lines=3, flood_s=60, history=2)
    a = Caller(world, world.station("G0ABC"), "GM4BRI-2")
    a.wait_for("/help")
    for i in range(5):
        a.conn.send(f"line {i}\r".encode())
    a.wait_for("Too many lines too fast")
    b = Caller(world, world.station("G4XYZ"), "GM4BRI-2")
    b.wait_for("/help")
    assert "G0ABC: line 1" in b.screen() and "G0ABC: line 2" in b.screen()
    assert "line 0" not in b.screen() and "line 3" not in b.screen()


class StationMail:
    """An OpenHamShack station's side of store-and-forward, as workstream
    E's client would keep it: outgoing mail, and what it collected."""

    def __init__(self, call, outbox=()):
        self.call = call
        self.outbox = list(outbox)
        self.inbox = []
        self.sent = []

    def outgoing(self, peer):
        return [m for m in self.outbox if m not in self.sent]

    def answer(self, peer, p):
        return "-" if any(m.proposal.bid == p.bid for m in self.inbox) else "+"

    def received(self, peer, msg):
        self.inbox.append(msg)

    def delivered(self, peer, out, how):
        self.sent.append(out)


def letter(n, to, subject, body, at="GM4BRI", sender="G0ABC"):
    return fbb.OutMessage(fbb.Proposal("P", sender, at, to, f"{n}_{sender}", len(body)), subject, body)


def test_a_station_delivers_and_collects_by_forwarding():
    world, box, store, bbs, room, wall = make()
    store.post("P", "G4XYZ", "G0ABC", "GM4BRI", "Waiting for you", "collect me", bid="77_G4XYZ")
    store.post("P", "G4XYZ", "M0DEF", "GM4BRI", "Not yours", "someone else's", bid="78_G4XYZ")
    station = world.station("G0ABC-3")
    mail = StationMail("G0ABC", [letter(1, "G4XYZ", "Sked", "2 m at 2000?\n73"),
                                 letter(2, "M0DEF", "Hello", "hi")])
    ended = []
    ex = fbb.call(station, "GM4BRI-1", mail, on_done=ended.append)
    assert world.run(300, lambda: ended)
    assert ended[0].state == "done", ended[0].error
    assert [m.subject for m in mail.inbox] == ["Waiting for you"]
    assert mail.inbox[0].body.endswith("collect me") and mail.inbox[0].body.startswith("R:")
    assert len(mail.sent) == 2
    kept = store.messages(sender="G0ABC")
    assert [(m.recipient, m.subject, m.body, m.origin) for m in kept] == [
        ("G4XYZ", "Sked", "2 m at 2000?\n73", "forward:G0ABC"), ("M0DEF", "Hello", "hi", "forward:G0ABC")]
    collected = store.messages(states=("forwarded",))
    assert [m.bid for m in collected] == ["77_G4XYZ"]
    assert ex.result.sent == 2 and ex.result.received == 1
    assert world.run(60, lambda: not station.connections)

    # Again: nothing new either way, and nothing arrives twice.
    ended.clear()
    fbb.call(station, "GM4BRI-1", mail, on_done=ended.append)
    assert world.run(300, lambda: ended)
    assert ended[0].state == "done" and len(mail.inbox) == 1
    assert len(store.messages(sender="G0ABC")) == 2


def test_forwarding_refuses_what_a_stranger_may_not_send():
    world, box, store, bbs, room, wall = make()
    store.post("P", "G4XYZ", "G0ABC", "GM4BRI", "Seen", "x", bid="5_G4XYZ")
    mail = StationMail("G0ABC", [
        letter(1, "G4XYZ", "Relay", "elsewhere", at="GB7XXX"),                 # not this BBS: refused
        fbb.OutMessage(fbb.Proposal("B", "G0ABC", "GBR", "ALL", "2_G0ABC", 5), "Bull", "bull"),  # bulletin: refused
        fbb.OutMessage(fbb.Proposal("P", "G4XYZ", "GM4BRI", "G0ABC", "5_G4XYZ", 1), "Seen", "x"),  # BID known
    ])
    ended = []
    fbb.call(world.station("G0ABC"), "GM4BRI-1", mail, on_done=ended.append)
    assert world.run(300, lambda: ended)
    assert ended[0].state == "done"
    assert ended[0].result.refused == 3
    assert store.messages(sender="G0ABC") == []


def test_forwarding_with_a_partner_bbs_both_ways():
    world = World()
    wall = [T0]
    box = world.station("GM4BRI")
    store = BbsStore(home="GM4BRI", clock=lambda: wall[0])
    Bbs(box, store, BbsConfig(ssid=1, partners=[Partner.parse("GB7XXX-1 routes GB7XXX,#79")]),
        wall_clock=lambda: wall[0])
    other = world.station("GB7XXX")
    ostore = BbsStore(home="GB7XXX", clock=lambda: wall[0])
    obbs = Bbs(other, ostore, BbsConfig(ssid=1, partners=[Partner.parse("GM4BRI-1")]),
               wall_clock=lambda: wall[0])
    store.post("P", "G0ABC", "G9ZZZ", "GB7XXX.#79.GBR.EURO", "Over there", "for a GB7XXX user")
    store.post("B", "G0ABC", "ALL", "GBR", "Rally", "Sunday")
    store.post("P", "G0ABC", "G4XYZ", "GM4BRI", "Stays here", "local")
    ostore.post("P", "G9ZZZ", "G0ABC", "GM4BRI", "Reply", "from GB7XXX")
    obbs.forward_now()
    assert world.run(600, lambda: obbs._forwards and obbs._forwards["GM4BRI"].state in ("done", "failed"))
    assert obbs._forwards["GM4BRI"].state == "done", obbs._forwards["GM4BRI"].error
    there = {m.subject: m for m in ostore.messages()}
    assert set(there) == {"Over there", "Rally"}
    assert there["Over there"].body.startswith("R:") and "@GM4BRI" in there["Over there"].body
    here = {m.subject: m for m in store.messages(states=("new", "read", "forwarded"))}
    assert here["Reply"].origin == "forward:GB7XXX" and here["Over there"].state == "forwarded"
    assert here["Stays here"].state == "new"


def test_id_beacon_only_after_traffic_and_at_most_every_15_minutes():
    world, box, store, bbs, room, wall = make()
    ids = []
    listener = world.station("G4LST")
    listener.ui_handlers.append(lambda f: ids.append(f.info) if f.destination.call == "ID" else None)
    IdBeacon(box, "GM4BRI-1 BBS, GM4BRI-2 conference")
    world.run(1800)
    assert ids == []                                  # silent station, no ID
    a = Caller(world, world.station("G0ABC"), "GM4BRI-1")
    a.wait_for(PROMPT)
    assert world.run(120, lambda: ids)
    assert ids[0] == b"GM4BRI-1 BBS, GM4BRI-2 conference"
    a.type("L", then=PROMPT)
    world.run(300)
    assert len(ids) == 1                              # not again within 15 minutes
    world.run(1200)
    assert len(ids) == 2                              # owed for the L, sent once due
    world.run(3600)
    assert len(ids) == 2                              # quiet again


def test_store_expiry_numbers_and_schema(tmp_path):
    wall = [T0]
    path = tmp_path / "bbs.db"
    store = BbsStore(path, home="GM4BRI", retention=Retention(bulletin_days=1, personal_days=10, read_days=2),
                     clock=lambda: wall[0])
    b = store.post("B", "G0ABC", "ALL", "GBR", "Bull", "x")
    p = store.post("P", "G0ABC", "G4XYZ", "GM4BRI", "Mail", "y")
    assert store.post("P", "G0ABC", "G4XYZ", "GM4BRI", "Again", "y", bid=p.bid) is None
    store.mark_read(p.number, now=T0 + 1)
    wall[0] = T0 + 1.5 * DAY
    assert store.sweep() == 1 and store.get(b.number) is None
    wall[0] = T0 + 3 * DAY
    assert store.sweep() == 1 and store.get(p.number) is None
    n = store.post("P", "G0ABC", "G4XYZ", "GM4BRI", "Later", "z")
    assert n.number == 3                             # numbers are never reused
    assert store.has_bid(p.bid)                      # the BID outlives its message
    store.close()
    again = BbsStore(path, home="GM4BRI", clock=lambda: wall[0])
    assert again.get(3).subject == "Later"
    again._db.execute("PRAGMA user_version = 99")
    again.close()
    import pytest
    from shack_packet.bbsstore import StoreTooNew
    with pytest.raises(StoreTooNew):
        BbsStore(path, home="GM4BRI")
