"""The merge every replica runs: client stores, the controller, and the
Kotlin and Swift ports (SYNC.md §4). One function, so the client and the
controller cannot drift: the Dock360 edition imports this module rather
than reimplementing it (REPO-LAYOUT.md §4 "the Dock360 edition pins
shack-client (the merge, HLC and wire models it must share)").

A record is the wire shape of SYNC.md §6.1:

    {"collection": "logbook", "id": "<uuid>", "fields": {...},
     "fieldHlc": {...}, "deletedHlc": null | "<hlc>",
     "createdAt": "<iso>", "origin": {...}}

plus any envelope keys a later contract version adds, which are carried
through untouched (§8 T19; SERVICES.md §1 "a field a client does not know
is kept and sent back").

The merge is state-based and per field: for each field, the value with
the greater HLC. It is commutative, associative and idempotent, so the
order and number of times two copies meet do not matter
(tests/test_merge.py checks all three on generated records).
"""
from __future__ import annotations

import copy
import json

ENVELOPE = ("collection", "id", "fields", "fieldHlc", "deletedHlc", "createdAt", "origin")

# The profile's two maps whose entries merge one by one (§4.2): two
# devices adding different pad layouts, or answering different licence
# conditions, must both be kept.
PROFILE_SPLIT = {"conditions": 1, "controllerMaps": 2}


def extra_of(rec: dict) -> dict:
    """Envelope keys this version does not know, to carry through."""
    return {k: v for k, v in rec.items() if k not in ENVELOPE and k != "seq"}


def max_hlc(*hlcs: str | None) -> str | None:
    present = [h for h in hlcs if h]
    return max(present) if present else None


def is_live(rec: dict) -> bool:
    """Deleted only while the tombstone is later than every field edit:
    a later edit brings a contact back (§5, edit beats delete)."""
    d = rec.get("deletedHlc")
    if not d:
        return True
    return any(h > d for h in rec.get("fieldHlc", {}).values())


def merge(a: dict | None, b: dict | None) -> dict:
    """The merged record. Either side may be None (a record one replica
    has never seen)."""
    if a is None:
        return copy.deepcopy(b)
    if b is None:
        return copy.deepcopy(a)
    fields, hlcs = {}, {}
    for f in set(a["fieldHlc"]) | set(b["fieldHlc"]):
        ha, hb = a["fieldHlc"].get(f), b["fieldHlc"].get(f)
        # Equal stamps mean the same edit seen twice: either side's value
        # is the same value. Ties never happen between different edits.
        src = a if (hb is None or (ha is not None and ha >= hb)) else b
        hlcs[f] = src["fieldHlc"][f]
        if f in src["fields"]:
            fields[f] = copy.deepcopy(src["fields"][f])
    ca, cb = a.get("createdAt") or "", b.get("createdAt") or ""
    # The origin is the earlier creation's; a tie (two copies of one
    # creation) breaks on the origin itself, so the merge stays
    # commutative even for copies that disagree.
    first = min((a, b), key=lambda r: (not r.get("createdAt"), r.get("createdAt") or "",
                                       json.dumps(r.get("origin") or {}, sort_keys=True)))
    out = {"collection": a["collection"], "id": a["id"], "fields": fields, "fieldHlc": hlcs,
           "deletedHlc": max_hlc(a.get("deletedHlc"), b.get("deletedHlc")),
           "createdAt": min(x for x in (ca, cb) if x) if (ca or cb) else "",
           "origin": copy.deepcopy(first.get("origin") or {})}
    # Unknown envelope keys: the side with the later state wins each, so a
    # newer client's addition survives an older one's push.
    newer, older = (a, b) if _latest(a) >= _latest(b) else (b, a)
    out.update(copy.deepcopy(extra_of(older)))
    out.update(copy.deepcopy(extra_of(newer)))
    return out


def _latest(rec: dict) -> str:
    return max_hlc(rec.get("deletedHlc"), *rec.get("fieldHlc", {}).values()) or ""


def same(a: dict | None, b: dict | None) -> bool:
    """Whether two states are the same for sync purposes (so a merge that
    returns `same(merged, stored)` changed nothing and takes no new seq)."""
    if a is None or b is None:
        return a is b
    keys = ("fields", "fieldHlc", "deletedHlc", "createdAt", "origin")
    return all(a.get(k) == b.get(k) for k in keys) and extra_of(a) == extra_of(b)


def apply_edit(rec: dict | None, collection: str, rid: str, kind: str, fields: dict, hlc: str,
               origin: dict, now_iso: str) -> dict:
    """A local edit as a record state: the same merge, with the edit as a
    one-op record. `fields` values of None clear a field (that is an edit
    with its own HLC: SERVICES.md §3.2 PATCH)."""
    one = {"collection": collection, "id": rid, "fields": {}, "fieldHlc": {}, "deletedHlc": None,
           "createdAt": now_iso if rec is None else rec.get("createdAt", now_iso),
           "origin": origin if rec is None else rec.get("origin", origin)}
    if kind == "delete":
        one["deletedHlc"] = hlc
    else:
        for f, v in fields.items():
            one["fieldHlc"][f] = hlc
            if v is not None:
                one["fields"][f] = v
    return merge(rec, one)


def lost_fields(op: dict, state: dict) -> list:
    """The fields an arriving op set that a later-HLC edit had already set
    (SERVICES.md §3.2 `lost`): it arrived late and lost. A later stamp
    from the op's own store is not a loss - it is the same store's next
    edit - so only another node's later value counts."""
    node = op["hlc"].rsplit("-", 1)[-1]
    out = []
    for f in sorted(op.get("fields") or {}):
        h = state.get("fieldHlc", {}).get(f)
        if h and h > op["hlc"] and h.rsplit("-", 1)[-1] != node:
            out.append(f)
    return out


# ---------------------------------------------------------------- profile paths
def split_profile(partial: dict) -> dict:
    """A profile patch as record fields: top-level keys, except the two
    maps of §4.2, which become dotted paths ("conditions.nearLondon",
    "controllerMaps.qo100wb.ajazz-akp03e")."""
    out = {}
    for k, v in partial.items():
        depth = PROFILE_SPLIT.get(k)
        if depth and isinstance(v, dict):
            for path, leaf in _flatten(v, depth):
                out[f"{k}." + ".".join(path)] = leaf
        else:
            out[k] = v
    return out


def _flatten(d: dict, depth: int, prefix: tuple = ()):
    for k, v in d.items():
        if depth > 1 and isinstance(v, dict):
            yield from _flatten(v, depth - 1, prefix + (k,))
        else:
            yield prefix + (k,), v


def join_profile(fields: dict) -> dict:
    """Record fields back into the SERVICES.md §2.1 shape."""
    out: dict = {}
    for k in sorted(fields):
        head, _, rest = k.partition(".")
        if head in PROFILE_SPLIT and rest:
            node = out.setdefault(head, {})
            parts = rest.split(".", PROFILE_SPLIT[head] - 1)
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = fields[k]
        else:
            out[k] = fields[k]
    return out
