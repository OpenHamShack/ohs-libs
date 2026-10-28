"""Runs spec/vectors/spectrum/ (CLIENT-UI-PLAN.md §2) against shack_spectrum.

The same files are what the Kotlin, Swift and JS decoders run; here they
also pin the reference encoder, so a change in behaviour fails until the
vectors are regenerated and the contract change is reviewed.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from shack_spectrum import LatestWins, LevelsWindow, Row, RowError, decode, encode, quantise, reduce

ROOT = Path(__file__).resolve().parents[3]
VEC = ROOT / "spec" / "vectors" / "spectrum"


def load(name):
    return json.loads((VEC / name).read_text())


ROWS = load("rows.json")["cases"]
DEC = load("decimation.json")
LEVELS = load("levels.json")["cases"]
GAPS = load("gaps.json")["cases"]
MESSAGES = load("messages.json")["cases"]


@pytest.mark.parametrize("case", ROWS, ids=[c["id"] for c in ROWS])
def test_row(case):
    data = (VEC / case["file"]).read_bytes()
    e = case["expect"]
    if "error" in e:
        with pytest.raises(RowError) as err:
            decode(data)
        assert err.value.code == e["error"]
        return
    r = decode(data)
    got = {"stream": r.stream, "seq": r.seq, "tMs": r.t_ms, "loHz": r.lo_hz, "colHz": r.col_hz, "depth": r.depth,
           "levelsChanged": r.levels_changed, "streamChanged": r.stream_changed, "gap": r.gap,
           "values": r.values.tolist()}
    assert got == e
    # Encoding what was decoded gives the file back, except flags this
    # version does not know, which a reader drops.
    want = bytearray(data)
    want[3] &= 0x0F
    assert encode(r) == bytes(want)


@pytest.mark.parametrize("case", DEC["cases"], ids=[c["id"] for c in DEC["cases"]])
def test_decimation(case):
    src, sub, e = DEC["source"], case["subscribe"], case["expect"]
    lo, col_hz, db = reduce(src["db"], src["loHz"], src["binHz"], sub["cols"], sub.get("loHz"), sub.get("hiHz"),
                            sub["reduce"])
    assert lo == pytest.approx(e["loHz"], abs=1e-6)
    assert col_hz == pytest.approx(e["colHz"], rel=1e-12)
    np.testing.assert_allclose(db, e["db"], rtol=0, atol=1e-9)
    lv = case["levels"]
    assert quantise(db, lv["dbFloor"], lv["dbStep"], sub["depth"]).tolist() == e["values"]


@pytest.mark.parametrize("case", LEVELS, ids=[c["id"] for c in LEVELS])
def test_levels(case):
    w = LevelsWindow()
    for i, row in enumerate(case["rows"]):
        changed = w.update(row["db"], row["tMs"])
        e = row["expect"]
        assert changed == e["changed"], f"row {i}"
        got = [w.floor, w.top, w.step(8), w.noise_db, w.peak_db]
        assert got == pytest.approx([e["dbFloor"], e["dbTop"], e["dbStep8"], e["noiseDb"], e["peakDb"]],
                                    abs=1e-9), f"row {i}"


@pytest.mark.parametrize("case", GAPS, ids=[c["id"] for c in GAPS])
def test_gaps(case):
    sent = []
    s = LatestWins(lambda client, payload: sent.append((client, decode(payload))))
    for op in case["ops"]:
        if "offer" in op:
            o = op["offer"]
            s.offer(o["client"], Row(o["stream"], 0, o["tMs"], 0.0, 1.0, np.array([0], np.uint8)))
        else:
            s.drained(op["drained"])
    got = [{"client": c, "stream": r.stream, "seq": r.seq, "tMs": r.t_ms, "gap": r.gap} for c, r in sent]
    assert got == case["expect"]["sent"]


@pytest.mark.parametrize("case", MESSAGES, ids=[c["id"] for c in MESSAGES])
def test_control_message(case):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "spec" / "schemas" / "spectrum.schema.json").read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(case["message"]) == case["valid"]


def test_levels_message_from_the_window_is_valid():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "spec" / "schemas" / "spectrum.schema.json").read_text())
    w = LevelsWindow()
    w.update(LEVELS[0]["rows"][0]["db"], 0)
    jsonschema.Draft202012Validator(schema).validate(w.message("main", depth=16))
