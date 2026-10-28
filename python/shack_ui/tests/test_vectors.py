"""Runs spec/vectors/ui/, overlay/ and presence/ (CLIENT-UI-PLAN.md §3, §5,
§7.2). ui cases go through shack_ui.validate and must get the named error
they claim; each is also run through the bare schema, as a client that
does not name errors would. Overlay and presence live here because they
are JSON documents checked with the same schema machinery, and overlay
items use ui's action shape."""
import importlib
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

import shack_ui

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "spec" / "schemas"
VEC = ROOT / "spec" / "vectors"


def load(p):
    return json.loads(p.read_text())


REGISTRY = Registry().with_resources(
    [(s["$id"], Resource.from_contents(s)) for s in map(load, SCHEMAS.glob("*.schema.json")) if "$id" in s])
UI = load(VEC / "ui" / "documents.json")["cases"]
OVERLAY = load(VEC / "overlay" / "messages.json")["cases"]
PRESENCE = load(VEC / "presence" / "messages.json")["cases"]


def validator(name, ref=None):
    schema = load(SCHEMAS / name)
    if ref:
        schema = {"$ref": f"{schema['$id']}#/$defs/{ref}"}
    return jsonschema.Draft202012Validator(schema, registry=REGISTRY)


def test_packaged_schema_is_the_spec():
    # The wheel cannot reach spec/, so it carries a copy; this keeps the
    # copy from quietly becoming the contract.
    assert importlib.import_module("shack_ui.validate").schema() == load(SCHEMAS / "ui.schema.json")


@pytest.mark.parametrize("case", UI, ids=[c["id"] for c in UI])
def test_ui_document(case):
    want = case["expect"].get("error")
    if want is None:
        assert shack_ui.validate(case["document"]) == case["document"]
    else:
        with pytest.raises(shack_ui.UIError) as err:
            shack_ui.validate(case["document"])
        assert err.value.code == want
        assert isinstance(err.value, shack_ui.TransmitActionError) == want.startswith("transmit")
    assert validator("ui.schema.json").is_valid(case["document"]) == (want is None)


@pytest.mark.parametrize("case", OVERLAY, ids=[c["id"] for c in OVERLAY])
def test_overlay_message(case):
    assert validator("overlay.schema.json").is_valid(case["message"]) == case["valid"]


@pytest.mark.parametrize("case", PRESENCE, ids=[c["id"] for c in PRESENCE])
def test_presence_message(case):
    assert validator("presence.schema.json", case["def"]).is_valid(case["message"]) == case["valid"]
