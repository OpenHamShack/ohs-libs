#!/usr/bin/env python3
"""Write the client-contract vectors of CLIENT-UI-PLAN.md (step 0).

spec/vectors/spectrum/, overlay/, ui/ and presence/ are what the Kotlin,
Swift and JS clients will be tested against, so they are committed files,
not something a test regenerates: a change to them is a change to the
contract, and it shows up in review as a diff.

Spectrum expectations are computed by shack_spectrum (it is the reference
encoder); every case that has an obvious right answer also carries that
answer written by hand below, and the script stops if the reference
disagrees, so a bug in shack_spectrum cannot be laundered into the
vectors. The JSON-document cases (ui, overlay, presence) are written by
hand entirely; the script checks each against the schema and shack_ui
and stops if one does not get the verdict it claims.

Deterministic: every pseudo-random source row comes from a fixed seed and
is rounded to 0.1 dB, so a rerun with no code change rewrites the same
bytes. It also refreshes shack_ui's packaged copy of ui.schema.json.

Usage: tools/gen-client-vectors.py     (needs numpy and jsonschema; run it
with ~/build/venvs/ohs-libs/bin/python)
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "python" / "shack_spectrum" / "src"), str(ROOT / "python" / "shack_ui" / "src")]

import jsonschema  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

import shack_ui  # noqa: E402
from shack_spectrum import LatestWins, LevelsWindow, Row, RowError, decode, encode, quantise, reduce  # noqa: E402

VEC = ROOT / "spec" / "vectors"
SCHEMAS = ROOT / "spec" / "schemas"
T0 = 1_767_225_600_000  # 2026-01-01T00:00:00Z: capture times are ms since the epoch


def write(path: Path, head: dict, cases: list[dict]) -> None:
    """One case per line: diffs then show which case moved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["{"] + [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in head.items()]
    lines.append('  "cases": [')
    lines.append(",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in cases))
    lines += ["  ]", "}", ""]
    path.write_text("\n".join(lines))


def check(cond: bool, what: str) -> None:
    if not cond:
        sys.exit(f"gen-client-vectors: the reference disagrees with the hand-written answer: {what}")


def noise(seed: int, n: int, mean: float, sd: float = 2.0) -> np.ndarray:
    return np.round(np.random.default_rng(seed).normal(mean, sd, n), 1)


def registry() -> Registry:
    res = [(s["$id"], Resource.from_contents(s)) for s in
           (json.loads(p.read_text()) for p in SCHEMAS.glob("*.schema.json")) if "$id" in s]
    return Registry().with_resources(res)


def validator(name: str, ref: str | None = None):
    schema = json.loads((SCHEMAS / name).read_text())
    if ref:
        schema = {"$ref": f"{schema['$id']}#/$defs/{ref}"}
    return jsonschema.Draft202012Validator(schema, registry=registry())


# ------------------------------------------------------------------ rows
def rows() -> None:
    out = VEC / "spectrum" / "rows"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cases = []

    def good(name, row, note, mutate=None):
        data = encode(row)
        if mutate:
            data = mutate(bytearray(data))
        (out / f"{name}.bin").write_bytes(bytes(data))
        r = decode(bytes(data))
        cases.append({"id": name, "file": f"rows/{name}.bin", "note": note, "expect": {
            "stream": r.stream, "seq": r.seq, "tMs": r.t_ms, "loHz": r.lo_hz, "colHz": r.col_hz, "depth": r.depth,
            "levelsChanged": r.levels_changed, "streamChanged": r.stream_changed, "gap": r.gap,
            "values": r.values.tolist()}})
        return data

    def bad(name, data, code, note):
        (out / f"{name}.bin").write_bytes(bytes(data))
        try:
            decode(bytes(data))
            got = None
        except RowError as e:
            got = getattr(e, "code", None)
        check(got == code, f"{name} should fail with {code}, got {got}")
        cases.append({"id": name, "file": f"rows/{name}.bin", "note": note, "expect": {"error": code}})

    u8 = good("u8-basic", Row(0, 7, T0 + 125, 144_100_000.0, 312.5,
                              np.array([0, 1, 2, 127, 128, 200, 254, 255], np.uint8)),
              "8-bit, no flags; header bytes are the §2.3 table exactly")
    check(u8[:8].hex() == "4f53010000000800" and len(u8) == 40, "u8-basic header")
    good("u16-levels", Row(1, 70000, T0, 10_489_500_000.0, 1171.875,
                           np.array([0, 1, 256, 4095, 65534, 65535], np.uint16), levels_changed=True),
         "16-bit (depth16), levelsChanged, a 10 GHz loHz that needs f64, seq past 16 bits")
    good("flags-all", Row(3, 0xFFFFFFFF, T0 + 999, 7_000_000.0, 0.5, np.array([9, 9, 9], np.uint8),
                          levels_changed=True, stream_changed=True, gap=True),
         "every defined flag; stream 3; largest seq")
    good("empty", Row(0, 1, T0, 0.0, 1.0, np.array([], np.uint8)), "zero columns is a valid row")
    good("unknown-flag", Row(0, 2, T0, 144e6, 100.0, np.array([5, 6], np.uint8)),
         "bit 4 set: flags a reader does not know are ignored, so spectrum/1 can add one",
         mutate=lambda b: b.__setitem__(3, b[3] | 0x10) or b)
    bad("bad-magic", b"XS" + u8[2:], "magic", "not OS: refuse, never guess")
    bad("bad-version", u8[:2] + b"\x02" + u8[3:], "version", "version 2 is a different layout")
    bad("short-header", u8[:20], "length", "shorter than the 32-byte header")
    bad("extra-byte", u8 + b"\x00", "length", "a frame is one row: trailing bytes mean the layouts disagree")
    bad("missing-column", u8[:-1], "length", "fewer bytes than the column count says")
    write(VEC / "spectrum" / "rows.json", {
        "description": "spectrum/1 binary rows (CLIENT-UI-PLAN.md §2.3). Each case is a file in rows/ and either "
                       "the decoded header and values or the error code a decoder must refuse it with: magic, "
                       "version or length (a frame is exactly one row). Written by tools/gen-client-vectors.py.",
        "version": 1}, cases)


# ------------------------------------------------------------ decimation
def decimation() -> None:
    lo, bin_hz, n = 144_100_000.0, 1000.0, 400
    src = noise(1, n, -130.0)
    src[100] = -60.0                      # a one-bin carrier: peak keeps it, mean dilutes it
    src[250:254] = [-90.0, -85.0, -85.0, -90.0]
    floor, step = -140.0, 0.375
    source = {"loHz": lo, "binHz": bin_hz, "db": src.tolist()}
    cases = []

    def case(id_, note, cols, mode, lo_hz=None, hi_hz=None, depth=8, want=None):
        got_lo, col_hz, db = reduce(src, lo, bin_hz, cols, lo_hz, hi_hz, mode)
        if want:
            check((got_lo, col_hz, len(db)) == want, f"{id_}: {(got_lo, col_hz, len(db))} != {want}")
        sub = {"cols": cols, "reduce": mode, "depth": depth} | (
            {"loHz": lo_hz} if lo_hz is not None else {}) | ({"hiHz": hi_hz} if hi_hz is not None else {})
        cases.append({"id": id_, "note": note, "subscribe": sub, "levels": {"dbFloor": floor, "dbStep": step},
                      "expect": {"loHz": got_lo, "colHz": col_hz, "db": db.tolist(),
                                 "values": quantise(db, floor, step, depth).tolist()}})
        return db

    pk = case("whole-peak-80", "5 bins a column", 80, "peak", want=(lo, 5000.0, 80))
    check(pk[20] == -60.0, "peak keeps a one-bin carrier")
    mn = case("whole-mean-80", "power mean, not dB mean", 80, "mean", want=(lo, 5000.0, 80))
    check(-68.0 < mn[20] < -66.0, "mean of one -60 dB bin among four noise bins is about -67 dB")
    case("crop-peak-50", "zoom inside the span: 144.2-144.3 MHz", 50, "peak", 144_200_000.0, 144_300_000.0,
         want=(144_200_000.0, 2000.0, 50))
    case("crop-uneven-mean-37", "crop edges inside bins widen to whole bins; 37 does not divide the bins, so "
         "widths differ by one", 37, "mean", 144_123_456.7, 144_387_654.3, want=(144_123_000.0, 265000 / 37, 37))
    case("more-cols-than-bins", "1280 columns asked over 20 bins gives 20: no invented resolution", 1280, "peak",
         144_195_000.0, 144_215_000.0, want=(144_195_000.0, 1000.0, 20))
    case("beyond-span", "a crop wider than the span is clipped to it (zooming out further is a tune)", 64,
         "peak", 144_000_000.0, 144_600_000.0, want=(lo, 6250.0, 64))
    case("depth16", "16-bit values from the same window", 40, "mean", depth=16, want=(lo, 10000.0, 40))
    write(VEC / "spectrum" / "decimation.json", {
        "description": "Per-client decimation (CLIENT-UI-PLAN.md §2.1) and quantisation (§2.3). source is one row "
                       "of dB bins, bin i covering loHz + i*binHz up to the next. The crop is widened to whole "
                       "bins [floor((loHz-src)/binHz), ceil((hiHz-src)/binHz)) and clipped to the source; with "
                       "m bins and n = min(cols, m) columns, column k takes bins [k*m//n, (k+1)*m//n) of the "
                       "crop. peak is the max; mean is 10*log10 of the mean of 10^(dB/10). values = "
                       "clamp(floor((dB - dbFloor)/dbStep + 0.5), 0, 2^depth - 1). Compare db within 1e-9.",
        "version": 1, "source": source}, cases)


# ---------------------------------------------------------------- levels
def levels() -> None:
    n, dt = 64, 250
    cases = []

    def scenario(id_, note, frames, want_changed):
        w = LevelsWindow()
        steps, changed_at = [], []
        for i, db in enumerate(frames):
            t = T0 + i * dt
            moved = w.update(db, t)
            if moved:
                changed_at.append(i)
            steps.append({"tMs": t, "db": db.tolist(), "expect": {
                "changed": moved, "dbFloor": w.floor, "dbTop": w.top, "dbStep8": w.step(8),
                "noiseDb": w.noise_db, "peakDb": w.peak_db}})
        check(changed_at == want_changed, f"{id_}: window moved at rows {changed_at}, want {want_changed}")
        cases.append({"id": id_, "note": note, "rows": steps})

    def quiet(seed, mean=-130.0, carrier=-80.0):
        r = noise(seed, n, mean)
        if carrier is not None:
            r[30:33] = carrier
        return r

    scenario("steady", "noise jitter only: set once on the first row, never again",
             [quiet(100 + i) for i in range(12)], [0])
    scenario("short-rise", "noise up 15 dB for 1.5 s then back: under 2 s, no move",
             [quiet(200 + i, -115.0 if 2 <= i < 8 else -130.0) for i in range(14)], [0])
    scenario("sustained-rise", "noise up 15 dB from row 2 (t=500 ms): moves at the first row 2 s later, row 10",
             [quiet(300 + i, -115.0 if i >= 2 else -130.0) for i in range(14)], [0, 10])
    scenario("small-shift", "noise up 2 dB for good: within the 3 dB hysteresis, no move",
             [quiet(400 + i, -128.0 if i >= 2 else -130.0) for i in range(14)], [0])
    scenario("carrier-gone", "a carrier 70 dB up holds the top high; when it goes for 2 s the top comes down "
             "(either edge, either direction)", [quiet(500 + i, carrier=-60.0 if i < 3 else None) for i in range(14)],
             [0, 11])
    scenario("interrupted", "out for 1.75 s, in for one row, out again: the 2 s restarts",
             [quiet(600 + i, -130.0 if i in (0, 8) else -115.0) for i in range(18)], [0, 17])
    write(VEC / "spectrum" / "levels.json", {
        "description": "The server's levels window with hysteresis (CLIENT-UI-PLAN.md §2.3). Feed each row's dB "
                       "bins with its capture time; after each, expect the window. Wanted floor = 20th "
                       "percentile - 10; wanted top = max(floor + 60, 99.7th percentile + 6); percentiles are "
                       "linear between closest ranks (numpy default, R type 7). The first row sets the window. "
                       "After that it moves to the wanted window only once either edge has been more than 3 dB "
                       "from the current one on every row for at least 2000 ms (measured from the first such "
                       "row); a row within 3 dB at both edges restarts the wait. dbStep8 = (top - floor)/256. "
                       "noiseDb is the 20th percentile and peakDb the maximum, of every row. Compare within 1e-9.",
        "version": 1}, cases)


# ------------------------------------------------------------------ gaps
def gaps() -> None:
    cases = []

    def scenario(id_, note, ops, want):
        sent = []
        s = LatestWins(lambda client, payload: sent.append((client, decode(payload))))
        for op in ops:
            if "offer" in op:
                o = op["offer"]
                s.offer(o["client"], Row(o["stream"], 0, o["tMs"], 0.0, 1.0, np.array([o["tMs"] % 256], np.uint8)))
            else:
                s.drained(op["drained"])
        got = [{"client": c, "stream": r.stream, "seq": r.seq, "tMs": r.t_ms, "gap": r.gap} for c, r in sent]
        check(got == want, f"{id_}: sent {got}")
        cases.append({"id": id_, "note": note, "ops": ops, "expect": {"sent": got}})

    def off(client, stream, t):
        return {"offer": {"client": client, "stream": stream, "tMs": T0 + t}}

    def sent(client, stream, seq, t, gap=False):
        return {"client": client, "stream": stream, "seq": seq, "tMs": T0 + t, "gap": gap}

    scenario("keeps-up", "every write drains before the next row: nothing dropped",
             [off("a", 0, 0), {"drained": "a"}, off("a", 0, 66), {"drained": "a"}, off("a", 0, 133)],
             [sent("a", 0, 0, 0), sent("a", 0, 1, 66), sent("a", 0, 2, 133)])
    scenario("one-dropped", "two rows arrive while the socket is busy: the older is replaced, the newer goes "
             "with gap, and seq jumps 0 to 2",
             [off("a", 0, 0), off("a", 0, 66), off("a", 0, 133), {"drained": "a"}, {"drained": "a"},
              off("a", 0, 200)],
             [sent("a", 0, 0, 0), sent("a", 0, 2, 133, True), sent("a", 0, 3, 200)])
    scenario("held-not-dropped", "one row waits while busy: it is sent on drain with no gap",
             [off("a", 0, 0), off("a", 0, 66), {"drained": "a"}],
             [sent("a", 0, 0, 0), sent("a", 0, 1, 66)])
    scenario("per-stream-per-client", "a slow client drops on one stream only; another client is unaffected; "
             "pending rows go in stream order",
             [off("a", 0, 0), off("b", 0, 0), off("a", 1, 10), off("a", 0, 66), off("a", 0, 133),
              {"drained": "b"}, off("b", 0, 66), {"drained": "a"}],
             [sent("a", 0, 0, 0), sent("b", 0, 0, 0), sent("b", 0, 1, 66), sent("a", 0, 2, 133, True),
              sent("a", 1, 0, 10)])
    write(VEC / "spectrum" / "gaps.json", {
        "description": "Latest wins (CLIENT-UI-PLAN.md §2.4). Run ops in order against a sender with an injected "
                       "send. offer: a new row for client/stream; it is sent at once if the client's socket is "
                       "idle (the socket is then busy until drained), else held as that stream's one pending row, "
                       "replacing any row already pending. drained: the socket is idle; each pending row goes, in "
                       "stream order. seq is stamped per client and stream when a row is offered; the first row "
                       "sent after a replacement carries gap. expect.sent lists every row sent, in order.",
        "version": 1}, cases)


# ------------------------------------------------------- spectrum control messages
def messages() -> None:
    v = validator("spectrum.schema.json")
    cases = []

    def case(id_, msg, valid, note=""):
        check(v.is_valid(msg) == valid, f"spectrum message {id_} should be {'valid' if valid else 'invalid'}")
        cases.append({"id": id_, "message": msg, "valid": valid} | ({"note": note} if note else {}))

    case("subscribe-plan", {"type": "subscribe", "stream": "main", "cols": 1280, "fps": 15, "loHz": 144100000,
                            "hiHz": 144500000, "reduce": "peak", "depth": 8}, True, "§2.1's example")
    case("subscribe-minimal", {"type": "subscribe", "stream": "zoom", "cols": 640}, True)
    case("subscribe-depth12", {"type": "subscribe", "stream": "main", "cols": 640, "depth": 12}, False,
         "depth is 8 or 16")
    case("subscribe-median", {"type": "subscribe", "stream": "main", "cols": 640, "reduce": "median"}, False)
    case("subscribe-no-cols", {"type": "subscribe", "stream": "main"}, False)
    case("stream-plan", {"type": "stream", "stream": "main", "rev": 7, "centreHz": 144300000, "spanHz": 2400000,
                         "loHz": 143100000, "hiHz": 145500000, "binHz": 1171.875, "rbwHz": 1757.8, "srcBins": 2048,
                         "rate": 20.0, "units": "dB", "calibrated": False, "dcBins": [1023, 1025],
                         "pinned": {"by": ["ais-1"]}, "label": "Wide"}, True, "§2.2's example")
    case("stream-dbuv", {"type": "stream", "stream": "main", "rev": 1, "centreHz": 1, "spanHz": 1, "loHz": 0,
                         "hiHz": 1, "binHz": 1, "srcBins": 1, "rate": 1, "units": "dBuV", "calibrated": True},
         False, "units is dBm, dB or rel")
    case("levels-plan", {"type": "levels", "stream": "main", "dbFloor": -142.0, "dbStep": 0.375,
                         "noiseDb": -131.5, "peakDb": -62.0}, True, "§2.3's example")
    case("levels-zero-step", {"type": "levels", "stream": "main", "dbFloor": -142.0, "dbStep": 0,
                              "noiseDb": -131.5, "peakDb": -62.0}, False)
    case("error-busy", {"type": "error", "code": "busy"}, True, "§2.4: the ninth client")
    case("unknown-type", {"type": "hello"}, False)
    write(VEC / "spectrum" / "messages.json", {
        "description": "spectrum/1 JSON control messages against spec/schemas/spectrum.schema.json: valid or not.",
        "version": 1}, cases)


# ------------------------------------------------------------------- overlay
def overlay() -> None:
    v = validator("overlay.schema.json")
    rpt = {"id": "rpt:GB3XX", "kind": "channel", "layer": "channels", "loHz": 145787500, "hiHz": 145787500,
           "centreHz": 145787500, "label": "GB3XX", "short": "GB3XX", "detail": "Repeater · 88.5 Hz · 12 km",
           "prio": 60, "readAtHzPerPx": 400, "role": "channel.repeater", "active": False, "expires": None,
           "source": "local-frequencies",
           "actions": [{"id": "tune", "label": "Tune", "do": "post", "path": "api/set",
                        "body": {"vfo": 145787500, "mode": "fm"}, "level": "control"}]}
    seg = {"id": "seg:2m-cw", "kind": "segment", "layer": "band", "loHz": 144000000, "hiHz": 144110000,
           "label": "CW", "prio": 10, "readAtHzPerPx": None, "role": "segment.cw"}
    spot = {"id": "spot:1", "kind": "spot", "layer": "spots", "loHz": 144174000, "hiHz": 144176000,
            "label": "GM4BRI FT8", "prio": 70, "active": True, "expires": T0 + 600_000, "role": "spot.ft8"}
    cases = []

    def case(id_, msg, valid, note=""):
        check(v.is_valid(msg) == valid, f"overlay {id_} should be {'valid' if valid else 'invalid'}")
        cases.append({"id": id_, "message": msg, "valid": valid} | ({"note": note} if note else {}))

    def env(op, items, rev=41):
        return {"type": "overlay", "rev": rev, "op": op, "items": items}

    case("snapshot-plan", env("snapshot", [rpt, seg]), True, "§3.2's example item and a band segment")
    case("upsert-spot", env("upsert", [spot], 42), True)
    case("remove-by-id", env("remove", [{"id": "spot:1"}], 43), True, "a remove needs only ids")
    case("snapshot-empty", env("snapshot", []), True)
    case("transmit-action", env("upsert", [rpt | {"actions": [{"id": "tx", "label": "Call", "do": "post",
                                                              "path": "api/tx", "level": "transmit"}]}]), False,
         "§3.2/§5.6: never a transmit action")
    case("action-without-label", env("upsert", [rpt | {"actions": [{"id": "tune", "do": "post", "path": "api/set",
                                                                    "level": "control"}]}]), False,
         "item actions need id and label")
    case("unknown-kind", env("upsert", [spot | {"kind": "aircraft"}]), False)
    case("prio-over-100", env("upsert", [spot | {"prio": 101}]), False)
    case("rgb-role", env("upsert", [spot | {"role": "#ff0000"}]), False, "a role, never RGB")
    case("upsert-id-only", env("upsert", [{"id": "spot:1"}]), False, "only remove may carry bare ids")
    write(VEC / "overlay" / "messages.json", {
        "description": "overlay/1 envelopes and items (CLIENT-UI-PLAN.md §3) against "
                       "spec/schemas/overlay.schema.json, which needs ui.schema.json registered for the action "
                       "shape: valid or not.",
        "version": 1}, cases)


# ------------------------------------------------------------------------ ui
def ui() -> None:
    sv = validator("ui.schema.json")
    plan = {"ui": "1.0", "screen": "radio/home", "rev": "b13-4f2a", "title": "Radio",
            "data": {"state": "ws:api/events?kinds=state,meter", "stations": "get:api/dab/stations"},
            "root": {"c": "column", "children": [
                {"c": "nowPlaying", "title": {"bind": "state.dab.service"}, "subtitle": {"bind": "state.dab.dls"},
                 "image": {"bind": "state.dab.slideUrl"}},
                {"c": "native", "name": "waterfall", "v": 1,
                 "props": {"stream": "main", "layers": ["band", "dial", "stations"]},
                 "fallback": {"c": "text", "text": "This needs a newer OpenHamShack app."}},
                {"c": "row", "children": [
                    {"c": "button", "label": "Scan", "icon": "scan",
                     "action": {"do": "post", "path": "api/dab/scan", "level": "control"}},
                    {"c": "slider", "label": "Volume", "value": {"bind": "state.volume"}, "min": 0, "max": 1,
                     "action": {"do": "post", "path": "api/set", "body": {"volume": "$value"},
                                "level": "control"}}]}]}}

    def doc(root, **kw):
        return {"ui": "1.0", "screen": "test/screen", "root": root} | kw

    def button(action):
        return {"c": "button", "label": "Go", "action": action}

    cases = []

    def case(id_, d, error=None, note=""):
        try:
            shack_ui.validate(d)
            got = None
        except shack_ui.UIError as e:
            got = e.code
        check(got == error, f"ui {id_}: shack_ui says {got}, want {error}")
        check(sv.is_valid(d) == (error is None), f"ui {id_}: schema disagrees")
        cases.append({"id": id_, "document": d, "expect": {"error": error} if error else {"ok": True}}
                     | ({"note": note} if note else {}))

    case("plan-example", plan, note="§5.2's example, unchanged")
    case("every-action", doc({"c": "column", "children": [
        button({"do": "open", "screen": "radio/dab", "params": {"band": "III"}}),
        button({"do": "back"}),
        button({"do": "local", "key": "layers.spots", "value": False}),
        button({"do": "native", "verb": "waterfall.zoomIn"}),
        button({"do": "confirm", "text": "Stop the scan?",
                "action": {"do": "post", "path": "api/dab/stop", "level": "control"}})]}))
    case("ptt-placed", doc({"c": "row", "children": [
        {"c": "native", "name": "enableTx", "v": 1}, {"c": "native", "name": "ptt", "v": 1, "tv": False}]}),
         note="the server places ptt/enableTx; their behaviour is client code")
    case("polled-feed", doc({"c": "value", "value": {"bind": "status.vfo", "format": "freq"}},
                            data={"status": {"src": "get:api/status", "poll": 5}}))
    case("unknown-property-ignored", doc({"c": "text", "text": "hi", "sparkle": True}),
         note="§5.3: unknown properties are ignored, so a newer server can add one")
    case("transmit-post", doc(button({"do": "post", "path": "api/tx", "level": "transmit"})), "transmit_action",
         "§5.6: no generic action may transmit")
    case("transmit-in-confirm", doc(button({"do": "confirm", "text": "Key up?",
                                            "action": {"do": "post", "path": "api/ptt", "level": "transmit"}})),
         "transmit_action", "wrapping it in a question does not make it allowed")
    case("transmit-in-fallback", doc({"c": "native", "name": "waterfall", "v": 1,
                                      "fallback": button({"do": "post", "path": "api/tx", "level": "transmit"})}),
         "transmit_action")
    case("ptt-with-action", doc({"c": "native", "name": "ptt", "v": 1,
                                 "action": {"do": "post", "path": "api/ptt", "level": "control"}}),
         "transmit_component_action", "ptt/enableTx take no generic action, whatever its level")
    case("unknown-component", doc({"c": "marquee", "text": "hi"}), "unknown_component",
         "a closed vocabulary: the server sends a fallback instead")
    case("unknown-native", doc({"c": "native", "name": "spectrumAnalyser", "v": 1}), "unknown_native")
    case("unknown-action", doc(button({"do": "eval", "code": "1"})), "unknown_action")
    case("absolute-path", doc(button({"do": "post", "path": "/api/set", "level": "control"})), "schema",
         "relative to the app's base, never absolute")
    case("post-without-level", doc(button({"do": "post", "path": "api/set"})), "schema",
         "every post declares its level")
    case("ui-2", plan | {"ui": "2.0"}, "schema", "ui 2 is a different contract")
    write(VEC / "ui" / "documents.json", {
        "description": "ui/1 screen documents (CLIENT-UI-PLAN.md §5) and what a validator must say: ok, or the "
                       "named error. Every error case is also invalid against spec/schemas/ui.schema.json, so "
                       "a client that only runs the schema refuses the same documents; the names are what "
                       "shack_ui (and any port that names errors) reports, checked node by node, depth first, in this order: "
                       "unknown_component, unknown_native, transmit_component_action, unknown_action, "
                       "transmit_action, then the schema (schema).",
        "version": 1}, cases)


# ------------------------------------------------------------------ presence
def presence() -> None:
    cases = []

    def case(id_, ref, msg, valid, note=""):
        check(validator("presence.schema.json", ref).is_valid(msg) == valid, f"presence {id_}")
        cases.append({"id": id_, "def": ref, "message": msg, "valid": valid} | ({"note": note} if note else {}))

    renew = {"client": "remote-7f3a", "app": "shack-radio", "screen": "radio/dab", "holds": ["dab", "audio"]}
    case("renew-plan", "renew", renew, True, "§7.2's example")
    case("renew-nothing-held", "renew", {"client": "remote-7f3a", "app": "shack-radio"}, True)
    case("renew-no-client", "renew", {"app": "shack-radio"}, False)
    case("renew-duplicate-hold", "renew", renew | {"holds": ["dab", "dab"]}, False)
    case("lease-plan", "lease", {"leaseS": 30, "renewEveryS": 10}, True)
    case("list", "list", {"clients": [renew]}, True)
    case("arrived", "arrived", renew, True)
    case("moved", "moved", renew | {"screen": "radio/fm", "holds": ["audio"]}, True)
    case("gone-goodbye", "gone", {"client": "remote-7f3a", "why": "goodbye"}, True)
    case("gone-expired", "gone", {"client": "remote-7f3a", "why": "expired"}, True)
    case("gone-kicked", "gone", {"client": "remote-7f3a", "why": "kicked"}, False, "why is goodbye or expired")
    write(VEC / "presence" / "messages.json", {
        "description": "Presence service bodies and SSE event data (CLIENT-UI-PLAN.md §7.2) against "
                       "spec/schemas/presence.schema.json: def names the $defs entry the message is checked "
                       "against.",
        "version": 1}, cases)


def main() -> None:
    shutil.copyfile(SCHEMAS / "ui.schema.json", ROOT / "python" / "shack_ui" / "src" / "shack_ui" / "ui.schema.json")
    rows()
    decimation()
    levels()
    gaps()
    messages()
    overlay()
    ui()
    presence()
    print("gen-client-vectors: written")


if __name__ == "__main__":
    main()
