"""The dataset's shape (JSON Schema) and its cross-row rules (the
validator), and that load() refuses a dataset it must not trust."""
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

import shack_bandplan as bp
from shack_bandplan import validate

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data" / "bandplan"
SCHEMA = json.loads((ROOT / "spec" / "schemas" / "bandplan.schema.json").read_text())


def validator_for(kind: str):
    schema = dict(SCHEMA, **{"$ref": f"#/$defs/{kind}"})
    return jsonschema.Draft202012Validator(schema)


def files_of_kind():
    yield "manifest", DATA_DIR / "manifest.json"
    yield "vocab", DATA_DIR / "vocab.json"
    yield "countries", DATA_DIR / "countries.json"
    yield "regions", DATA_DIR / "regions.json"
    yield "repeaters", DATA_DIR / "repeaters.json"
    yield "presets", DATA_DIR / "presets.json"
    for p in sorted((DATA_DIR / "regimes").glob("*.json")):
        yield "regime", p
    for p in sorted((DATA_DIR / "plans").glob("*.json")):
        yield "plan", p
    for p in sorted((DATA_DIR / "satellite").glob("*.json")):
        yield "satellite", p


@pytest.mark.parametrize("kind,path", list(files_of_kind()), ids=lambda x: getattr(x, "name", x))
def test_every_file_matches_the_schema(kind, path):
    errors = sorted(validator_for(kind).iter_errors(json.loads(path.read_text())), key=str)
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors[:10])


def test_the_dataset_has_no_problems():
    assert validate.dataset(bp.load()) == []


def test_the_validator_cli_exits_zero(capsys):
    assert validate.main([str(DATA_DIR)]) == 0
    assert "no problems" in capsys.readouterr().out


def test_seed_status_is_what_the_beta_needs():
    ds = bp.load()
    uk = ds.regimes["ofcom-2024"]
    assert (uk["status"], uk["partial"]) == ("checked", False)
    assert uk["coverage"] == {"loHz": 135_700, "hiHz": 10_500_000_000}
    others = [r for rid, r in ds.regimes.items() if rid != "ofcom-2024"]
    assert len(others) == 15 and all(r["status"] == "researched" and r["partial"] for r in others)
    # M1: the UK and Region 1 plans speak for 2200 m to 3 cm with nothing
    # left untranscribed; where their sources print nothing they say so.
    for pid in ("rsgb-2026", "iaru-r1"):
        pl = ds.plans[pid]
        assert (pl["partial"], pl["gaps"]) == (False, [])
        assert pl["coverage"] == {"loHz": 135_700, "hiHz": 10_500_000_000}
        for f in (136_000, 473_000, 10_368_100_000):
            assert any(s["loHz"] <= f < s["hiHz"] for s in pl["segments"]), (pid, f)
        assert pl["unplanned"]
    for pid in ("iaru-r2", "iaru-r3"):
        assert any(g["hiHz"] == 10_500_000_000 for g in ds.plans[pid]["gaps"])
    assert ds.regimes["ofcom-2024"]["permits"][0]["status"] == "researched"


@pytest.fixture
def copy(tmp_path):
    dest = tmp_path / "bandplan"
    shutil.copytree(DATA_DIR, dest)
    return dest


def rewrite(root: Path, rel: str, change):
    p = root / rel
    obj = json.loads(p.read_text())
    change(obj)
    p.write_text(json.dumps(obj))
    return obj


def remanifest(root: Path):
    import hashlib
    man = json.loads((root / "manifest.json").read_text())
    man["files"] = {k: hashlib.sha256((root / k).read_bytes()).hexdigest() for k in man["files"]}
    (root / "manifest.json").write_text(json.dumps(man))


def test_load_refuses_a_file_changed_behind_the_manifest(copy):
    rewrite(copy, "regimes/ofcom-2024.json", lambda r: r["allocations"][2]["limits"]["full"][0].update(watts=4000))
    with pytest.raises(bp.DataError, match="does not match"):
        bp.load(copy)


def test_load_refuses_an_unknown_schema(copy):
    rewrite(copy, "manifest.json", lambda m: m.update(schema=2))
    with pytest.raises(bp.DataError, match="schema"):
        bp.load(copy)


def problems_after(copy, rel, change):
    rewrite(copy, rel, change)
    remanifest(copy)
    return validate.dataset(bp.load(copy))


def test_validator_finds_two_answers_for_one_frequency(copy):
    def overlap(r):
        r["allocations"][2]["hiHz"] = r["allocations"][3]["loHz"] + 1000
    assert any("overlap" in p for p in problems_after(copy, "regimes/ofcom-2024.json", overlap))


def test_validator_finds_a_permit_on_no_refused_row(copy):
    def orphan(r):
        for row in r["refused"]:
            row.pop("permit", None)
    assert any("sits on no refused row" in p for p in problems_after(copy, "regimes/ofcom-2024.json", orphan))


def test_validator_finds_gaps_in_a_file_that_is_not_partial(copy):
    assert any("partial is false but gaps" in p
               for p in problems_after(copy, "plans/iaru-r2.json", lambda pl: pl.update(partial=False)))


def test_validator_finds_an_unplanned_span_that_has_a_row(copy):
    def bad(pl):
        pl["unplanned"][0]["hiHz"] = pl["unplanned"][0]["hiHz"] + 1_000_000
    assert any("overlaps a segment" in p for p in problems_after(copy, "plans/rsgb-2026.json", bad))


def test_validator_finds_an_advisory_that_could_never_warn(copy):
    def bad(pl):
        pl["advisories"][0].update(modes=None, maxBwHz=None)
    assert any("could never warn" in p for p in problems_after(copy, "plans/rsgb-2026.json", bad))


def test_validator_finds_a_row_without_a_source(copy):
    def nosrc(r):
        r["allocations"][0]["src"] = ""
    assert any("no source" in p for p in problems_after(copy, "regimes/bnetza-2024.json", nosrc))


def test_validator_finds_a_variant_on_an_unknown_condition(copy):
    def bad(r):
        r["permits"][0]["variants"][0]["condition"] = "inWales"
    assert any("inWales" in p for p in problems_after(copy, "regimes/ofcom-2024.json", bad))


def test_validator_finds_an_unlisted_file(copy):
    (copy / "plans" / "stray.json").write_text("{}")
    assert any("stray.json is not listed" in p for p in validate.dataset(bp.load(copy, verify=False)))
