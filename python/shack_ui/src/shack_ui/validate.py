"""Validating a ui/1 document (CLIENT-UI-PLAN.md §5).

Two layers. The named checks come first and raise an error whose code a
server author can act on; then the whole document goes through
ui.schema.json, the contract every client also validates against. The
transmit rule (§5.6) is in both on purpose: the schema so a client needs
no code of its own to refuse a document, the named check so the message
says "transmit" rather than a schema path.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

import jsonschema

COMPONENTS = frozenset({
    "column", "row", "grid", "list", "tabs", "section", "card", "sheet", "spacer",
    "text", "value", "badge", "chip", "image", "meter", "nowPlaying", "empty",
    "button", "toggle", "slider", "stepper", "choice", "textField", "frequencyField", "settingsForm",
    "native",
})
NATIVE = frozenset({"waterfall", "map", "audio", "video", "radioFace", "sstvPicture", "decodeList", "ptt",
                    "enableTx", "camera"})
# The only components that may key a radio: their lease, heartbeat and
# time-outs are client code with tests, so the server only places them.
TRANSMIT_NATIVE = frozenset({"ptt", "enableTx"})
ACTIONS = frozenset({"post", "open", "back", "local", "native", "confirm"})


class UIError(ValueError):
    """A document that must not be served. `code` is the name the vectors
    use: transmit_action, transmit_component_action, unknown_component,
    unknown_native, unknown_action, schema."""

    def __init__(self, code: str, detail: str, where: str = ""):
        super().__init__(f"{code}{' at ' + where if where else ''}: {detail}")
        self.code = code
        self.where = where


class TransmitActionError(UIError):
    """Something tried to make a generic action transmit (§5.6)."""


@cache
def schema() -> dict:
    return json.loads(resources.files("shack_ui").joinpath("ui.schema.json").read_text())


def check_action(action: dict, where: str = "action") -> None:
    if not isinstance(action, dict):
        raise UIError("schema", "an action is an object", where)
    if action.get("do") not in ACTIONS:
        raise UIError("unknown_action", f"do {action.get('do')!r} is not one of {sorted(ACTIONS)}", where)
    if action.get("level") == "transmit":
        raise TransmitActionError("transmit_action", "no generic action may transmit; place the native ptt or "
                                  "enableTx component instead", where)
    if action.get("do") == "confirm" and isinstance(action.get("action"), dict):
        check_action(action["action"], where + ".action")


def check_node(node: dict, where: str = "root") -> None:
    if not isinstance(node, dict):
        raise UIError("schema", "a node is an object", where)
    c = node.get("c")
    if c not in COMPONENTS:
        raise UIError("unknown_component", f"{c!r} is not in the ui/1 vocabulary", where)
    if c == "native":
        if node.get("name") not in NATIVE:
            raise UIError("unknown_native", f"{node.get('name')!r} is not a ui/1 native component", where)
        if node["name"] in TRANSMIT_NATIVE and "action" in node:
            raise TransmitActionError("transmit_component_action", f"{node['name']} takes no generic action: "
                                      "its transmit behaviour is client code", where)
    if "action" in node:
        check_action(node["action"], where + ".action")
    if isinstance(node.get("fallback"), dict):
        check_node(node["fallback"], where + ".fallback")
    for i, child in enumerate(node.get("children") or []):
        check_node(child, f"{where}.children[{i}]")


def validate(doc: dict) -> dict:
    """Raise UIError unless doc is a servable ui/1 document; return it."""
    if isinstance(doc, dict) and isinstance(doc.get("root"), dict):
        check_node(doc["root"])
    errors = sorted(jsonschema.Draft202012Validator(schema()).iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise UIError("schema", e.message, "/".join(str(p) for p in e.path))
    return doc
