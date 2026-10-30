"""Runs spec/vectors/ui2/ the way every port must (spec/vectors/ui2/README.md):
the validator's problems in order, the schema's verdict, typed values,
paths, op application and the Aircraft minute. The same files are the
Kotlin and Swift renderers' tests."""
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

from shack_ui import apply_frame, apply_ops, apply_poll, check, check_path, check_typed, public_state, state_from_document

ROOT = Path(__file__).resolve().parents[3]
VEC = ROOT / "spec" / "vectors" / "ui2"
FIX = VEC / "fixtures"
SCHEMA = json.loads((ROOT / "spec" / "schemas" / "ui2.schema.json").read_text())
REGISTRY = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))


def load(p):
    return json.loads(Path(p).read_text())


def validator(ref=None):
    s = SCHEMA if ref is None else {"$ref": f"{SCHEMA['$id']}#/$defs/{ref}"}
    return jsonschema.Draft202012Validator(s, registry=REGISTRY)


DOCS = load(VEC / "documents.json")["cases"]
TYPED = load(VEC / "typed.json")["cases"]
PATHS = load(VEC / "paths.json")["cases"]
OPS = load(VEC / "ops.json")
STREAM = load(VEC / "aircraft-stream.json")


def document_of(case, base=None):
    if "fixture" in case:
        return load(FIX / case["fixture"])
    d = case["document"]
    return base if d == "base" else d


def test_the_schema_is_a_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("case", DOCS, ids=[c["id"] for c in DOCS])
def test_document(case):
    doc = document_of(case)
    got = [{"code": p.code, "where": p.where} for p in check(doc, base_kind=case["baseKind"])]
    assert got == case["problems"]
    assert validator().is_valid(doc) == case["schema"]


@pytest.mark.parametrize("case", TYPED, ids=[c["id"] for c in TYPED])
def test_typed_value(case):
    assert (check_typed(case["value"]) == []) == case["ok"]


@pytest.mark.parametrize("case", PATHS, ids=[c["id"] for c in PATHS])
def test_path(case):
    out = []
    check_path(case["value"], "/x", out, kind=case["kind"])
    assert (out[0].code if out else None) == case["problem"]


@pytest.mark.parametrize("case", OPS["cases"], ids=[c["id"] for c in OPS["cases"]])
def test_ops(case):
    start = state_from_document(document_of(case, OPS["base"]))
    s = start
    for step in case["steps"]:
        if "frame" in step:
            s = apply_frame(s, step["frame"], start)
        elif "poll" in step:
            s = apply_poll(s, step["poll"], start)
        else:
            s = apply_ops(s, step["ops"])
    assert public_state(s) == case["state"]


def test_every_op_in_the_vectors_and_the_fixture_stream_has_the_schema_s_shape():
    v = validator("op")
    for case in OPS["cases"]:
        for step in case["steps"]:
            if "poll" in step:
                assert validator("pollAnswer").is_valid(step["poll"]), case["id"]
            for op in step.get("ops") or step.get("frame", step.get("poll", {})).get("ops") or []:
                if isinstance(op, dict) and op.get("op") in ("set", "up", "rm", "order", "push", "append", "replace",
                                                             "toast", "reload"):
                    assert v.is_valid(op), (case["id"], op)
    f = validator("frame")
    for line in (FIX / STREAM["stream"]).read_text().splitlines():
        assert f.is_valid(json.loads(line)["frame"])


def test_the_aircraft_minute():
    start = state_from_document(load(FIX / STREAM["document"]))
    lines = [json.loads(x) for x in (FIX / STREAM["stream"]).read_text().splitlines() if x.strip()]
    want = {c["line"]: c for c in STREAM["checkpoints"]}
    s = start
    for i, line in enumerate(lines):
        s = apply_frame(s, line["frame"], start)
        if i in want:
            assert line["atMs"] == want[i]["atMs"]
            assert public_state(s) == want[i]["state"], f"line {i}"
    assert len(want) == 3


def test_the_other_worked_example_messages_have_their_schema_shapes():
    assert validator("pollAnswer").is_valid(load(FIX / "polls" / "radio" / "wxsat.json"))
    assert validator("pollAnswer").is_valid(load(FIX / "launcher-status.json"))
    assert validator("descriptor").is_valid(load(FIX / "apps" / "shack-radio" / "app.json"))
    for f in (ROOT / "spec" / "ui2" / "symbols").glob("*.json"):
        assert validator("symbolSet").is_valid(load(f)), f.name
