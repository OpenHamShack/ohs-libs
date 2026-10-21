"""Store-and-forward between OpenHamShack stations, over the simulated
channel: straight to the addressee's mailbox, through the home BBS when
the addressee is not on, collected from the BBS by the addressee, held
while transmit is not allowed, and given up after its rounds."""

from __future__ import annotations

from stations import World

from shack_packet.bbs import Bbs, BbsConfig
from shack_packet.bbsstore import BbsStore
from shack_packet.mailbox import Mailbox, MailboxConfig, MemoryStore
from shack_packet.stationmail import RETRY_S, MailConfig, Outbox, Peer, StationMail, split_address

T0 = 1_790_000_000.0


class Station:
    def __init__(self, world, call, *, mailbox=True, config=None, armed=True):
        self.engine = world.station(call)
        self.events = []
        self.engine.on_event = self.events.append
        self.inbox = MemoryStore()
        self.mailbox = Mailbox(self.engine, self.inbox, MailboxConfig(enabled=mailbox, ssid=1),
                               wall_clock=lambda: T0 + world.clock())
        self.armed = [armed]
        self.outbox = Outbox(call=call, clock=lambda: T0 + world.clock())
        self.mail = StationMail(self.engine, self.outbox, self.inbox, config or MailConfig(),
                                can_send=lambda: self.armed[0], wall_clock=lambda: T0 + world.clock())

    def reasons(self, kind="mail"):
        return [e.reason for e in self.events if e.kind == kind]


def bbs(world, call="GM4BRI"):
    box = world.station(call)
    store = BbsStore(home=call, clock=lambda: T0 + world.clock())
    Bbs(box, store, BbsConfig(ssid=1), wall_clock=lambda: T0 + world.clock())
    return store


def test_addresses():
    assert split_address("g4xyz-7") == ("G4XYZ", "")
    assert split_address("G4XYZ@GB7XYZ.#79.GBR.EURO") == ("G4XYZ", "GB7XYZ.#79.GBR.EURO")
    p = Peer.parse("GB7XYZ-1 via MB7NXX routes GB7XYZ,#79")
    assert (p.call, p.via, p.routes) == ("GB7XYZ-1", ["MB7NXX"], ["GB7XYZ", "#79"])
    assert p.takes("GB7ABC.#79.GBR") and not p.takes("GB7ABC.#12.GBR")


def test_straight_to_the_addressees_mailbox():
    world = World()
    a = Station(world, "G0ABC")
    b = Station(world, "G4XYZ")
    m = a.mail.compose("G4XYZ", "Sked", "2 m at 2000?")
    assert world.run(600, lambda: a.outbox.get(m.id).state == "delivered")
    assert a.outbox.get(m.id).route == "G4XYZ-1"
    got = b.inbox.all()
    assert [(x.sender, x.to, x.subject, x.body) for x in got] == [("G0ABC", "G4XYZ", "Sked", "2 m at 2000?")]
    assert "mail-received" in b.reasons() and "mail-delivered" in a.reasons()
    # Offered again (a resend after a lost link), it is not kept twice.
    a.outbox.retry(m.id)
    a.outbox.get(m.id).state = "queued"
    a.outbox.get(m.id).tried.clear()
    world.run(600, lambda: a.outbox.get(m.id).state != "queued")
    assert len(b.inbox.all()) == 1


def test_through_the_home_bbs_and_collected_there():
    world = World()
    store = bbs(world)
    a = Station(world, "G0ABC", config=MailConfig(home="GM4BRI-1"))
    b = Station(world, "G4XYZ", mailbox=False, config=MailConfig(home="GM4BRI-1"))
    m = a.mail.compose("G4XYZ", "Sked", "2 m at 2000?")
    assert world.run(1200, lambda: a.outbox.get(m.id).state == "delivered")
    assert a.outbox.get(m.id).route == "GM4BRI-1"          # G4XYZ-1 did not answer
    held = store.messages(type_="P", to="G4XYZ")
    assert [x.subject for x in held] == ["Sked"]
    assert b.inbox.all() == []
    assert b.mail.collect_now()
    assert world.run(1200, lambda: b.inbox.all())
    assert [(x.sender, x.subject) for x in b.inbox.all()] == [("G0ABC", "Sked")]
    assert world.run(300, lambda: not b.mail.busy)
    assert "mail-collected" in b.reasons() and b.mail.last["received"] == 1
    assert store.deliveries(held[0].number)[0]["peer"] == "G4XYZ"


def test_mail_for_another_bbs_goes_to_a_partner_that_takes_it():
    world = World()
    store = bbs(world, "GB7XYZ")
    a = Station(world, "G0ABC", config=MailConfig(partners=[Peer.parse("GB7XYZ-1 routes GB7XYZ")]))
    m = a.mail.compose("G4XYZ@GB7XYZ.#79.GBR.EURO", "Hello", "via the BBS")
    assert a.mail.routes(m)[0][1].call == "GB7XYZ-1"
    assert world.run(1200, lambda: a.outbox.get(m.id).state == "delivered")
    assert store.messages(type_="P", to="G4XYZ")[0].at == "GB7XYZ.#79.GBR.EURO"


def test_nothing_goes_while_transmit_is_not_allowed():
    world = World()
    a = Station(world, "G0ABC", armed=False)
    b = Station(world, "G4XYZ")
    sent = []
    a.engine.channel.on_transmit = lambda start, end, frames: sent.extend(frames)
    m = a.mail.compose("G4XYZ", "Sked", "later")
    world.run(900)
    assert sent == [] and a.outbox.get(m.id).state == "queued"
    a.armed[0] = True
    assert world.run(900, lambda: a.outbox.get(m.id).state == "delivered")
    assert len(b.inbox.all()) == 1


def test_unreachable_mail_is_retried_then_failed():
    world = World()
    a = Station(world, "G0ABC", config=MailConfig(max_rounds=2))
    m = a.mail.compose("G4XYZ", "Anyone?", "hello")
    assert world.run(RETRY_S[0] + 3600, lambda: a.outbox.get(m.id).state == "failed")
    out = a.outbox.get(m.id)
    assert out.rounds == 2 and out.error == "no-answer"
    assert a.reasons().count("mail-failed") == 1
    a.outbox.retry(m.id)
    assert a.outbox.get(m.id).state == "queued" and a.outbox.get(m.id).rounds == 0


def test_a_station_typing_at_the_mailbox_still_can():
    """The SID line at the top of the greeting is harmless to a person."""
    world = World()
    Station(world, "G4XYZ")
    caller = world.station("G0ABC")
    conn = caller.connect("G4XYZ-1")
    text = bytearray()
    conn.on_data = text.extend
    assert world.run(120, lambda: b">" in text)
    assert b"[OHS-0.3.0-FHM$]" in text and b"Welcome to the mailbox of G4XYZ" in text
    conn.send(b"L\r")
    assert world.run(120, lambda: b"No messages" in text)


def test_outbox_survives_a_restart(tmp_path):
    path = tmp_path / "outbox.json"
    box = Outbox(path, call="G0ABC", clock=lambda: T0)
    m = box.add("G4XYZ@GB7XYZ", "Sked", "body")
    m.state = "sending"
    box.save()
    again = Outbox(path, call="G0ABC")
    assert again.get(m.id).state == "queued" and again.get(m.id).at == "GB7XYZ"
    assert again.add("G4XYZ", "two", "x").id == m.id + 1
