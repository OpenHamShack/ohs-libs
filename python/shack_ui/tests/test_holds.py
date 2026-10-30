"""Holds: a stop that needs the id its start returned, kept by the box."""
import json
import threading

from shack_ui import A, Holds, HoldRefused, Screen, p


def radio():
    log, n = [], iter(range(1, 100))

    def start(client, req):
        sid = f"s{next(n)}"
        log.append(("start", client, sid))
        return sid

    def stop(sid):
        log.append(("stop", sid))

    h = Holds()
    h.define("dmr", start, stop)
    return h, log


def body(r):
    return json.loads(r.body)


def test_the_stop_uses_the_id_the_start_returned():
    h, log = radio()
    r = h.answer("POST", "dmr", "remote-7f3a")
    assert r.status == 200 and body(r) == {"held": "dmr", "id": "s1"}
    assert h.held("dmr", "remote-7f3a") == "s1"
    assert body(h.answer("DELETE", "dmr", "remote-7f3a")) == {"stopped": True}
    assert body(h.answer("DELETE", "dmr", "remote-7f3a")) == {"stopped": False}
    assert log == [("start", "remote-7f3a", "s1"), ("stop", "s1")]


def test_a_repeated_start_keeps_the_one_session():
    h, log = radio()
    h.start("dmr", "tv")
    h.start("dmr", "tv")                    # back to the front, or a reconnect
    h.start("dmr", "phone")
    assert [e for e in log if e[0] == "start"] == [("start", "tv", "s1"), ("start", "phone", "s2")]


def test_racing_starts_leave_one_session_and_stop_the_other():
    h, log = Holds(), []
    gate = threading.Barrier(2)

    def start(client, req):
        gate.wait()
        sid = f"s{len(log)}-{threading.get_ident()}"
        log.append(("start", sid))
        return sid
    h.define("dmr", start, lambda sid: log.append(("stop", sid)))
    ids = []
    ts = [threading.Thread(target=lambda: ids.append(h.start("dmr", "tv"))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert ids[0] == ids[1] == h.held("dmr", "tv")
    assert len([e for e in log if e[0] == "stop"]) == 1


def test_gone_stops_everything_a_client_held():
    h, log = radio()
    h.define("ais", lambda c, r: "ais-1", lambda sid: log.append(("stop", sid)))
    h.start("dmr", "tv")
    h.start("ais", "tv")
    h.start("dmr", "phone")
    assert sorted(h.gone("tv")) == ["ais", "dmr"]
    assert h.held("dmr", "phone") == "s2" and h.held("dmr", "tv") is None


def test_a_refusal_carries_its_remedy():
    h = Holds()
    stop_dab = A.delete("api/decoders/sessions/dab", level="control", text="Stop DAB")

    def busy(client, req):
        raise HoldRefused(409, "DAB has the receiver", "receiver_busy", actions=[stop_dab])
    h.define("dmr", busy, lambda sid: None)
    r = h.answer("POST", "dmr", "tv")
    assert r.status == 409 and body(r) == {"error": "DAB has the receiver", "code": "receiver_busy",
                                           "actions": [stop_dab]}
    assert h.held("dmr", "tv") is None


def test_bad_requests():
    h, _ = radio()
    assert h.answer("POST", "nope", "tv").status == 404
    assert h.answer("POST", "dmr", None).status == 400
    assert h.answer("POST", "dmr", "../../etc").status == 400
    assert h.answer("PUT", "dmr", "tv").status == 405


def test_its_actions_make_a_valid_hold():
    h, _ = radio()
    s = Screen("radio/dmr", level="control", root=p.text("DMR"))
    s.hold("dmr", *h.actions("dmr"))
    d = s.json()
    assert d["holds"] == [{"name": "dmr",
                           "start": {"do": "call", "method": "POST", "path": "ui/v2/hold/dmr",
                                     "body": {"client": "$client"}, "level": "control", "timeoutS": 30},
                           "stop": {"do": "call", "method": "DELETE", "path": "ui/v2/hold/dmr?client={$client}",
                                    "level": "control"}}]
