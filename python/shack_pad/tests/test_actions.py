"""Action declarations: what validate() refuses, what load() keeps, and the
actions the library adds."""

import copy
import json
from pathlib import Path

import pytest

from shack_pad import actions
from shack_pad.actions import DeclarationError

# The declaration and layout schemas are the contract every app writes to,
# whatever its language, so they live with the other schemas in spec/.
SCHEMAS = Path(__file__).resolve().parents[3] / "spec" / "schemas"


def test_the_examples_are_valid(nb, wb):
    actions.validate(nb[0])
    actions.validate(wb[0])


def test_the_examples_match_the_json_schemas(nb, wb):
    jsonschema = pytest.importorskip("jsonschema")
    acts = json.loads((SCHEMAS / "pad-actions.schema.json").read_text())
    layout = json.loads((SCHEMAS / "pad-layout.schema.json").read_text())
    for decl, lay in (nb, wb):
        jsonschema.validate(decl, acts)
        jsonschema.validate(lay, layout)


@pytest.mark.parametrize("change,why", [
    ({"id": "tune", "tx": True}, "only ptt and transmit may key"),
    ({"id": "ptt", "tx": True, "hold": False, "arm": True}, "momentary"),
    ({"id": "transmit", "kind": "press", "tx": True, "hold": True}, "arms"),
    ({"id": "ptt", "tx": False}, "must say tx"),
    ({"id": "mute", "hold": True}, "hold and arm only go with tx"),
    ({"id": "mute", "label": "Mute the audio"}, "catalog key"),
    ({"id": "Mute"}, "lower-case"),
    ({"id": "tune", "kind": "press"}, "shared vocabulary"),
    ({"id": "page"}, "library provides"),
    ({"id": "tune:up", "kind": "press"}, "library provides"),
    ({"id": "mute", "group": "fun"}, "group must be"),
])
def test_validate_refuses_with_the_reason(change, why):
    base = {"id": "mute", "kind": "press", "label": "pad.mute", "group": "audio"}
    if change["id"] in ("ptt", "transmit"):
        base = {"id": change["id"], "kind": "press", "label": "pad.x", "tx": True,
                "hold": change["id"] == "ptt", "arm": change["id"] == "transmit"}
    decl = {"schema": 1, "app": "x", "actions": [{**base, **change}]}
    with pytest.raises(DeclarationError, match=why):
        actions.validate(decl)


def test_load_keeps_what_it_can_and_notes_the_rest(nb):
    decl = copy.deepcopy(nb[0])
    decl["actions"].append({"id": "tune", "kind": "turn", "label": "pad.tune"})          # twice
    decl["actions"].append({"id": "nuke", "kind": "press", "label": "pad.nuke", "tx": True})
    acts, notes = actions.load(decl)
    assert "nuke" not in acts and "mute" in acts and acts["ptt"].tx and acts["ptt"].hold
    assert any("declared twice" in n for n in notes) and any("only ptt and transmit" in n for n in notes)


def test_every_turn_gives_a_pair_of_presses_and_the_library_adds_its_own(nb):
    acts, notes = actions.load(nb[0])
    assert notes == []
    up = acts["tune:up"]
    assert (up.kind, up.turn_of, up.step) == ("press", "tune", +1) and acts["tune:down"].step == -1
    assert {"none", "none-turn", "page"} <= set(acts) and "app:next" not in acts
    on_platform, _n = actions.load(nb[0], kinds=["qo100nb", "radio"])
    assert {"app:next", "app:qo100nb", "app:radio"} <= set(on_platform)


def test_an_action_whose_needs_are_missing_is_not_offered(wb):
    acts, notes = actions.load(wb[0], capabilities=set())
    assert "rx-cycle" not in acts and any("rx-cycle: needs rx_cycle" in n for n in notes)
    assert "rx-cycle" in actions.load(wb[0], capabilities={"rx_cycle"})[0]


def test_transmit_actions_are_always_single(wb):
    decl = copy.deepcopy(wb[0])
    decl["actions"][0].pop("single")
    assert actions.load(decl)[0]["transmit"].single
