"""The merge's algebra and the HLC's ordering: what makes the sync safe
whatever order copies meet in (SYNC.md §1 rule 2, §3, §4)."""
import itertools
import random

import pytest

from shack_client import hlc as H
from shack_client.merge import apply_edit, is_live, join_profile, merge, same, split_profile

NODES = ["st_AAAAAAAAAAAA", "st_BBBBBBBBBBBB", "st_CCCCCCCCCCCC"]


def random_record(rng: random.Random) -> dict:
    fields, hlcs = {}, {}
    for f in rng.sample(["call", "rst_rcvd", "rst_sent", "comment", "qsl_sent"], rng.randint(1, 5)):
        h = H.fmt(H.CLOCK_FLOOR_MS + rng.randint(0, 20), rng.randint(0, 3), rng.choice(NODES))
        hlcs[f] = h
        if rng.random() > 0.2:
            fields[f] = f"{f}-{h[-4:]}-{rng.randint(0, 9)}"
    dh = H.fmt(H.CLOCK_FLOOR_MS + rng.randint(0, 20), 0, rng.choice(NODES)) if rng.random() < 0.4 else None
    return {"collection": "logbook", "id": "q", "fields": fields, "fieldHlc": hlcs, "deletedHlc": dh,
            "createdAt": f"2026-10-09T10:00:{rng.randint(10, 59)}.000Z", "origin": {"app": rng.choice("xyz")}}


def consistent(recs):
    """The same stamp always carries the same value (as real edits do)."""
    seen = {}
    for r in recs:
        for f, h in r["fieldHlc"].items():
            v = r["fields"].get(f, None)
            if (f, h) in seen:
                if v is None:
                    r["fields"].pop(f, None)
                    if seen[(f, h)] is not None:
                        r["fields"][f] = seen[(f, h)]
                else:
                    r["fields"][f] = seen[(f, h)] if seen[(f, h)] is not None else r["fields"].pop(f)
                    if seen[(f, h)] is None:
                        r["fields"].pop(f, None)
            else:
                seen[(f, h)] = v
    return recs


@pytest.mark.parametrize("seed", range(200))
def test_merge_is_commutative_associative_idempotent(seed):
    rng = random.Random(seed)
    a, b, c = consistent([random_record(rng) for _ in range(3)])
    assert same(merge(a, b), merge(b, a))
    assert same(merge(merge(a, b), c), merge(a, merge(b, c)))
    assert same(merge(a, a), a)
    for x, y, z in itertools.permutations([a, b, c]):
        assert same(merge(merge(x, y), z), merge(merge(a, b), c))


def test_edit_beats_an_earlier_delete_and_loses_to_a_later_one():
    h = lambda n, node=NODES[0]: H.fmt(H.CLOCK_FLOOR_MS + n, 0, node)
    r = apply_edit(None, "logbook", "q", "create", {"call": "DL1ABC"}, h(1), {}, "t")
    deleted = apply_edit(r, "logbook", "q", "delete", {}, h(2), {}, "t")
    assert not is_live(deleted)
    revived = merge(deleted, apply_edit(None, "logbook", "q", "update", {"comment": "back"}, h(3, NODES[1]), {}, "t"))
    assert is_live(revived) and revived["fields"] == {"call": "DL1ABC", "comment": "back"}
    again = apply_edit(revived, "logbook", "q", "delete", {}, h(4), {}, "t")
    assert not is_live(again)


def test_null_clears_a_field_with_its_own_stamp():
    h1, h2 = H.fmt(H.CLOCK_FLOOR_MS, 0, NODES[0]), H.fmt(H.CLOCK_FLOOR_MS + 1, 0, NODES[1])
    r = apply_edit(None, "logbook", "q", "create", {"call": "X", "comment": "c"}, h1, {}, "t")
    r = apply_edit(r, "logbook", "q", "update", {"comment": None}, h2, {}, "t")
    assert "comment" not in r["fields"] and r["fieldHlc"]["comment"] == h2


def test_unknown_envelope_keys_survive_a_merge():
    a = {"collection": "logbook", "id": "q", "fields": {}, "fieldHlc": {"x": H.fmt(H.CLOCK_FLOOR_MS, 0, "st_A")},
         "deletedHlc": None, "createdAt": "", "origin": {}, "futureEnvelope": {"v": 2}}
    b = dict(a, fieldHlc={"x": H.fmt(H.CLOCK_FLOOR_MS + 5, 0, "st_B")})
    b.pop("futureEnvelope")
    assert merge(a, b)["futureEnvelope"] == {"v": 2}


def test_hlc_strings_order_by_time_count_node():
    s = [H.fmt(5, 0, "st_B"), H.fmt(4, 9, "st_Z"), H.fmt(5, 1, "st_A"), H.fmt(5, 0, "st_A")]
    assert sorted(s) == [H.fmt(4, 9, "st_Z"), H.fmt(5, 0, "st_A"), H.fmt(5, 0, "st_B"), H.fmt(5, 1, "st_A")]


def test_hlc_local_and_receive_rules():
    w, c, s1 = H.local(0, 0, "st_A", 1000)
    w, c, s2 = H.local(w, c, "st_A", 900)          # clock went backwards: count rises
    assert s2 > s1 and H.parse(s2)[:2] == (1000, 1)
    w, c = H.receive(w, c, H.fmt(5000, 3, "st_B"), 1000)
    _w, _c, s3 = H.local(w, c, "st_A", 1000)
    assert s3 > H.fmt(5000, 3, "st_B")
    assert H.corrected(10, 0) == H.CLOCK_FLOOR_MS     # a 1970 clock stamps at the floor


def test_hlc_count_overflow_moves_the_wall():
    w, c, s = H.local(1000, 0xFFFF, "st_A", 1000)
    assert (w, c) == (1001, 0)


def test_profile_maps_split_into_independent_fields():
    patch = {"locator": "IO75RL", "conditions": {"nearLondon": True},
             "controllerMaps": {"qo100wb": {"ajazz-akp03e": {"pages": [{"K1": {"press": "transmit"}}]}}}}
    flat = split_profile(patch)
    assert set(flat) == {"locator", "conditions.nearLondon", "controllerMaps.qo100wb.ajazz-akp03e"}
    assert join_profile(flat) == patch
