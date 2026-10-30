"""The validator and applier against the contract's worked examples: every
fixture is valid, each rule fires on a document broken in exactly one way,
and the applier replays the Aircraft stream and the Weather Sats poll to
the state a renderer should hold.

Ported with the code from the contract's fixture tool (uidoc's tests); the
fixtures are spec/vectors/ui2/fixtures/, copied from the contract
(openhamshack docs/contract/screens-fixtures, see the README there).
"""
import copy
import json
import unittest
from pathlib import Path

from shack_ui.check import check, check_action, check_typed, iter_parts
from shack_ui.ops import apply_ops, drawn_keys, state_from_document

FIX = Path(__file__).resolve().parents[3] / "spec" / "vectors" / "ui2" / "fixtures"


def load(rel):
    with open(FIX / rel) as f:
        return json.load(f)


def frames():
    with open(FIX / "streams" / "radio" / "aircraft.jsonl") as f:
        return [json.loads(line) for line in f if line.strip()]


AIRCRAFT = load("screens/radio/aircraft.json")


def codes(doc, **kw):
    return [p.code for p in check(doc, **kw)]


def find(doc, pid):
    for part, _, _ in iter_parts(doc["root"]):
        if part.get("id") == pid:
            return part
    raise KeyError(pid)


def header(doc):
    return find(doc, "header")["children"]


class FixturesValid(unittest.TestCase):
    def test_every_fixture_document_checks_clean(self):
        docs = [("launcher.json", "platform"), ("screens/radio/aircraft.json", "app"),
                ("screens/radio/wxsat.json", "app"), ("screens/radio/wxsat-viewer.json", "app")]
        for rel, kind in docs:
            with self.subTest(rel):
                self.assertEqual([str(p) for p in check(load(rel), base_kind=kind)], [])

    def test_launcher_opening_other_apps_is_refused_as_an_app_document(self):
        self.assertIn("cross_app_open", codes(load("launcher.json"), base_kind="app"))


class EachRule(unittest.TestCase):
    """Start from a copy of aircraft.json, break one thing, expect exactly
    that code."""

    def assertOnly(self, doc, code, **kw):
        got = codes(doc, **kw)
        self.assertTrue(got, f"expected {code}, got nothing")
        self.assertEqual(set(got), {code}, [str(p) for p in check(doc, **kw)])

    def doc(self):
        return copy.deepcopy(AIRCRAFT)

    def test_bad_version(self):
        d = self.doc()
        d["ui"] = "3.0"
        self.assertOnly(d, "bad_version")

    def test_unknown_part(self):
        d = self.doc()
        header(d).append({"type": "marquee", "text": "hi"})
        self.assertOnly(d, "unknown_part")

    def test_duplicate_id(self):
        d = self.doc()
        header(d).append({"type": "spacer", "id": "status"})
        self.assertOnly(d, "duplicate_id")

    def test_template_ids_may_repeat(self):
        d = self.doc()
        find(d, "list")["template"]["children"][0]["id"] = "rowBar"
        self.assertEqual(codes(d), [])

    def test_from_an_unknown_collection(self):
        d = self.doc()
        find(d, "list")["from"] = "ships"
        self.assertOnly(d, "bad_reference")

    def test_selection_from_an_unknown_collection(self):
        d = self.doc()
        d["selections"]["aircraft"]["from"] = "ships"
        self.assertIn("bad_reference", codes(d))

    def test_action_on_an_unknown_part(self):
        d = self.doc()
        d["keys"]["menu"]["part"] = "nowhere"
        self.assertOnly(d, "bad_reference")

    def test_bad_paths(self):
        for path in ("/api/x", "../x", "a/%2E%2E/b", "a/%2e%2e", "api\\x"):
            with self.subTest(path):
                d = self.doc()
                d["holds"][0]["start"]["path"] = path
                self.assertOnly(d, "bad_path")

    def test_absolute_call_path(self):
        d = self.doc()
        d["holds"][0]["start"]["path"] = "http://x/y"
        self.assertOnly(d, "absolute_url")

    def test_relative_tile_path_with_braces_passes(self):
        d = self.doc()
        find(d, "map")["ground"]["tiles"] = "api/map/tiles/{z}/{x}/{y}.png"
        self.assertEqual(codes(d), [])

    def test_image_on_a_host_off_the_allow_list(self):
        d = self.doc()
        header(d).append({"type": "image", "src": "https://example.com/a.jpg"})
        self.assertOnly(d, "absolute_url")

    def test_image_on_the_allow_list_passes(self):
        d = self.doc()
        header(d).append({"type": "image", "src": "https://t.plnspttrs.net/a.jpg",
                          "link": "https://www.planespotters.net/photo/1"})
        self.assertEqual(codes(d), [])

    def test_image_on_the_allow_list_over_http_is_refused(self):
        d = self.doc()
        header(d).append({"type": "image", "src": "http://t.plnspttrs.net/a.jpg"})
        self.assertOnly(d, "absolute_url")

    def test_open_another_app_from_an_app(self):
        d = self.doc()
        d["keys"]["info"] = {"do": "open", "app": "shack-icom", "screen": "icom/home"}
        self.assertOnly(d, "cross_app_open")

    def test_transmit_call_without_tx_or_lease(self):
        d = self.doc()
        header(d).append({"type": "button", "text": "CQ",
                          "action": {"do": "call", "method": "POST", "path": "api/cq", "level": "transmit"}})
        self.assertOnly(d, "transmit_action")

    def test_tx_call_with_a_lease_on_a_ptt_passes(self):
        d = self.doc()
        header(d).extend([
            {"type": "ptt", "id": "ptt", "key": "api/ptt", "heartbeat": "api/ptt/heartbeat", "holdMs": 1000},
            {"type": "button", "text": "CQ", "action": {"do": "call", "method": "POST", "path": "api/cq",
                                                       "level": "transmit", "tx": True, "lease": "ptt"}}])
        self.assertEqual(codes(d), [])

    def test_lease_naming_a_part_that_is_not_transmit(self):
        d = self.doc()
        header(d).append({"type": "button", "text": "CQ", "action": {
            "do": "call", "method": "POST", "path": "api/cq", "level": "transmit", "tx": True, "lease": "status"}})
        self.assertOnly(d, "bad_reference")

    def test_txarm_with_an_action(self):
        d = self.doc()
        header(d).append({"type": "txArm", "id": "arm", "arm": "api/autotx", "heartbeat": "api/ptt/heartbeat",
                          "action": {"do": "back"}})
        self.assertOnly(d, "transmit_part_action")

    def test_ptt_hold_below_a_second(self):
        d = self.doc()
        header(d).append({"type": "ptt", "id": "ptt", "key": "api/ptt", "holdMs": 500})
        self.assertOnly(d, "transmit_part_action")

    def test_ptt_with_another_timing(self):
        d = self.doc()
        header(d).append({"type": "ptt", "id": "ptt", "key": "api/ptt", "heartbeatMs": 5000})
        self.assertOnly(d, "transmit_part_action")

    def test_call_to_a_ptt_key_route(self):
        d = self.doc()
        header(d).extend([
            {"type": "ptt", "id": "ptt", "key": "api/ptt", "heartbeat": "api/ptt/heartbeat"},
            {"type": "button", "text": "Sneaky", "action": {"do": "call", "method": "POST", "path": "api/ptt?x=1",
                                                           "body": {"on": True}, "level": "control"}}])
        self.assertOnly(d, "key_route")

    def test_unknown_do(self):
        d = self.doc()
        d["keys"]["info"] = {"do": "eval", "code": "1"}
        self.assertOnly(d, "bad_action")

    def test_call_with_a_get(self):
        d = self.doc()
        d["holds"][0]["start"]["method"] = "GET"
        self.assertOnly(d, "bad_action")

    def test_unknown_do_inside_a_seq(self):
        d = self.doc()
        d["keys"]["info"] = {"do": "seq", "actions": [{"do": "back"}, {"do": "shell"}]}
        self.assertOnly(d, "bad_action")

    def test_evil_placeholder(self):
        d = self.doc()
        d["holds"][0]["start"]["body"] = {"client": "$evil"}
        self.assertOnly(d, "bad_placeholder")

    def test_evil_placeholder_in_a_path(self):
        d = self.doc()
        d["holds"][0]["stop"]["path"] = "api/adsb/session?client={$token}"
        self.assertOnly(d, "bad_placeholder")

    def test_binding_outside_a_template(self):
        d = self.doc()
        header(d).append({"type": "text", "value": {"$": "x"}})
        self.assertOnly(d, "template_binding")

    def test_binding_in_a_for_panel_passes(self):
        d = self.doc()
        find(d, "hud")["child"]["children"].append({"type": "text", "value": {"$": "squawk"}})
        self.assertEqual(codes(d), [])

    def test_countdown_with_four_texts(self):
        d = self.doc()
        header(d).append({"type": "text", "value": {"countdown": {
            "to": "2026-10-30T09:19:21Z", "then": "2026-10-30T09:34:21Z", "text": ["a", "b", "c", "d"]}}})
        self.assertOnly(d, "bad_typed")

    def test_countdown_without_then_needs_two_texts(self):
        self.assertEqual(check_typed({"countdown": {"to": "2026-10-30T09:34:21Z", "text": ["in {d}", "over"]}}), [])
        self.assertTrue(check_typed({"countdown": {"to": "2026-10-30T09:34:21Z", "text": ["a", "b", "c"]}}))

    def test_other_typed_values(self):
        good = [{"freq": 1090000000, "unit": "MHz", "decimals": 3}, {"ago": "2026-10-30T09:21:04Z"},
                {"since": "2026-10-30T09:21:04Z"}, {"clock": "local+utc", "style": "hms"},
                {"time": "2026-10-30T09:19:21Z", "zone": "local+utc", "style": "dayhm"},
                {"cycle": {"periodS": 15, "offsetS": 0}}, {"ago": {"$": "at"}}]
        bad = [{"freq": "1090"}, {"ago": "yesterday"}, {"clock": "mars"}, {"cycle": {"periodS": 0}},
               {"time": "2026-10-30T09:19:21Z", "zone": "moon"}]
        for v in good:
            self.assertEqual(check_typed(v), [], v)
        for v in bad:
            self.assertTrue(check_typed(v), v)

    def test_problems_come_in_a_stable_order(self):
        d = self.doc()
        d["ui"] = "1.0"
        header(d).append({"type": "marquee"})
        header(d).append({"type": "text", "value": {"$": "x"}})
        first = [str(p) for p in check(d)]
        self.assertEqual(first, [str(p) for p in check(copy.deepcopy(d))])
        self.assertEqual([p.split(" ")[0] for p in first], ["bad_version", "unknown_part", "template_binding"])


class ApplyOps(unittest.TestCase):
    def test_aircraft_stream(self):
        lines = frames()
        self.assertEqual(lines[0]["frame"]["t"], "hello")
        self.assertEqual(lines[1]["frame"]["t"], "snap")
        state = state_from_document(AIRCRAFT)
        self.assertTrue(state["parts"]["emergency"]["hidden"])
        removed = set()
        for line in lines[1:]:
            ms, frame = line["atMs"], line["frame"]
            before = state
            state = apply_ops(state, frame["ops"])
            self.assertIsNot(state, before)
            coll = state["collections"]["aircraft"]
            for op in frame["ops"]:
                if op["op"] == "rm":
                    removed.update(op["keys"])
            with self.subTest(ms=ms):
                self.assertTrue(drawn_keys(state, "aircraft"))
                self.assertTrue(all(k in coll["items"] for k in coll["order"]))
                self.assertFalse(removed & set(coll["items"]))
                for k, item in coll["items"].items():
                    self.assertLessEqual(len(item.get("trail", [])), 30, k)
                emergency_shown = not state["parts"]["emergency"].get("hidden")
                self.assertEqual(emergency_shown, 20_000 <= ms < 40_000)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(state["collections"]["aircraft"]["items"]), 40 + 2 - 2)
        self.assertEqual(len(drawn_keys(state, "aircraft")), 40)

    def test_set_null_deletes_a_property(self):
        s = state_from_document(AIRCRAFT)
        s = apply_ops(s, [{"op": "set", "id": "status", "v": {"text": "On", "tone": None}}])
        self.assertEqual(s["parts"]["status"]["text"], "On")
        self.assertNotIn("tone", s["parts"]["status"])

    def test_up_merges_creates_and_null_deletes_a_field(self):
        s = state_from_document(AIRCRAFT)
        s = apply_ops(s, [{"op": "up", "c": "aircraft", "items": {"A": {"lat": 1, "label": "X"}}},
                                {"op": "up", "c": "aircraft", "items": {"A": {"lat": 2, "label": None}}}])
        self.assertEqual(s["collections"]["aircraft"]["items"]["A"], {"lat": 2})

    def test_up_on_an_unknown_collection_creates_nothing(self):
        s = state_from_document(AIRCRAFT)
        t = apply_ops(s, [{"op": "up", "c": "ships", "items": {"A": {"lat": 1}}}])
        self.assertEqual(t["collections"], s["collections"])

    def test_push_keeps_the_last_max(self):
        s = state_from_document(AIRCRAFT)
        ops = [{"op": "up", "c": "aircraft", "items": {"A": {}}}]
        ops += [{"op": "push", "c": "aircraft", "key": "A", "f": "trail", "v": [[i, i]], "max": 3} for i in range(5)]
        ops.append({"op": "push", "c": "aircraft", "key": "nobody", "f": "trail", "v": [[0, 0]]})
        s = apply_ops(s, ops)
        self.assertEqual(s["collections"]["aircraft"]["items"]["A"]["trail"], [[2, 2], [3, 3], [4, 4]])
        self.assertNotIn("nobody", s["collections"]["aircraft"]["items"])

    def test_unknown_op_and_unknown_targets_are_ignored(self):
        s = state_from_document(AIRCRAFT)
        t = apply_ops(s, [{"op": "explode"}, {"op": "set", "id": "nowhere", "v": {"x": 1}},
                                {"op": "rm", "c": "ships", "keys": ["A"]}, "junk"])
        self.assertEqual(t, s)

    def test_order_with_an_unknown_key_is_tolerated(self):
        s = state_from_document(AIRCRAFT)
        s = apply_ops(s, [{"op": "order", "c": "airports", "keys": ["EGPF", "NOPE", "EGPK"]}])
        self.assertEqual(drawn_keys(s, "airports"), ["EGPF", "EGPK", "EGPH", "EGPN"])

    def test_append_keeps_the_last_max(self):
        doc = {"ui": "2.0", "root": {"type": "chart", "id": "snr", "marks": [{"type": "line", "points": [[0, 1]]}]}}
        s = state_from_document(doc)
        s = apply_ops(s, [{"op": "append", "id": "snr", "mark": 0, "v": [[1, 2], [2, 3]], "max": 2},
                                {"op": "append", "id": "snr", "mark": 5, "v": [[9, 9]]}])
        self.assertEqual(s["charts"]["snr"], {0: [[1, 2], [2, 3]]})

    def test_replace_reload_and_toast(self):
        s = state_from_document(AIRCRAFT)
        self.assertIn("count", s["parts"])
        s = apply_ops(s, [{"op": "replace", "id": "nearest", "part": {
                                    "type": "panel", "child": {"type": "text", "id": "fresh", "value": "x"}}},
                                {"op": "reload"}, {"op": "toast", "text": "Hi", "tone": "warn", "s": 6}])
        self.assertNotIn("count", s["parts"])
        self.assertIn("fresh", s["parts"])
        self.assertEqual(s["parts"]["nearest"]["type"], "panel")
        self.assertTrue(s["reload"])
        self.assertEqual(s["toasts"], [{"text": "Hi", "tone": "warn", "s": 6}])

    def test_wxsat_poll(self):
        doc = load("screens/radio/wxsat.json")
        s = apply_ops(state_from_document(doc), load("polls/radio/wxsat.json")["ops"])
        keys = drawn_keys(s, "passes")
        self.assertEqual(len(keys), 5)
        self.assertEqual(keys, sorted(keys, key=lambda k: k.rsplit("-", 1)[1]))
        for k in keys:
            with self.subTest(k):
                action = s["collections"]["passes"]["items"][k]["action"]
                self.assertEqual(check_action(action, doc=doc, data=True), [])
        self.assertEqual(s["parts"]["tabs"]["badges"], {"live": "onAir"})

    def test_an_item_action_may_never_transmit(self):
        a = {"do": "call", "method": "POST", "path": "api/cq", "level": "transmit", "tx": True, "lease": "ptt"}
        self.assertEqual([p.code for p in check_action(a, data=True)], ["transmit_action"])


