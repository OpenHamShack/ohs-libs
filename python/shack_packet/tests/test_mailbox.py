"""The personal mailbox, used over a connection as a caller would."""

from __future__ import annotations

from stations import World

from shack_packet.datalink import State
from shack_packet.mailbox import FileStore, Mailbox, MailboxConfig, MemoryStore


class Caller:
    """A station typing at the mailbox and reading what comes back."""

    def __init__(self, world, engine, target):
        self.world = world
        self.conn = engine.connect(target)
        self.text = bytearray()
        self.conn.on_data = self.text.extend

    def wait_for(self, words, seconds=120):
        ok = self.world.run(seconds, lambda: words.encode() in self.text)
        assert ok, bytes(self.text).decode(errors="replace")[-500:]

    def type(self, line, then=None):
        self.text.clear()
        self.conn.send((line + "\r").encode())
        if then is not None:
            self.wait_for(then)

    def screen(self):
        return bytes(self.text).decode()


def setup(store=None, **config):
    world = World()
    owner = world.station("GM4BRI")
    mailbox = Mailbox(owner, store or MemoryStore(), MailboxConfig(enabled=True, **config),
                      wall_clock=lambda: 1_790_000_000.0)
    caller = world.station("G0ABC-9")
    return world, owner, mailbox, caller


def test_a_caller_leaves_a_message_and_reads_one_left_for_them():
    world, owner, mailbox, caller_engine = setup()
    mailbox.store.add("GM4BRI", "G0ABC", "Sked", "Are you on 2 m tonight?\nGM4BRI", 1_789_990_000.0)
    c = Caller(world, caller_engine, "GM4BRI")
    c.wait_for(">")
    assert "Welcome to the mailbox of GM4BRI" in c.screen()
    assert "Messages waiting for you: 1" in c.screen()
    c.type("L", then=">")
    assert "1* " in c.screen() and "from GM4BRI to G0ABC: Sked" in c.screen()
    c.type("r 1", then=">")
    assert "Subject: Sked" in c.screen() and "Are you on 2 m tonight?" in c.screen()
    assert mailbox.store.get(1).read
    c.type("S", then="Subject:")
    c.type("Re: Sked", then="/EX")
    c.type("Yes, from 2000.")
    c.type("73 Alan")
    c.type("/ex", then=">")
    assert "Message 2 kept for GM4BRI" in c.screen()
    kept = mailbox.store.get(2)
    assert (kept.sender, kept.to, kept.subject, kept.body) == ("G0ABC", "GM4BRI", "Re: Sked", "Yes, from 2000.\n73 Alan")
    assert any(e.kind == "mail" for e in owner.events)
    c.type("K 1", then=">")
    assert mailbox.store.get(1) is None
    c.type("BYE", then="goodbye")
    assert world.run(60, lambda: c.conn.state is State.DISCONNECTED)


def test_limits_and_refusals():
    world, owner, mailbox, caller_engine = setup(max_message_bytes=40, max_per_caller=1)
    other = mailbox.store.add("M0XYZ", "GM4BRI", "private", "not for G0ABC", 0.0)
    c = Caller(world, caller_engine, "GM4BRI")
    c.wait_for(">")
    c.type(f"R {other.id}", then=">")
    assert "There is no message" in c.screen()
    c.type("S M0XYZ", then=">")
    assert "only takes messages for GM4BRI" in c.screen()
    c.type("frobnicate", then=">")
    assert "Unknown command" in c.screen()
    c.type("S", then="Subject:")
    c.type("long", then="/EX")
    c.type("x" * 60, then=">")
    assert "too long" in c.screen()
    c.type("S", then="Subject:")
    c.type("short", then="/EX")
    c.type("hello\x1a", then=">")
    assert "kept for GM4BRI" in c.screen()
    c.type("S", then=">")
    assert "as many messages" in c.screen()


def test_off_unless_allowed_and_a_refused_caller_gets_dm():
    world = World()
    owner = world.station("GM4BRI")
    Mailbox(owner)                                            # not enabled
    caller = world.station("G0ABC")
    conn = caller.connect("GM4BRI")
    assert world.run(60, lambda: conn.state is State.DISCONNECTED)
    assert conn.reason == "refused"
    world2, owner2, mailbox2, caller2 = setup(allow=lambda call: call != "G0ABC-9")
    conn2 = caller2.connect("GM4BRI")
    assert world2.run(60, lambda: conn2.state is State.DISCONNECTED) and conn2.reason == "refused"


def test_a_silent_caller_is_disconnected():
    world, owner, mailbox, caller_engine = setup(idle_timeout_s=120)
    c = Caller(world, caller_engine, "GM4BRI")
    c.wait_for(">")
    assert world.run(300, lambda: c.conn.state is State.DISCONNECTED)
    assert "No input for a while" in c.screen()


def test_the_file_store_survives_a_restart(tmp_path):
    path = tmp_path / "mailbox.json"
    store = FileStore(path)
    store.add("G0ABC", "GM4BRI", "Hello", "body\nlines", 1.0)
    store.add("M0XYZ", "GM4BRI", "Two", "x", 2.0)
    store.mark_read(1)
    store.delete(2)
    again = FileStore(path)
    assert [(m.id, m.sender, m.subject, m.body, m.read) for m in again.all()] == [(1, "G0ABC", "Hello", "body\nlines", True)]
    assert again.add("G0ABC", "GM4BRI", "Three", "", 3.0).id == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_two_threads_saving_at_once_never_lose_the_file(tmp_path):
    """The window marks a message read while the engine files another: both
    used to write mailbox.json.tmp, and one os.replace found it gone (an
    unhandled error in a Qt slot, which aborted Packet Radio)."""
    import threading
    store = FileStore(tmp_path / "mailbox.json")
    for i in range(20):
        store.add("G4SIM", "GM4BRI", f"subject {i}", "body", float(i))
    errors = []

    def mark():
        try:
            for _ in range(200):
                for m in store.all():
                    store.mark_read(m.id)
        except Exception as e:                      # noqa: BLE001 - what the test is for
            errors.append(e)

    def add():
        try:
            for i in range(100):
                store.add("G4SIM", "GM4BRI", f"more {i}", "body", float(i))
        except Exception as e:                      # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=f) for f in (mark, add, mark)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(FileStore(tmp_path / "mailbox.json").all()) == 120
