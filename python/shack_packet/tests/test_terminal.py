"""What nodes and BBSs print, read as an operator reads it: prompts,
paging questions, NODES and ROUTES listings, message lists and who is in
a conference. The samples are in the shapes LinBPQ, BPQMail, TheNet and
FBB print (terminal.py's docstring lists them)."""

from __future__ import annotations

from shack_packet import terminal as t


def test_prompts_and_paging_are_told_apart():
    assert t.prompt_kind("de GB7EDN>") == "bbs"
    assert t.prompt_kind("(#1) GM4BRI de GB7EDN> ") == "bbs"
    assert t.prompt_kind("GB7EDN>") == "bbs"
    assert t.prompt_kind("<A>bort, <CR> Continue..>") == "page"
    assert t.prompt_kind("Continue (Y/N) ?") == "page"
    assert t.prompt_kind("EDIN:GB7EDN} Connected to BBS") == ""
    assert t.prompt_kind("Hello there") == ""
    assert t.paging_reply("<A>bort, <CR> Continue..>") == ""
    assert t.paging_reply("<A>bort, <CR> Continue..>", go_on=False) == "A"
    assert t.paging_reply("More (Y/N)?") == "Y" and t.paging_reply("More (Y/N)?", go_on=False) == "N"


def test_a_nodes_and_a_routes_listing_are_read_a_line_at_a_time():
    r = t.ListingReader()
    got = []
    for line in ["EDIN:GB7EDN} Welcome", "EDIN:GB7EDN} Nodes",
                 "ABDN:GB7AB          BRSTL:GB7BS-7      :GB7XX   ",
                 "CHAT:GB7EDN-4", "", "this is not a node: list"]:
        got += r.feed(line)
    assert got == [t.NodeEntry("ABDN", "GB7AB"), t.NodeEntry("BRSTL", "GB7BS-7"), t.NodeEntry("", "GB7XX"),
                   t.NodeEntry("CHAT", "GB7EDN-4")]
    assert r.source == "GB7EDN"
    got = []
    for line in ["EDIN:GB7EDN} Routes", "> 1 GB7AB-1    192 5", "  2 GB7XX     150 0!", "de GB7EDN>", "  3 X 1 1"]:
        got += r.feed(line)
    assert got == [t.RouteEntry(1, "GB7AB-1", 192, 5, True, False), t.RouteEntry(2, "GB7XX", 150, 0, False, True)]


def test_bpqmail_and_fbb_message_lists_are_read():
    lines = ["Msg#   Date   Type   Size  To      @       From    Subject",
             "22413  20-Oct PN     342 GM4BRI @GB7EDN G0ABC   Hello there",
             "22412  19-Oct B$    1840 ALL    @GBR    G8ABC   For sale: IC-7300",
             " 12345 PN     1234 GM4BRI @ GB7EDN G0ABC   1020/1530 Sked tonight?",
             "305    01-Jan PY      12 GM4BRI         G4XYZ",
             "de GB7EDN>"]
    items = t.parse_mail_list(lines)
    assert [(i.number, i.type, i.status, i.size, i.to, i.at, i.sender, i.date, i.subject) for i in items] == [
        (22413, "P", "N", 342, "GM4BRI", "GB7EDN", "G0ABC", "20-Oct", "Hello there"),
        (22412, "B", "$", 1840, "ALL", "GBR", "G8ABC", "19-Oct", "For sale: IC-7300"),
        (12345, "P", "N", 1234, "GM4BRI", "GB7EDN", "G0ABC", "1020/1530", "Sked tonight?"),
        (305, "P", "Y", 12, "GM4BRI", "", "G4XYZ", "01-Jan", ""),
    ]


def test_who_is_in_a_conference_follows_its_lines():
    p = t.Participants(own="GM4BRI-1")
    for line in ["Here: G4SIM, G8SIM, GM4BRI", "*** G0ABC joined", "G4XYZ: hello all", "<M0AAA>: hi",
                 "*** G8SIM left", "*** Connected to GB7EDN", "GM4BRI: my own line", "*** GM4BRI joined"]:
        p.feed(line)
    assert p.calls == ["G4SIM", "G0ABC", "G4XYZ", "M0AAA"]
    assert t.conference_event("* G0ABC waves") is None
    # shack_packet.conference's own words (the room the desktop and the
    # controller host), read by another station's client.
    room = t.Participants(own="M0AAA")
    room.feed("*** Welcome to the GM4BRI-4 conference. 3 here: GM4BRI G0ABC M0AAA.")
    assert room.calls == ["GM4BRI", "G0ABC"]
    room.feed("*** G4XYZ joined")
    room.feed("*** G0ABC left")
    assert room.calls == ["GM4BRI", "G4XYZ"]
    room.feed("*** 2 here: GM4BRI M0AAA")
    assert room.calls == ["GM4BRI"]
    room.feed("*** You have left the conference.")
    assert room.calls == ["GM4BRI"]
    assert t.conference_event("G0ABC: hi") == t.ConferenceEvent("say", "G0ABC", "hi")


def test_callsigns_in_text_and_the_call_a_log_knows():
    assert t.callsigns("de GB7EDN: G0ABC-9 and 2E0XYZ, M0? 144 also g0abc-9") == ["GB7EDN", "G0ABC-9", "2E0XYZ"]
    assert t.base_call("G0ABC-9") == "G0ABC"
    assert t.base_call("GM/G0ABC/P") == "G0ABC"
    assert t.node_line("EDIN:GB7EDN-1} Nodes") == t.NodeLine("EDIN", "GB7EDN-1", "Nodes")
    assert t.node_line("Hello") is None
