"""The controller's side alone (shack_client.controller), which the Dock360
edition mounts: push idempotence, clock and level refusals, paging, epochs."""
import pytest

from shack_client import hlc as H
from shack_client.controller import SyncService

NOW = H.CLOCK_FLOOR_MS + 36_000_000


@pytest.fixture
def svc(tmp_path):
    s = SyncService(tmp_path / "services.db", "7f3c2a9e-51d4-4c0b-9a51-2f7e3d1c4b60", now_ms=lambda: NOW)
    yield s
    s.close()


def rec(rid, wall=NOW, node="st_AAAAAAAAAAAA", collection="logbook", **fields):
    h = H.fmt(wall, 0, node)
    fields = fields or {"call": "DL1ABC"}
    return {"collection": collection, "id": rid, "fields": fields, "fieldHlc": {k: h for k in fields},
            "deletedHlc": None, "createdAt": H.iso_ms(wall), "origin": {"app": "t"}}


def test_push_is_idempotent_and_numbers_changes(svc):
    body = {"records": [rec("q1")], "ops": [{"opId": "o1", "collection": "logbook", "id": "q1", "kind": "create",
                                             "hlc": H.fmt(NOW, 0, "st_AAAAAAAAAAAA"), "fields": {"call": "DL1ABC"}}]}
    s1, a1 = svc.push(body)
    s2, a2 = svc.push(body)
    assert a1["records"][0]["status"] == "merged" and a1["records"][0]["seq"] == 1
    assert a2["records"][0]["status"] == "unchanged" and a2["ops"] == {"accepted": [], "duplicate": ["o1"],
                                                                        "ignored": []}
    assert svc.latest_seq == 1


def test_a_future_stamp_never_lands(svc):
    _, a = svc.push({"records": [rec("q1", wall=NOW + H.MAX_AHEAD_MS + 1)]})
    assert a["records"][0]["code"] == "clock_ahead" and svc.get("logbook", "q1") is None
    _, a = svc.push({"records": [rec("q2", wall=NOW + H.MAX_AHEAD_MS - 1)]})
    assert a["records"][0]["status"] == "merged"


def test_profile_needs_transmit(svc):
    _, a = svc.push({"records": [rec("station", collection="profile", locator="IO75RL")]}, can_edit_profile=False)
    assert a["records"][0]["code"] == "level_too_low"


def test_bad_records_are_refused_one_by_one(svc):
    bad = rec("q1")
    bad["fieldHlc"]["call"] = "yesterday"
    _, a = svc.push({"records": [bad, rec("q2")]})
    assert [r["status"] for r in a["records"]] == ["rejected", "merged"]
    assert svc.push({"records": [rec(str(i)) for i in range(501)]})[0] == 400


def test_pull_pages_in_seq_order_and_detects_a_restore(svc):
    for i in range(7):
        svc.push({"records": [rec(f"q{i}")]})
    seen, since, epoch = [], 0, None
    while True:
        status, a = svc.pull(since, epoch, limit=3)
        seen += [r["id"] for r in a["records"]]
        since, epoch = a["next"], a["epoch"]
        if not a["more"]:
            break
    assert seen == [f"q{i}" for i in range(7)]
    assert svc.pull(99, epoch)[0] == 409
    snap = svc.snapshot()
    svc.restore(snap)
    status, a = svc.pull(since, epoch)
    assert (status, a["code"]) == (409, "epoch_changed") and a["epoch"] != epoch


def test_history_marks_a_late_loser(svc):
    win = rec("q1", wall=NOW, node="st_BBBBBBBBBBBB", rst_rcvd="57")
    lose = rec("q1", wall=NOW - 1000, node="st_AAAAAAAAAAAA", rst_rcvd="55")
    svc.push({"records": [win], "ops": [{"opId": "b", "collection": "logbook", "id": "q1", "kind": "update",
                                         "hlc": win["fieldHlc"]["rst_rcvd"], "fields": {"rst_rcvd": "57"}}]})
    _, a = svc.push({"records": [lose], "ops": [{"opId": "a", "collection": "logbook", "id": "q1", "kind": "update",
                                                 "hlc": lose["fieldHlc"]["rst_rcvd"], "fields": {"rst_rcvd": "55"}}]})
    assert a["records"][0]["status"] == "unchanged" or a["records"][0]["lost"] == ["rst_rcvd"]
    edits = svc.history("logbook", "q1")["edits"]
    assert [(e["opId"], e["lost"]) for e in edits] == [("a", ["rst_rcvd"]), ("b", [])]
    assert svc.get("logbook", "q1")["fields"]["rst_rcvd"] == "57"
