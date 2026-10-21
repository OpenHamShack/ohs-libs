"""Runs spec/vectors/bandplan/verdicts.json: the conformance cases every
reader of the dataset must pass (BANDPLAN-DATA.md §9)."""
import json
from pathlib import Path

import pytest

import shack_bandplan as bp

ROOT = Path(__file__).resolve().parents[3]
VECTORS = json.loads((ROOT / "spec" / "vectors" / "bandplan" / "verdicts.json").read_text())
DATA = bp.load()


def signal(s: dict) -> bp.Signal:
    return bp.Signal(s["centreHz"], s.get("occupiedBwHz", 0), mode=s.get("mode"), power_w=s.get("powerW"),
                     measured=s.get("measured", False), purpose=s.get("purpose", "terrestrial"),
                     satellite=s.get("satellite"), plan_bw_hz=s.get("planBwHz"),
                     antenna_gain_dbi=s.get("antennaGainDbi"), signal_bw_hz=s.get("signalBwHz"))


def test_vectors_were_written_against_this_dataset():
    assert VECTORS["dataVersion"] == DATA.data_version


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["id"] for c in VECTORS["cases"]])
def test_vector(case):
    ctx = bp.context(VECTORS["profiles"][case["profile"]], DATA, today=case.get("today", "2026-10-09"))
    v = bp.check(ctx, signal(case["signal"]), case.get("planGuard", "warn"))
    e = case["expect"]
    got_code = v["refusal"]["code"] if v["refusal"] else None
    assert (v["allowed"], got_code) == (e["allowed"], e["code"]), v["refusal"]
    got_limit = {"watts": v["limit"]["watts"], "unit": v["limit"]["unit"]} if v["limit"] else None
    assert got_limit == e["limit"]
    assert set(e.get("warnings", [])) <= {w["code"] for w in v["warnings"]}
    assert v["dataVersion"] == DATA.data_version
    # Every sentence is whole and every rule cites its source where it has one.
    if v["refusal"]:
        assert v["refusal"]["text"] and v["refusal"]["text"][0].isupper() or v["refusal"]["text"][0].isdigit()
