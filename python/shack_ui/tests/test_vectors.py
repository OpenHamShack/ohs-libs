"""Runs spec/vectors/overlay/ and presence/ (CLIENT-UI-PLAN.md §3, §7.2)
against their schemas. They live here because they are JSON documents
checked with the same schema machinery as ui/2, and overlay items use
ui.schema.json's (ui/1) action shape, which is why that schema stays in
spec/ although shack_ui no longer builds ui/1."""
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "spec" / "schemas"
VEC = ROOT / "spec" / "vectors"


def load(p):
    return json.loads(p.read_text())


REGISTRY = Registry().with_resources(
    [(s["$id"], Resource.from_contents(s)) for s in map(load, SCHEMAS.glob("*.schema.json")) if "$id" in s])
OVERLAY = load(VEC / "overlay" / "messages.json")["cases"]
PRESENCE = load(VEC / "presence" / "messages.json")["cases"]


def validator(name, ref=None):
    schema = load(SCHEMAS / name)
    if ref:
        schema = {"$ref": f"{schema['$id']}#/$defs/{ref}"}
    return jsonschema.Draft202012Validator(schema, registry=REGISTRY)


@pytest.mark.parametrize("case", OVERLAY, ids=[c["id"] for c in OVERLAY])
def test_overlay_message(case):
    assert validator("overlay.schema.json").is_valid(case["message"]) == case["valid"]


@pytest.mark.parametrize("case", PRESENCE, ids=[c["id"] for c in PRESENCE])
def test_presence_message(case):
    assert validator("presence.schema.json", case["def"]).is_valid(case["message"]) == case["valid"]
