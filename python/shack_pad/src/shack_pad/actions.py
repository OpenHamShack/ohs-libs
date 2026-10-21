"""Action declarations: what an app says its pad can do (pad-actions.json).

The box had one hard-coded action list (pad.py ACTIONS) whose entries also
ran the code. Here the mechanism stays in the library and the actions move
to each app, as data (CONTROLLER-PAD.md §3): an action is an id the app
understands, its kind, a label catalog key, a group, and for transmit the
rules the library enforces on the app's behalf. What an action *does* is
the app's business; the library only ever tells the app "tune, -2".

Two ways to read a declaration, because two different people meet a bad one:
- validate() raises on the first problem. Apps call it in their own tests
  and at install, where a mistake should stop the build.
- load() keeps every action it can use and notes the rest, so a pad on a
  running controller never stops because one app shipped a bad entry.

The library adds its own actions to every app's set: `none`, `none-turn`,
`page`, `<turn>:up`/`<turn>:down` for each turn (so a keypad can do a
knob's job, the box's _press_pairs), and on the platform `app:next` and
`app:<kind>`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHEMA = 1
ID = re.compile(r"^[a-z0-9][a-z0-9:-]{0,47}$")
# A catalog key (I18N.md): dotted lower-case words, never English text.
LABEL = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_-]+)+$")
KINDS = ("press", "turn")
GROUPS = ("transmit", "receive", "audio", "screens", "other")
TX_IDS = ("ptt", "transmit")
# Shared vocabulary (CONTROLLER-PAD.md §3): use these ids where the meaning
# fits, so a remap learned in one app carries to the next.
VOCABULARY = {"tune": "turn", "step": "press", "band": "turn", "band:next": "press", "mode:next": "press",
              "channel": "turn", "mute": "press", "volume": "turn", "record": "press", "scope": "press",
              "receivers": "press", "ptt": "press", "transmit": "press", "power": "turn"}
# Icons drawn for vocabulary actions when the app names none.
DEFAULT_ICONS = {"ptt": "ptt", "transmit": "transmit", "mute": "mute", "volume": "volume", "record": "record",
                 "tune": "tune", "step": "step", "band": "band", "band:next": "band", "mode:next": "mode",
                 "scope": "scope", "power": "power", "channel": "channel", "receivers": "receivers",
                 "page": "page", "app:next": "apps"}
LIBRARY_IDS = ("none", "none-turn", "page", "app:next")


class DeclarationError(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    id: str
    kind: str                       # press | turn
    label: str                      # catalog key
    group: str = "other"
    short: tuple[str, str] = ("", "")
    icon: str | None = None
    tx: bool = False
    hold: bool = False              # momentary: keyed while held (ptt)
    arm: bool = False               # two presses within ARM_SECONDS to key; one press unkeys (transmit)
    single: bool = False
    needs: tuple[str, ...] = ()
    # For a derived <turn>:up/down press: the turn it stands for and its step.
    turn_of: str | None = None
    step: int = 0
    library: bool = field(default=False, compare=False)

    def public(self) -> dict:
        """As a settings screen lists it."""
        return {"id": self.id, "kind": self.kind, "label": self.label, "group": self.group,
                "single": self.single, "tx": self.tx}


def _problems(a: dict, seen: set[str]) -> list[str]:
    """Everything wrong with one declared action, in reading order."""
    out = []
    aid = a.get("id") if isinstance(a, dict) else None
    if not isinstance(aid, str) or not ID.match(aid):
        return [f"{aid!r}: an id is lower-case a-z0-9, '-' and ':'"]
    if aid in seen:
        out.append(f"{aid}: declared twice")
    if aid in LIBRARY_IDS or aid.startswith("app:") or aid.endswith((":up", ":down")):
        out.append(f"{aid}: the library provides this id itself")
    if a.get("kind") not in KINDS:
        out.append(f"{aid}: kind must be press or turn")
    if aid in VOCABULARY and a.get("kind") in KINDS and VOCABULARY[aid] != a.get("kind"):
        out.append(f"{aid}: the shared vocabulary makes this a {VOCABULARY[aid]}")
    if not isinstance(a.get("label"), str) or not LABEL.match(a["label"]):
        out.append(f"{aid}: label must be a catalog key such as pad.{aid.split(':')[0]}, not text")
    if a.get("group", "other") not in GROUPS:
        out.append(f"{aid}: group must be one of {', '.join(GROUPS)}")
    short = a.get("short", ["", ""])
    if not (isinstance(short, list) and len(short) == 2 and all(isinstance(s, str) for s in short)):
        out.append(f"{aid}: short is two strings")
    tx, hold, arm = bool(a.get("tx")), bool(a.get("hold")), bool(a.get("arm"))
    if tx and aid not in TX_IDS:
        out.append(f"{aid}: only ptt and transmit may key")
    if aid in TX_IDS and not tx:
        out.append(f"{aid}: must say tx: true")
    if (hold or arm) and not tx:
        out.append(f"{aid}: hold and arm only go with tx")
    if aid == "ptt" and tx and not (hold and not arm):
        out.append("ptt: is momentary (hold: true, no arm)")
    if aid == "transmit" and tx and not (arm and not hold):
        out.append("transmit: arms (arm: true, no hold)")
    if tx and a.get("kind") != "press":
        out.append(f"{aid}: a transmit action is a press")
    needs = a.get("needs", [])
    if not (isinstance(needs, list) and all(isinstance(n, str) for n in needs)):
        out.append(f"{aid}: needs is a list of capability names")
    return out


def _action(a: dict) -> Action:
    tx = bool(a.get("tx"))
    short = a.get("short") or ["", ""]
    return Action(a["id"], a["kind"], a["label"], a.get("group", "other"), (short[0], short[1]),
                  a.get("icon") or DEFAULT_ICONS.get(a["id"]), tx, bool(a.get("hold")), bool(a.get("arm")),
                  bool(a.get("single")) or tx, tuple(a.get("needs") or ()))


def _header(decl: dict) -> list[str]:
    if not isinstance(decl, dict):
        return ["a declaration is a JSON object"]
    out = []
    if decl.get("schema") != SCHEMA:
        out.append(f"schema must be {SCHEMA}")
    if not isinstance(decl.get("app"), str) or not decl["app"]:
        out.append("app names the app kind")
    if not isinstance(decl.get("actions"), list):
        out.append("actions is a list")
    return out


def validate(decl: dict) -> None:
    """Raise DeclarationError on anything wrong with a pad-actions.json."""
    problems = _header(decl)
    if not problems:
        seen: set[str] = set()
        for a in decl["actions"]:
            problems += _problems(a, seen)
            if isinstance(a, dict) and isinstance(a.get("id"), str):
                seen.add(a["id"])
    if problems:
        raise DeclarationError("; ".join(problems))


def library_actions(kinds: list[str] | None = None) -> dict[str, Action]:
    """The actions every app gets. kinds: the apps the platform can switch
    to (none on a desktop, where there is nothing to switch to)."""
    out = {"none": Action("none", "press", "pad.none", "other", library=True),
           "none-turn": Action("none-turn", "turn", "pad.none", "other", library=True),
           "page": Action("page", "press", "pad.page", "other", icon="page", single=True, library=True)}
    if kinds:
        out["app:next"] = Action("app:next", "press", "pad.app_next", "screens", icon="apps", library=True)
        for k in kinds:
            out[f"app:{k}"] = Action(f"app:{k}", "press", f"pad.app.{k}", "screens", icon=f"app:{k}", library=True)
    return out


def load(decl: dict, capabilities: set[str] | None = None, kinds: list[str] | None = None) -> tuple[dict[str, Action], list[str]]:
    """The actions an app offers here, and notes on what was left out.

    capabilities: what the app can do on this machine; an action whose
    `needs` are not all present is not offered (the box's `needs`). None
    means "do not filter", for an app that does not use needs."""
    acts = library_actions(kinds)
    notes = _header(decl) if not isinstance(decl, dict) or not isinstance(decl.get("actions"), list) else []
    if notes:
        return acts, notes
    if decl.get("schema") != SCHEMA:
        notes.append(f"schema {decl.get('schema')!r}: read as {SCHEMA}")
    seen: set[str] = set()
    turns = []
    for a in decl["actions"]:
        problems = _problems(a, seen)
        if problems:
            notes += [f"{p}; left out" for p in problems]
            continue
        seen.add(a["id"])
        act = _action(a)
        if capabilities is not None and not set(act.needs) <= capabilities:
            notes.append(f"{act.id}: needs {', '.join(sorted(set(act.needs) - capabilities))}; not offered here")
            continue
        acts[act.id] = act
        if act.kind == "turn":
            turns.append(act)
    for t in turns:
        for word, step, sign in (("up", +1, "+"), ("down", -1, "−")):
            aid = f"{t.id}:{word}"
            acts[aid] = Action(aid, "press", f"{t.label}_{word}", t.group,
                               (t.short[0] or t.id.upper()[:6], f"{sign} {t.short[1]}".strip()),
                               t.icon, needs=t.needs, turn_of=t.id, step=step, library=True)
    return acts, notes


def tx_actions(acts: dict[str, Action]) -> list[str]:
    return [aid for aid, a in acts.items() if a.tx]
