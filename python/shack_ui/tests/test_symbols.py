"""The shared symbol sets and icon names: what an app gets from
Symbols.load, and the packaged copies being the spec's."""
import json
from pathlib import Path

import pytest

from shack_ui.check import iter_parts
from shack_ui.symbols import SETS, Symbols, icon_names

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "spec" / "ui2"
FIX = ROOT / "spec" / "vectors" / "ui2" / "fixtures"
PKG = ROOT / "python" / "shack_ui" / "src" / "shack_ui" / "data" / "ui2"


def test_packaged_copies_are_the_spec():
    # The wheel cannot reach spec/, so it carries a copy; this keeps the
    # copy from quietly becoming the source (tools/gen-ui2-vectors.py
    # refreshes it).
    spec = sorted(p.relative_to(SPEC) for p in SPEC.rglob("*.json"))
    assert spec == sorted(p.relative_to(PKG) for p in PKG.rglob("*.json"))
    for rel in spec:
        assert json.loads((PKG / rel).read_text()) == json.loads((SPEC / rel).read_text()), rel


def test_every_set_loads_and_says_where_it_came_from():
    for name in SETS:
        d = json.loads((SPEC / "symbols" / f"{name}.json").read_text())
        assert d["set"] == name and d["description"]
        assert Symbols.load(name).names() == list(d["symbols"])


def test_aircraft_and_station_are_the_contract_example_s_marks():
    # The Aircraft worked example draws these; a box using the shared set
    # must send exactly what the renderers were built against.
    fixture = json.loads((FIX / "screens" / "radio" / "aircraft.json").read_text())["symbols"]
    assert (Symbols.load("aircraft") + Symbols.load("station")).to_json() == fixture


def test_only_takes_a_few_and_refuses_a_missing_name():
    s = Symbols.load("aprs").only("car", "default")
    assert list(s.to_json()) == ["car", "default"]
    with pytest.raises(KeyError):
        Symbols.load("aprs").only("unicorn")
    with pytest.raises(KeyError):
        Symbols.load("dragons")


def test_aprs_codes_map_to_symbols_and_anything_else_to_default():
    aprs = Symbols.load("aprs")
    assert aprs.for_aprs("/>") == "car"
    assert aprs.for_aprs("/-") == "house"
    assert aprs.for_aprs("\\?") == "default"
    assert set(aprs.table.values()) <= set(aprs.names())


def test_to_json_is_a_copy():
    s = Symbols.load("ships")
    s.to_json()["vessel"]["paths"].clear()
    assert Symbols.load("ships").to_json()["vessel"]["paths"]


def test_every_icon_a_fixture_names_is_in_the_client_s_set():
    names = icon_names()
    used = set()
    for f in FIX.rglob("*.json"):
        doc = json.loads(f.read_text())
        if isinstance(doc.get("root"), dict):
            used |= {p["icon"] for p, _, _ in iter_parts(doc["root"]) if isinstance(p.get("icon"), str)}
        used |= {e["icon"] for e in doc.get("entries", []) if isinstance(e, dict) and "icon" in e}
    assert used and used <= names, used - names


def test_the_packaged_schema_is_the_spec_and_checks_shapes():
    from shack_ui import schema, schema_errors
    assert schema() == json.loads((ROOT / "spec" / "schemas" / "ui2.schema.json").read_text())
    assert schema_errors(json.loads((FIX / "screens" / "radio" / "aircraft.json").read_text())) == []
    assert schema_errors({"op": "rm", "c": "x"}, "op") == ["/: 'keys' is a required property"]
