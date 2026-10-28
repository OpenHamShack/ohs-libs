"""Building ui/1 documents in Python (CLIENT-UI-PLAN.md §5.2).

Thin dataclasses that turn into the JSON the schema describes. Each one
checks what it can when it is made, so a transmit-level action fails at
the line that wrote it, in the app's own code, not when a test later
validates the whole screen.

    doc = Document("radio/home", title="Radio", data={"state": "ws:api/events?kinds=state"},
                   root=node("column",
                             node("value", value=Bind("state.vfo", format="freq")),
                             native("waterfall", stream="main", fallback=node("text", text="...")),
                             node("button", label="Scan", action=Action.post("api/dab/scan", "control"))))
    serve(doc.to_json())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .validate import NATIVE, TRANSMIT_NATIVE, COMPONENTS, TransmitActionError, UIError, check_action, validate

UI_VERSION = "1.0"


def _json(v: Any) -> Any:
    if hasattr(v, "to_json"):
        return v.to_json()
    if isinstance(v, (list, tuple)):
        return [_json(x) for x in v]
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    return v


@dataclass(frozen=True)
class Bind:
    path: str
    format: str | None = None

    def to_json(self) -> dict:
        return {"bind": self.path} | ({"format": self.format} if self.format else {})


@dataclass(frozen=True)
class Action:
    """One §5.5 action. Use the constructors; they name what each needs."""
    do: str
    fields: dict = field(default_factory=dict)

    def __post_init__(self):
        check_action(self.to_json())

    @classmethod
    def post(cls, path: str, level: str, body: Any = None) -> Action:
        # level is required, not defaulted: a default would be one more
        # place a level is decided without anyone reading it.
        if level == "transmit":
            raise TransmitActionError("transmit_action", f"post {path} at level transmit: no generic action may "
                                      "transmit; place the native ptt or enableTx component instead")
        return cls("post", {"path": path, "level": level} | ({} if body is None else {"body": body}))

    @classmethod
    def open(cls, screen: str, **params) -> Action:
        return cls("open", {"screen": screen} | ({"params": params} if params else {}))

    @classmethod
    def back(cls) -> Action:
        return cls("back")

    @classmethod
    def local(cls, key: str, value: Any) -> Action:
        return cls("local", {"key": key, "value": value})

    @classmethod
    def native(cls, verb: str) -> Action:
        return cls("native", {"verb": verb})

    @classmethod
    def confirm(cls, text: str, action: Action) -> Action:
        return cls("confirm", {"text": text, "action": action})

    def to_json(self) -> dict:
        return {"do": self.do} | _json(self.fields)


@dataclass
class Node:
    c: str
    children: list[Node] = field(default_factory=list)
    action: Action | None = None
    fallback: Node | None = None
    props: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.c not in COMPONENTS:
            raise UIError("unknown_component", f"{self.c!r} is not in the ui/1 vocabulary")
        if self.c == "native":
            name = self.props.get("name")
            if name not in NATIVE:
                raise UIError("unknown_native", f"{name!r} is not a ui/1 native component")
            if name in TRANSMIT_NATIVE and self.action is not None:
                raise TransmitActionError("transmit_component_action",
                                          f"{name} takes no generic action: its transmit behaviour is client code")

    def to_json(self) -> dict:
        out: dict = {"c": self.c} | _json(self.props)
        if self.children:
            out["children"] = _json(self.children)
        if self.action is not None:
            out["action"] = self.action.to_json()
        if self.fallback is not None:
            out["fallback"] = self.fallback.to_json()
        return out


def node(c: str, *children: Node, action: Action | None = None, fallback: Node | None = None, **props) -> Node:
    """Any component; props are its properties (label=, value=Bind(...),
    tv=False). Unknown properties pass, as clients ignore them (§5.3)."""
    return Node(c, list(children), action, fallback, props)


def native(name: str, v: int = 1, fallback: Node | None = None, action: Action | None = None, **props) -> Node:
    """A native component the client draws itself; props go in its props
    object. Give a fallback for clients that lack it."""
    return Node("native", [], action, fallback, {"name": name, "v": v} | ({"props": props} if props else {}))


@dataclass
class Document:
    screen: str
    root: Node
    title: str | None = None
    rev: str | None = None
    data: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        """The document, validated: a screen that would be refused is never
        handed to the web layer."""
        out: dict = {"ui": UI_VERSION, "screen": self.screen}
        for k in ("rev", "title"):
            if getattr(self, k) is not None:
                out[k] = getattr(self, k)
        if self.data:
            out["data"] = dict(self.data)
        out["root"] = self.root.to_json()
        return validate(out)
