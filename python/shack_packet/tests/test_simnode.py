"""The pretend BPQ node, used as an operator would use a GB7 node, and
read back with terminal.py's readers: the node table, a paged BBS
listing, reading, sending and killing mail, the chat conference and an
onward connection."""

from __future__ import annotations

from stations import World

from shack_packet.netrom import NodeTable
from shack_packet.simnode import SimBpqNode, SimNodeConfig
from shack_packet.terminal import ListingReader, Participants, parse_mail_list, prompt_kind


class Op:
    def __init__(self, world, engine, target):
        self.world = world
        self.conn = engine.connect(target)
        self.text = bytearray()
        self.conn.on_data = self.text.extend

    def wait_for(self, words, seconds=120):
        ok = self.world.run(seconds, lambda: words.encode() in self.text)
        assert ok, bytes(self.text).decode(errors="replace")[-800:]

    def type(self, line, then=None):
        self.text.clear()
        self.conn.send((line + "\r").encode())
        if then is not None:
            self.wait_for(then)

    def lines(self):
        return bytes(self.text).decode().split("\r")


def setup():
    world = World()
    node_engine = world.station("GB7SIM")
    cfg = SimNodeConfig("SIM", "GB7SIM", nodes=[("SJ", "GB7SJ", 200), ("ABDN", "GB7AB", 150)],
                        routes=[(1, "GB7SJ", 192, 2)])
    onward = SimNodeConfig("SJ", "GB7SJ", nodes=[("SIM", "GB7SIM", 192)])
    node = SimBpqNode(node_engine, cfg, onward=[onward], wall=lambda: 1_790_000_000.0)
    return world, node, node_engine


def test_the_node_table_fills_from_a_listing_and_a_broadcast():
    world, node, node_engine = setup()
    me = world.station("GM4BRI")
    table = NodeTable()
    me.monitors.append(lambda f: table.heard_frame(f, world.clock.now))
    node.broadcast()
    assert world.run(30, lambda: "GB7SJ" in table.nodes)
    assert table.find("GB7SIM").direct and table.find("SJ").neighbour == "GB7SIM"
    op = Op(world, me, "GB7SIM")
    op.wait_for("? for commands.")
    reader = ListingReader()
    op.type("N", then="ABDN:GB7AB")
    rows = [e for line in op.lines() for e in reader.feed(line)]
    table.listed(reader.source, nodes=rows)
    assert [(e.alias, e.call) for e in rows] == [("SJ", "GB7SJ"), ("ABDN", "GB7AB")]
    assert table.find("ABDN").neighbour == "GB7SIM"


def test_bbs_listing_is_paged_and_mail_is_read_sent_and_killed():
    world, node, _e = setup()
    node.leave_for("GM4BRI", "Sked", "Are you on tonight?")
    op = Op(world, world.station("GM4BRI"), "GB7SIM")
    op.wait_for("? for commands.")
    op.type("BBS", then="de GB7SIM>")
    assert prompt_kind(op.lines()[-2]) == "bbs"              # the prompt line, then its line end
    op.type("L", then="Continue..>")
    first = parse_mail_list(op.lines())
    assert len(first) == 10 and prompt_kind(op.lines()[-1]) == "page"
    op.type("", then="de GB7SIM>")                           # a bare CR carries on
    rest = parse_mail_list(op.lines())
    assert len(first) + len(rest) == 15 and first[0].subject == "Sked" and first[0].status == "N"
    op.type(f"R {first[0].number}", then="[End of Message")
    assert "Are you on tonight?" in op.lines()
    op.type("SP G0ABC", then="Enter Title (only):")
    op.type("Hello", then="End with /ex")
    op.type("Two lines")
    op.type("of text")
    op.type("/ex", then="de GB7SIM>")
    sent = node.mail[-1]
    assert (sent.to, sent.sender, sent.subject, sent.body) == ("G0ABC", "GM4BRI", "Hello", "Two lines\rof text")
    op.type(f"K {sent.number}", then="Killed")
    assert sent not in node.mail


def test_the_chat_conference_and_an_onward_connection():
    world, node, _e = setup()
    op = Op(world, world.station("GM4BRI"), "GB7SIM")
    op.wait_for("? for commands.")
    op.type("CHAT", then="Here:")
    p = Participants(own="GM4BRI")
    for line in op.lines():
        p.feed(line)
    assert p.calls == ["G4SIM", "G8SIM"]
    op.type("hello", then="G4SIM: Hello GM4BRI")
    op.type("/q", then="Returned to node")
    op.type("C GB7SJ", then="Welcome to GB7SJ")
    op.type("N", then="SIM:GB7SIM")
    assert any(line.startswith("SJ:GB7SJ} Nodes") for line in op.lines())
