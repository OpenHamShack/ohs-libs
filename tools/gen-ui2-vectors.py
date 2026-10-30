#!/usr/bin/env python3
"""Write the ui/2 vectors (spec/vectors/ui2/, SCREENS.md §14) and refresh
shack_ui's packaged copies of spec/ui2/ and spec/schemas/ui2.schema.json.

The vectors are what the Python, Kotlin and Swift implementations are all
held to, so they are committed files, not something a test regenerates: a
change to them is a change to the contract, and it shows up in review as a
diff. Every case's verdict is written by hand below; the script computes
it again with shack_ui (and the schema with jsonschema) and stops if they
disagree, so a bug in shack_ui cannot be laundered into the vectors. The
one exception is the full state after replaying the Aircraft stream, which
no hand could write; those checkpoints are shack_ui's, pinned by the
hand-written facts the tests also check (40 drawn, the emergency shown
from 20 s to 40 s, every trail at most 30 points).

Deterministic: a rerun with no code change rewrites the same bytes.

    tools/gen-ui2-vectors.py                    write everything
    tools/gen-ui2-vectors.py --contract DIR     also say whether
        spec/vectors/ui2/fixtures/ still matches the contract's
        screens-fixtures folder DIR (exit 1 if not)

Run it with ~/build/venvs/ohs-libs/bin/python (needs jsonschema).
"""
from __future__ import annotations

import argparse
import copy
import filecmp
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "python" / "shack_ui" / "src")]

import jsonschema  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

from shack_ui import check, check_path, check_typed, ops  # noqa: E402

VEC = ROOT / "spec" / "vectors" / "ui2"
FIX = VEC / "fixtures"
SCHEMA = json.loads((ROOT / "spec" / "schemas" / "ui2.schema.json").read_text())
REG = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))


def schema_ok(value, ref: str | None = None) -> bool:
    s = SCHEMA if ref is None else {"$ref": f"{SCHEMA['$id']}#/$defs/{ref}"}
    return jsonschema.Draft202012Validator(s, registry=REG).is_valid(value)


def stop(what: str) -> None:
    sys.exit(f"gen-ui2-vectors: shack_ui disagrees with the hand-written answer: {what}")


def write(path: Path, head: dict, cases: list[dict]) -> None:
    """One case per line, so a diff shows which case moved."""
    lines = ["{"] + [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in head.items()]
    lines.append('  "cases": [')
    lines.append(",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in cases))
    lines += ["  ]", "}", ""]
    path.write_text("\n".join(lines))


def fixture(rel: str):
    return json.loads((FIX / rel).read_text())


# ------------------------------------------------------------------ documents
# A small screen with one of everything the rules look at, so each broken
# case is a few hundred bytes rather than a copy of aircraft.json.
BASE = {
    "ui": "2.0", "screen": "vector/base", "title": "Base", "form": "tv", "level": "control",
    "collections": {"things": {"items": {"a": {"label": "A", "lat": 55.0, "lon": -4.0}}, "order": ["a"]}},
    "selections": {"thing": {"from": "things"}},
    "holds": [{"name": "rx",
               "start": {"do": "call", "method": "POST", "path": "api/rx/session", "body": {"client": "$client"},
                         "level": "control", "timeoutS": 30},
               "stop": {"do": "call", "method": "DELETE", "path": "api/rx/session?client={$client}",
                        "level": "control"}}],
    "live": {"path": "ui/v2/live/vector/base"},
    "keys": {"menu": {"do": "show", "part": "menu", "hint": "Menu"}},
    "root": {"type": "stack", "children": [
        {"type": "map", "id": "map", "ground": {"tiles": "api/map/tiles/{z}/{x}/{y}.png", "land": "api/map/land"},
         "layers": [{"id": "things", "kind": "markers", "from": "things", "selection": "thing"}]},
        {"type": "row", "id": "bar", "children": [
            {"type": "text", "id": "status", "value": "Ready"},
            {"type": "ptt", "id": "ptt", "key": "api/ptt", "heartbeat": "api/ptt/heartbeat", "holdMs": 1000}]},
        {"type": "panel", "id": "list", "slot": "end", "child": {
            "type": "list", "id": "rows", "from": "things", "selection": "thing",
            "onActivate": {"do": "show", "part": "details"},
            "template": {"type": "text", "value": {"$": "label"}}}},
        {"type": "panel", "id": "details", "slot": "end", "for": "thing",
         "child": {"type": "field", "label": "Name", "value": {"$": "label"}}},
        {"type": "panel", "id": "menu", "slot": "end", "child": {"type": "text", "value": "Menu"}}]},
}


def bar(d):
    return d["root"]["children"][1]["children"]


def documents() -> None:
    cases = []

    def case(id_, doc, problems, *, schema: bool, base_kind="app", note="", ref=None):
        """`problems`: the hand-written [(code, where)], in the order the
        validator reports them."""
        got = [(p.code, p.where) for p in check(fixture(ref) if ref else doc, base_kind=base_kind)]
        if got != problems:
            stop(f"documents {id_}: check says {got}, want {problems}")
        if schema_ok(fixture(ref) if ref else doc) != schema:
            stop(f"documents {id_}: the schema says {not schema}, want {schema}")
        c = {"id": id_}
        c |= {"fixture": ref} if ref else {"document": doc}
        c |= {"baseKind": base_kind, "problems": [{"code": a, "where": b} for a, b in problems], "schema": schema}
        cases.append(c | ({"note": note} if note else {}))

    def broken(fn):
        d = copy.deepcopy(BASE)
        fn(d)
        return d

    for ref, kind in (("screens/radio/aircraft.json", "app"), ("screens/radio/wxsat.json", "app"),
                      ("screens/radio/wxsat-viewer.json", "app"), ("launcher.json", "platform")):
        case(Path(ref).stem + "-fixture", None, [], schema=True, base_kind=kind, ref=ref,
             note="a worked example of the contract: valid")
    case("launcher-as-app", None, [("cross_app_open", f"/root/children/{i}/action") for i in range(11)],
         schema=True, ref="launcher.json", note="only the platform's documents open another app: each of "
         "the 11 tiles that opens one is reported (the 12th, Logbook, is the platform's own screen)")
    case("base", copy.deepcopy(BASE), [], schema=True, note="the small screen the broken cases start from")

    case("bad-version", broken(lambda d: d.update(ui="3.0")), [("bad_version", "/ui")], schema=False)
    case("unknown-part", broken(lambda d: bar(d).append({"type": "marquee", "text": "hi"})),
         [("unknown_part", "/root/children/1/children/2")], schema=True,
         note="the schema allows it (a client draws the fallback); the server's own rule refuses it")
    case("unknown-part-with-fallback", broken(lambda d: bar(d).append(
        {"type": "marquee", "since": "2.9", "fallback": {"type": "text", "value": "hi"}})),
        [("unknown_part", "/root/children/1/children/2")], schema=True,
        note="still reported: the server knows every type of its own minor; a client draws the fallback")
    case("duplicate-id", broken(lambda d: bar(d).append({"type": "spacer", "id": "status"})),
         [("duplicate_id", "/root/children/1/children/2")], schema=True)
    case("template-ids-repeat", broken(lambda d: d["root"]["children"][2]["child"]["template"].update(id="row")),
         [], schema=True, note="ids inside a template repeat per item")
    case("from-unknown-collection", broken(lambda d: d["root"]["children"][2]["child"].update({"from": "ships"})),
         [("bad_reference", "/root/children/2/child/from")], schema=True)
    case("selection-from-unknown", broken(lambda d: d["selections"]["thing"].update({"from": "ships"})),
         [("bad_reference", "/selections/thing/from")], schema=True)
    case("show-unknown-part", broken(lambda d: d["keys"]["menu"].update(part="nowhere")),
         [("bad_reference", "/keys/menu")], schema=True)
    for i, path in enumerate(("/api/x", "../x", "a/%2E%2E/b", "a/%2e%2e", "api\\x")):
        case(f"bad-path-{i + 1}", broken(lambda d, path=path: d["holds"][0]["start"].update(path=path)),
             [("bad_path", "/holds/0/start/path")], schema=False, note=f"path {path!r}")
    case("absolute-call-path", broken(lambda d: d["holds"][0]["start"].update(path="http://x/y")),
         [("absolute_url", "/holds/0/start/path")], schema=False)
    case("braces-in-a-tile-path", copy.deepcopy(BASE), [], schema=True,
         note="api/map/tiles/{z}/{x}/{y}.png: braces that are not placeholders are fine in a part's path")
    case("image-off-the-allow-list", broken(lambda d: bar(d).append(
        {"type": "image", "src": "https://example.com/a.jpg"})),
        [("absolute_url", "/root/children/1/children/2/src")], schema=False)
    case("image-on-the-allow-list", broken(lambda d: bar(d).append(
        {"type": "image", "src": "https://t.plnspttrs.net/a.jpg", "link": "https://www.planespotters.net/photo/1",
         "credit": "Photo: A. N. Other", "qr": True})), [], schema=True, note="§9: the one built-in host")
    case("image-allow-list-over-http", broken(lambda d: bar(d).append(
        {"type": "image", "src": "http://t.plnspttrs.net/a.jpg"})),
        [("absolute_url", "/root/children/1/children/2/src")], schema=False)
    case("link-not-https", broken(lambda d: d["keys"].update(info={"do": "link", "url": "http://example.com"})),
         [("absolute_url", "/keys/info/url")], schema=False)
    case("open-another-app", broken(lambda d: d["keys"].update(info={"do": "open", "app": "shack-icom",
                                                                    "screen": "icom/home"})),
         [("cross_app_open", "/keys/info")], schema=True)
    case("open-another-app-as-platform", broken(lambda d: d["keys"].update(
        info={"do": "open", "app": "shack-icom", "screen": "icom/home"})), [], schema=True, base_kind="platform")
    case("transmit-without-tx", broken(lambda d: bar(d).append({"type": "button", "text": "CQ", "action": {
        "do": "call", "method": "POST", "path": "api/cq", "level": "transmit"}})),
        [("transmit_action", "/root/children/1/children/2/action")], schema=False,
        note="§7.2: level transmit needs tx: true and a lease")
    case("tx-without-lease", broken(lambda d: bar(d).append({"type": "button", "text": "CQ", "action": {
        "do": "call", "method": "POST", "path": "api/cq", "level": "transmit", "tx": True}})),
        [("transmit_action", "/root/children/1/children/2/action")], schema=False)
    case("tx-with-a-ptt-lease", broken(lambda d: bar(d).append({"type": "button", "text": "CQ", "action": {
        "do": "call", "method": "POST", "path": "api/cq", "level": "transmit", "tx": True, "lease": "ptt"}})),
        [], schema=True, note="§7.2: drawn in the transmit tone, enabled only while this client holds ptt's lease")
    case("lease-not-a-transmit-part", broken(lambda d: bar(d).append({"type": "button", "text": "CQ", "action": {
        "do": "call", "method": "POST", "path": "api/cq", "level": "transmit", "tx": True, "lease": "status"}})),
        [("bad_reference", "/root/children/1/children/2/action")], schema=True)
    case("txarm-with-an-action", broken(lambda d: bar(d).append({"type": "txArm", "id": "arm", "arm": "api/autotx",
                                                                 "heartbeat": "api/ptt/heartbeat",
                                                                 "action": {"do": "back"}})),
         [("transmit_part_action", "/root/children/1/children/2")], schema=False)
    case("ptt-hold-below-a-second", broken(lambda d: bar(d)[1].update(holdMs=500)),
         [("transmit_part_action", "/root/children/1/children/1")], schema=False)
    case("ptt-with-another-timing", broken(lambda d: bar(d)[1].update(heartbeatMs=5000)),
         [("transmit_part_action", "/root/children/1/children/1")], schema=False,
         note="§7.1: the 1 s/3 s/5 s timings are client code, never properties")
    case("ptt-on-tap", broken(lambda d: bar(d)[1].update(tap={"do": "back"})),
         [("transmit_part_action", "/root/children/1/children/1")], schema=False)
    case("call-to-a-key-route", broken(lambda d: bar(d).append({"type": "button", "text": "Sneaky", "action": {
        "do": "call", "method": "POST", "path": "api/ptt?x=1", "body": {"on": True}, "level": "control"}})),
        [("key_route", "/root/children/1/children/2/action")], schema=True,
        note="§7.3: whatever its level; the schema cannot see it, the renderer must")
    case("item-action-transmits", broken(lambda d: d["collections"]["things"]["items"]["a"].update(action={
        "do": "call", "method": "POST", "path": "api/cq", "level": "transmit", "tx": True, "lease": "ptt"})),
        [("transmit_action", "/collections/things/items/a/action")], schema=False,
        note="§7.2: an item's own action (data) may never transmit")
    case("item-action-records", broken(lambda d: d["collections"]["things"]["items"]["a"].update(action={
        "do": "call", "method": "POST", "path": "api/plan/a", "body": {"plan": "record"}, "level": "control"})),
        [], schema=True)
    case("unknown-do", broken(lambda d: d["keys"].update(info={"do": "eval", "code": "1"})),
         [("bad_action", "/keys/info")], schema=False)
    case("call-with-get", broken(lambda d: d["holds"][0]["start"].update(method="GET")),
         [("bad_action", "/holds/0/start")], schema=False)
    case("call-without-level", broken(lambda d: d["holds"][0]["start"].pop("level")),
         [("bad_action", "/holds/0/start")], schema=False, note="every call names its level")
    case("local-cycle-is-not-a-typed-value", broken(lambda d: d["keys"].update(
        info={"do": "local", "key": "map.style", "cycle": ["real", "night"]})), [], schema=True,
        note="an action's fields are not values: `cycle` here is §5.1's, not §2.1's typed value")
    case("spectrum-overlay-flag", broken(lambda d: bar(d).append(
        {"type": "spectrum", "id": "scope", "stream": {"path": "api/spectrum/v1", "name": "main"}, "overlay": True,
         "tap": {"do": "call", "method": "POST", "path": "api/set", "body": {"changes": {"vfo": "$hz"}},
                 "level": "control"}})), [], schema=True,
        note="§3.7: a spectrum's `overlay` is a flag; only a video's is a part")
    case("video-overlay-part", broken(lambda d: bar(d).append(
        {"type": "video", "id": "rx", "src": "api/rx/stream.ts", "ticket": True,
         "overlay": {"type": "text", "id": "mer", "value": "MER 9.1 dB"}})), [], schema=True)
    case("video-overlay-broken", broken(lambda d: bar(d).append(
        {"type": "video", "id": "rx", "src": "api/rx/stream.ts", "overlay": {"type": "image", "src": "/x.png"}})),
        [("bad_path", "/root/children/1/children/2/overlay/src")], schema=False,
        note="a video's overlay is a part and is checked as one")
    case("unknown-do-in-a-seq", broken(lambda d: d["keys"].update(info={"do": "seq", "actions": [
        {"do": "back"}, {"do": "shell"}]})), [("bad_action", "/keys/info/actions/1")], schema=False)
    case("evil-placeholder", broken(lambda d: d["holds"][0]["start"].update(body={"client": "$token"})),
         [("bad_placeholder", "/holds/0/start/body/client")], schema=False)
    case("evil-placeholder-in-a-path", broken(lambda d: d["holds"][0]["stop"].update(
        path="api/rx/session?client={$token}")), [("bad_placeholder", "/holds/0/stop/path")], schema=False)
    case("binding-outside-a-template", broken(lambda d: bar(d).append({"type": "text", "value": {"$": "x"}})),
         [("template_binding", "/root/children/1/children/2/value")], schema=True)
    case("binding-in-a-for-panel", broken(lambda d: d["root"]["children"][3]["child"].update(
        label={"$": "kind"})), [], schema=True, note="a `for` container's children read the selected item")
    case("countdown-four-texts", broken(lambda d: bar(d).append({"type": "text", "value": {"countdown": {
        "to": "2026-10-30T09:19:21Z", "then": "2026-10-30T09:34:21Z", "text": ["a", "b", "c", "d"]}}})),
        [("bad_typed", "/root/children/1/children/2/value")], schema=False)
    d = broken(lambda d: (d.update(ui="1.0"), bar(d).append({"type": "marquee"}),
                          bar(d).append({"type": "text", "value": {"$": "x"}})))
    case("problems-in-order", d, [("bad_version", "/ui"), ("unknown_part", "/root/children/1/children/2"),
                                  ("template_binding", "/root/children/1/children/3/value")], schema=False,
         note="the document-level rules first, then the parts in document order, depth first")
    write(VEC / "documents.json", {
        "description": "ui/2 screen documents and both verdicts. `problems` is what check.py (shack_ui) reports, "
                       "in its order, each {code, where} with `where` a JSON pointer; [] is valid. `schema` is "
                       "whether spec/schemas/ui2.schema.json accepts the document: the schema checks shape, and "
                       "what needs the whole document (references, leases, bindings in templates, duplicate "
                       "ids, cross-app opens, key routes) is the validator's. A case gives the `document` "
                       "inline, or a `fixture` path under fixtures/. `baseKind` is \"platform\" for the "
                       "launcher and platform screens (the only ones that may open another app).",
        "version": 1}, cases)


# ------------------------------------------------------------------ typed values and paths
def typed() -> None:
    cases = []

    def case(id_, value, ok, note=""):
        if (check_typed(value) == []) != ok:
            stop(f"typed {id_}: check_typed says {check_typed(value)}")
        cases.append({"id": id_, "value": value, "ok": ok} | ({"note": note} if note else {}))

    case("freq", {"freq": 1090000000, "unit": "MHz", "decimals": 3}, True)
    case("freq-bound", {"freq": {"$": "hz"}, "unit": "kHz"}, True)
    case("freq-string", {"freq": "1090"}, False)
    case("freq-decimals-negative", {"freq": 1, "decimals": -1}, False)
    case("ago", {"ago": "2026-10-30T09:21:04Z"}, True)
    case("ago-bound", {"ago": {"$": "at"}}, True)
    case("ago-offset", {"ago": "2026-10-30T10:21:04+01:00"}, True)
    case("ago-fraction", {"ago": "2026-10-30T09:21:04.250Z"}, True)
    case("ago-words", {"ago": "yesterday"}, False)
    case("ago-no-zone", {"ago": "2026-10-30T09:21:04"}, False, "an ISO time names its zone")
    case("since", {"since": "2026-10-30T09:21:04Z"}, True)
    case("clock", {"clock": "local+utc", "style": "hms"}, True)
    case("clock-mars", {"clock": "mars"}, False)
    case("clock-style", {"clock": "utc", "style": "dayhm"}, False, "a clock is hm or hms")
    case("time", {"time": "2026-10-30T09:19:21Z", "zone": "local+utc", "style": "dayhm"}, True)
    case("time-zone", {"time": "2026-10-30T09:19:21Z", "zone": "moon"}, False)
    case("countdown-three", {"countdown": {"to": "2026-10-30T09:19:21Z", "then": "2026-10-30T09:34:21Z",
                                           "text": ["in {d}", "up, sets in {d}", "over"]}}, True)
    case("countdown-two", {"countdown": {"to": "2026-10-30T09:34:21Z", "text": ["in {d}", "over"]}}, True)
    case("countdown-two-with-then", {"countdown": {"to": "2026-10-30T09:19:21Z", "then": "2026-10-30T09:34:21Z",
                                                   "text": ["in {d}", "over"]}}, False)
    case("countdown-three-without-then", {"countdown": {"to": "2026-10-30T09:34:21Z",
                                                        "text": ["a", "b", "c"]}}, False)
    case("countdown-no-to", {"countdown": {"text": ["a", "b"]}}, False)
    case("cycle", {"cycle": {"periodS": 15, "offsetS": 0}}, True)
    case("cycle-zero", {"cycle": {"periodS": 0}}, False)
    case("not-typed", {"label": "x"}, True, "a dict naming no typed kind is not checked as one")
    write(VEC / "typed.json", {
        "description": "Typed values (SCREENS.md §2.1) and whether each is well formed (check.py check_typed). "
                       "The first of freq, ago, countdown, since, clock, time, cycle present names the kind.",
        "version": 1}, cases)


def paths() -> None:
    cases = []

    def case(id_, value, kind, code, note=""):
        out = []
        check_path(value, "/x", out, kind=kind)
        got = out[0].code if out else None
        if got != code:
            stop(f"paths {id_}: check_path says {got}, want {code}")
        cases.append({"id": id_, "value": value, "kind": kind, "problem": code} | ({"note": note} if note else {}))

    case("relative", "api/adsb/session", "path", None)
    case("relative-query", "api/adsb/session?client={$client}", "path", None)
    case("braces", "api/map/tiles/{z}/{x}/{y}.png", "path", None)
    case("dots-in-a-name", "api/files/a..b.png", "path", None, "only a whole `..` segment is refused")
    case("leading-slash", "/api/x", "path", "bad_path")
    case("dotdot", "../x", "path", "bad_path")
    case("dotdot-inside", "a/../b", "path", "bad_path")
    case("dotdot-encoded", "a/%2E%2E/b", "path", "bad_path")
    case("dotdot-encoded-lower", "a/%2e%2e", "path", "bad_path")
    case("dotdot-half-encoded", "a/.%2e/b", "path", "bad_path")
    case("backslash", "api\\x", "path", "bad_path")
    case("backslash-encoded", "api%5cx", "path", "bad_path")
    case("scheme", "http://x/y", "path", "absolute_url")
    case("protocol-relative", "//x/y", "path", "absolute_url")
    case("javascript", "javascript:alert(1)", "path", "absolute_url")
    case("image-relative", "ui/v2/art/radio.png", "image", None)
    case("image-allowed-host", "https://t.plnspttrs.net/a.jpg", "image", None)
    case("image-allowed-apex", "https://plnspttrs.net/a.jpg", "image", None)
    case("image-lookalike-host", "https://evilplnspttrs.net/a.jpg", "image", "absolute_url",
         "the host must be the listed one or under it, not merely end with its name")
    case("image-allowed-host-http", "http://t.plnspttrs.net/a.jpg", "image", "absolute_url")
    case("image-other-host", "https://example.com/a.jpg", "image", "absolute_url")
    case("link-https", "https://www.planespotters.net/photo/1", "link", None)
    case("link-http", "http://www.planespotters.net/photo/1", "link", "absolute_url")
    case("link-relative", "photo/1", "link", "absolute_url", "a link is an https URL a person may open")
    write(VEC / "paths.json", {
        "description": "Paths and URLs (SCREENS.md §9, §11) and the problem check.py's check_path reports, or "
                       "null. kind: \"path\" (relative to the document's base: a call, a stream, a tile), "
                       "\"image\" (relative, or https on the client's built-in allow-list, today "
                       "plnspttrs.net and its subdomains), \"link\" (an https URL a person may open). Decoding "
                       "is applied before the `..` and backslash checks, so no spelling gets past them.",
        "version": 1}, cases)


# ------------------------------------------------------------------ ops
OPS_DOC = {
    "ui": "2.0", "screen": "vector/ops", "level": "view",
    "collections": {"c": {"items": {"a": {"label": "A", "n": 1}, "b": {"label": "B"}}, "order": ["a", "b"]}},
    "root": {"type": "column", "children": [
        {"type": "text", "id": "status", "value": "Starting", "tone": "warn"},
        {"type": "panel", "id": "side", "child": {"type": "column", "children": [
            {"type": "text", "id": "inner", "value": "x"}]}},
        {"type": "chart", "id": "snr", "marks": [{"mark": "line", "points": [[0, 1]]}, {"mark": "line"}]},
        {"type": "list", "id": "rows", "from": "c", "template": {"type": "text", "id": "cell", "value": {"$": "label"}}},
        {"type": "spectrum", "id": "scope", "stream": {"path": "api/spectrum/v1"}, "overlay": True},
        {"type": "video", "id": "rx", "src": "api/rx/stream.ts", "overlay": {"type": "text", "id": "mer", "value": "x"}}]},
}


def opscases() -> None:
    cases = []
    doc_state = ops.state_from_document(OPS_DOC)

    def case(id_, steps, want, note="", doc=None, ref=None):
        """`steps`: a list of op lists (or frames, as {"frame": ...});
        `want`: a function of the final public state that must hold,
        written by hand, so the recorded state is checked, not trusted."""
        d = fixture(ref) if ref else (doc or OPS_DOC)
        start = ops.state_from_document(d)
        s = start
        out = []
        for st in steps:
            if isinstance(st, dict) and "frame" in st:
                s = ops.apply_frame(s, st["frame"], start)
            else:
                s = ops.apply_ops(s, st)
            out.append({**st} if isinstance(st, dict) else {"ops": st})
        final = ops.public_state(s)
        if not want(final):
            stop(f"ops {id_}: the hand-written check fails on {json.dumps(final)[:400]}")
        c = {"id": id_}
        c |= {"fixture": ref} if ref else ({"document": doc} if doc else {"document": "base"})
        cases.append(c | {"steps": out, "state": final} | ({"note": note} if note else {}))

    case("start", [], lambda s: s["drawn"]["c"] == ["a", "b"] and s["parts"]["status"]["value"] == "Starting"
         and "inner" in s["parts"] and s["charts"]["snr"] == [[[0, 1]], []] and "cell" not in s["parts"]
         and s["parts"]["scope"]["overlay"] is True and "overlay" not in s["parts"]["rx"] and "mer" in s["parts"],
         "the state a renderer starts from: id'd parts flat with their own properties (no children), "
         "template ids not indexed, chart points per mark; a spectrum's overlay flag is a property, a "
         "video's overlay part is a child")
    case("set-merges-and-null-deletes", [[{"op": "set", "id": "status", "v": {"value": "On", "tone": None}}]],
         lambda s: s["parts"]["status"] == {"type": "text", "id": "status", "value": "On"})
    case("set-unknown-part", [[{"op": "set", "id": "nowhere", "v": {"x": 1}}]],
         lambda s: "nowhere" not in s["parts"], "ignored")
    case("up-merges-creates-and-null-deletes", [[
        {"op": "up", "c": "c", "items": {"a": {"n": 2, "label": None}, "z": {"label": "Z"}}}]],
         lambda s: s["collections"]["c"]["items"]["a"] == {"n": 2} and s["drawn"]["c"] == ["a", "b", "z"],
         "a new key is drawn after those in `order` until an `order` names it")
    case("up-unknown-collection", [[{"op": "up", "c": "ships", "items": {"a": {"x": 1}}}]],
         lambda s: list(s["collections"]) == ["c"], "creates nothing")
    case("rm-also-leaves-the-order", [[{"op": "rm", "c": "c", "keys": ["a", "nobody"]}],
                                      [{"op": "up", "c": "c", "items": {"a": {"label": "A again"}}}]],
         lambda s: s["collections"]["c"]["order"] == ["b"] and s["drawn"]["c"] == ["b", "a"],
         "a removed key leaves `order` too, so an `up` that brings it back draws it last")
    case("order-with-unknown-keys", [[{"op": "order", "c": "c", "keys": ["b", "nope", "a"]}]],
         lambda s: s["drawn"]["c"] == ["b", "a"] and s["collections"]["c"]["order"] == ["b", "nope", "a"],
         "kept as sent; a key with no item is not drawn")
    case("order-partial", [[{"op": "order", "c": "c", "keys": ["b"]}]],
         lambda s: s["drawn"]["c"] == ["b", "a"], "items missing from `order` are drawn last, sorted by key")
    case("push-keeps-the-last-max", [[{"op": "push", "c": "c", "key": "a", "f": "trail", "v": [[i, i]], "max": 3}
                                      for i in range(5)],
                                     [{"op": "push", "c": "c", "key": "nobody", "f": "trail", "v": [[0, 0]]}]],
         lambda s: s["collections"]["c"]["items"]["a"]["trail"] == [[2, 2], [3, 3], [4, 4]]
         and "nobody" not in s["collections"]["c"]["items"], "push to a missing item is ignored")
    case("push-several-without-max", [[{"op": "push", "c": "c", "key": "b", "f": "trail", "v": [[1, 1], [2, 2]]}]],
         lambda s: s["collections"]["c"]["items"]["b"]["trail"] == [[1, 1], [2, 2]])
    case("append-keeps-the-last-max", [[{"op": "append", "id": "snr", "mark": 0, "v": [[1, 2], [2, 3]], "max": 2},
                                        {"op": "append", "id": "snr", "mark": 1, "v": [[5, 5]]},
                                        {"op": "append", "id": "snr", "mark": 9, "v": [[9, 9]]}]],
         lambda s: s["charts"]["snr"] == [[[1, 2], [2, 3]], [[5, 5]]], "an unknown mark is ignored")
    case("append-default-mark", [[{"op": "append", "id": "snr", "v": [[3, 3]]}]],
         lambda s: s["charts"]["snr"][0] == [[0, 1], [3, 3]], "`mark` defaults to 0")
    case("replace-drops-the-old-subtree", [[{"op": "replace", "id": "side", "part": {
        "type": "panel", "id": "other", "child": {"type": "text", "id": "fresh", "value": "y"}}}]],
         lambda s: "inner" not in s["parts"] and s["parts"]["fresh"]["value"] == "y"
         and s["parts"]["side"]["type"] == "panel" and "other" not in s["parts"],
         "the new part keeps the id it replaces")
    case("reload-and-toast", [[{"op": "reload"}, {"op": "toast", "text": "Hi", "tone": "warn", "s": 6}]],
         lambda s: s["reload"] is True and s["toasts"] == [{"text": "Hi", "tone": "warn", "s": 6}])
    case("unknown-op-and-junk", [[{"op": "explode"}, "junk", {"op": "rm", "c": "ships", "keys": ["a"]}]],
         lambda s: s == ops.public_state(doc_state), "ignored, all of it")
    case("ops-apply-in-order", [[{"op": "up", "c": "c", "items": {"q": {"label": "Q"}}},
                                 {"op": "rm", "c": "c", "keys": ["q"]},
                                 {"op": "up", "c": "c", "items": {"q": {"n": 1}}}]],
         lambda s: s["collections"]["c"]["items"]["q"] == {"n": 1}, "within a frame, in order")
    case("snap-starts-from-the-document", [
        {"frame": {"t": "p", "seq": 2, "ops": [{"op": "up", "c": "c", "items": {"gone": {"label": "G"}}},
                                               {"op": "rm", "c": "c", "keys": ["b"]},
                                               {"op": "set", "id": "status", "v": {"value": "Old"}}]}},
        {"frame": {"t": "snap", "seq": 1, "ops": [{"op": "up", "c": "c", "items": {"new": {"label": "N"}}},
                                                  {"op": "order", "c": "c", "keys": ["new", "a", "b"]}]}}],
         lambda s: s["drawn"]["c"] == ["new", "a", "b"] and "gone" not in s["collections"]["c"]["items"]
         and s["parts"]["status"]["value"] == "Starting",
         "a `snap` (after a reconnect) is applied to the document's own state, not on top of what was held")
    case("hello-and-ping-change-nothing", [{"frame": {"t": "hello", "rev": "x", "maxHz": 2}},
                                           {"frame": {"t": "ping"}}],
         lambda s: s == ops.public_state(doc_state))

    # The worked examples: the Weather Sats poll, and the whole Aircraft minute.
    poll = fixture("polls/radio/wxsat.json")
    case("wxsat-poll", [poll["ops"]], lambda s: len(s["drawn"]["passes"]) == 5
         and s["parts"]["tabs"]["badges"] == {"live": "onAir"}, ref="screens/radio/wxsat.json",
         note="fixtures/polls/radio/wxsat.json applied to the Weather Sats document")
    write(VEC / "ops.json", {
        "description": "Op application (SCREENS.md §4.2) and live frames (§4.1). Each case starts from a "
                       "document's state (`document`: \"base\" is the file's `base` document, a fixture path "
                       "under fixtures/, or inline), applies each step in turn (a list of ops, or {\"frame\": "
                       "...} for a socket frame) and must reach `state`: id'd parts flat with their own "
                       "properties (children, and child, template, empty, fallback and overlay when they hold a part, left out; a tabs "
                       "part's tabs without their child), collections as held, `drawn` (each collection's keys "
                       "as drawn: those in `order` that have an item, then the rest sorted by key), chart "
                       "points per mark, toasts and reload. A snap frame applies to the document's own state.",
        "version": 1, "base": OPS_DOC}, cases)


def aircraft_stream() -> None:
    """The Aircraft minute: the whole fixture stream with the state at the
    snap, at 30 s (the emergency shown) and at the end."""
    doc = fixture("screens/radio/aircraft.json")
    start = ops.state_from_document(doc)
    lines = [json.loads(x) for x in (FIX / "streams" / "radio" / "aircraft.jsonl").read_text().splitlines() if x.strip()]
    s = start
    checkpoints = []
    for i, line in enumerate(lines):
        s = ops.apply_frame(s, line["frame"], start)
        t = line["frame"].get("t")
        if t == "snap" or line["atMs"] == 30_000 or i == len(lines) - 1:
            pub = ops.public_state(s)
            shown = not pub["parts"]["emergency"].get("hidden")
            trails_ok = all(len(it.get("trail", [])) <= 30 for it in pub["collections"]["aircraft"]["items"].values())
            if len(pub["drawn"]["aircraft"]) != 40 or shown != (line["atMs"] == 30_000) or not trails_ok:
                stop(f"aircraft stream at line {i}: drawn {len(pub['drawn']['aircraft'])}, emergency {shown}")
            checkpoints.append({"line": i, "atMs": line["atMs"], "state": pub})
    (VEC / "aircraft-stream.json").write_text(json.dumps({
        "description": "fixtures/streams/radio/aircraft.jsonl replayed on fixtures/screens/radio/aircraft.json: "
                       "after applying the frame on 0-based line `line` (apply_frame: the snap to the document's "
                       "state, each p frame on top), the public state must be `state` (as ops.json). Written by "
                       "shack_ui, not by hand: the generator stops unless 40 aircraft are drawn at each "
                       "checkpoint, the emergency badge shows exactly at 30 s and no trail is over 30 points.",
        "version": 1, "document": "screens/radio/aircraft.json", "stream": "streams/radio/aircraft.jsonl",
        "checkpoints": checkpoints}, ensure_ascii=False, separators=(",", ":")) + "\n")


# ------------------------------------------------------------------ packaged data
def package_data() -> None:
    """shack_ui reads the symbol sets, icon names and ui2.schema.json at
    run time and a wheel cannot reach spec/, so it carries copies."""
    data = ROOT / "python" / "shack_ui" / "src" / "shack_ui" / "data"
    dst = data / "ui2"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(ROOT / "spec" / "ui2", dst)
    (data / "schemas").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "spec" / "schemas" / "ui2.schema.json", data / "schemas" / "ui2.schema.json")


def compare_contract(contract: Path) -> bool:
    ours = sorted(p.relative_to(FIX) for p in FIX.rglob("*") if p.is_file() and p.name != "README.md")
    theirs = sorted(p.relative_to(contract) for p in contract.rglob("*") if p.is_file())
    same = ours == theirs and all(filecmp.cmp(FIX / p, contract / p, shallow=False) for p in ours)
    if not same:
        print("gen-ui2-vectors: spec/vectors/ui2/fixtures/ differs from", contract)
        for p in sorted(set(ours) ^ set(theirs)):
            print("  only in one:", p)
        for p in sorted(set(ours) & set(theirs)):
            if not filecmp.cmp(FIX / p, contract / p, shallow=False):
                print("  differs:", p)
    return same


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--contract", type=Path, help="the contract's screens-fixtures folder to compare with")
    a = ap.parse_args()
    package_data()
    documents()
    typed()
    paths()
    opscases()
    aircraft_stream()
    print("gen-ui2-vectors: written")
    if a.contract and not compare_contract(a.contract):
        sys.exit(1)


if __name__ == "__main__":
    main()
