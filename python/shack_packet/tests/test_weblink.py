"""The link test's return path through openhamshack.com (weblink.py),
against a stand-in for the site's PHP relay (weblinkfake.py) on a local
port: pairing by callsign, frames both ways, and every way it can go
wrong failing safe."""

from __future__ import annotations

import time

import pytest
from weblinkfake import FakeLinkServer

from shack_packet.linktest import LinkTestError, RelayReturnPath
from shack_packet.weblink import WebRelayClient

A, B = "GM4BRI-1", "GM0OTB-1"
FRAME = bytes.fromhex("9c6e9a8a9a40e09c6e9a8a9a40613f")


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def server():
    s = FakeLinkServer()
    yield s
    s.close()


def client(server, call, peer, role="rf-sender", **kw):
    kw.setdefault("poll_wait_s", 1.0)
    kw.setdefault("heartbeat_s", 0.2)
    kw.setdefault("silence_s", 1.5)
    kw.setdefault("peer_silent_s", 1.0)
    return WebRelayClient(server.url, call=call, peer=peer, role=role, **kw)


class Sink:
    def __init__(self):
        self.frames, self.lost = [], []

    def return_frame(self, path, frame):
        self.frames.append(frame)

    def return_lost(self, path, reason):
        self.lost.append(reason)


def test_two_stations_asking_for_each_other_pair_and_carry_frames_both_ways(server):
    a, b = client(server, A, B), client(server, B, A, role="rf-receiver")
    try:
        a.start()
        assert a.state == "waiting" and a.first == "GM0OTB-1"
        b.start()
        assert b.state == "ready"
        assert until(lambda: a.ready)
        assert a.peer_hello["call"] == "GM0OTB-1"
        got_a, got_b = [], []
        a.on_frame, b.on_frame = got_a.append, got_b.append
        a.send_frame(FRAME)
        b.send_frame(FRAME)
        assert until(lambda: got_a == [FRAME] and got_b == [FRAME])
        assert until(lambda: a.clock() is not None)
        assert a.rtts and a.clock()["rtt_s"] >= 0
    finally:
        a.close()
        b.close()


def test_a_station_asking_for_someone_else_never_pairs(server):
    a, c = client(server, A, B), client(server, "M0ABC", A)
    try:
        a.start()
        c.start()
        time.sleep(0.5)
        assert a.state == "waiting" and c.state == "waiting"
    finally:
        a.close()
        c.close()


def test_the_site_going_away_loses_the_return_path(server):
    a, b = client(server, A, B), client(server, B, A, role="rf-receiver")
    a.start()
    b.start()
    sink = Sink()
    path = RelayReturnPath(a, B, outbound=False)
    path.bind(sink)
    assert until(lambda: path.up)
    server.kill()
    assert until(lambda: a.state == "lost", timeout=4.0)
    assert a.lost_why == "relay-silent" and sink.lost == ["relay-silent"]
    assert not path.up
    a.close()
    b.close()


def test_the_other_station_going_quiet_and_coming_back(server):
    a, b = client(server, A, B), client(server, B, A, role="rf-receiver")
    states = []
    a.on_state = lambda s, why: states.append((s, why))
    a.start()
    b.start()
    assert until(lambda: a.ready)
    server.quiet(B)
    assert until(lambda: a.state == "waiting", timeout=5.0)
    assert ("waiting", "peer-silent") in states
    server.quiet(B, False)
    assert until(lambda: a.state == "ready", timeout=5.0)
    a.close()
    b.close()


def test_a_new_run_of_the_same_callsign_replaces_the_old(server):
    a, b = client(server, A, B), client(server, B, A, role="rf-receiver")
    a.start()
    b.start()
    assert until(lambda: a.ready)
    b2 = client(server, B, A, role="rf-receiver").start()
    assert until(lambda: b.state == "lost")
    assert b.lost_why == "refused"
    # A saw the old run leave and the new one join, and is ready with it.
    assert until(lambda: a.ready and a.peer_hello is not None)
    got = []
    b2.on_frame = got.append
    a.send_frame(FRAME)
    assert until(lambda: got == [FRAME])
    for c in (a, b, b2):
        c.close()


def test_leaving_mid_poll_is_quick_and_the_other_station_hears_it(server):
    a, b = client(server, A, B, poll_wait_s=10.0), client(server, B, A, role="rf-receiver")
    states = []
    a.start()
    b.on_state = lambda s, why: states.append((s, why))
    b.start()
    assert until(lambda: a.ready)
    t = time.monotonic()
    a.close("stopped")
    assert time.monotonic() - t < 3.0
    assert a.state == "closed"
    assert until(lambda: ("waiting", "peer-left") in states)
    b.close()


def test_an_unreachable_site_is_an_error_at_start():
    c = WebRelayClient("http://127.0.0.1:9/api/link/link.php", call=A, peer=B, role="rf-sender")
    with pytest.raises(LinkTestError) as e:
        c.start()
    assert e.value.reason == "unreachable"


def test_set_role_changes_what_the_telemetry_says(server):
    a = client(server, A, B).start()
    a.set_role("rf-receiver")
    assert a.role == "rf-receiver"
    a.close()


def test_a_web_return_path_says_where_it_goes(server):
    from shack_packet.weblink import WebReturnPath
    a = client(server, A, B).start()
    path = WebReturnPath(a, B, outbound=True)
    d = path.describe()
    assert d["remote"] == B and d["outbound"] and d["url"] == server.url and d["first"] == "GM0OTB-1"
    assert not path.up
    a.close()
