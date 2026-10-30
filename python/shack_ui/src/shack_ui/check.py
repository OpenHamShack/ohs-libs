"""The ui/2 validator (SCREENS.md §2-§7, §11): every rule a renderer also
enforces, each a small function named by the code it reports.

    check(doc, base_kind="app")   -> [Problem]   the static rules, by name
    check_action(action, ...)     -> [Problem]   one action on its own (an
                                                 item's `action`, a remedy)
    check_typed(value)            -> [str]       one typed value (§2.1)
    check_path(value, where, out, kind)          one path, URL or link (§11)

Moved here from the contract's fixture tool (openhamshack
tools/screens-fixture/uidoc.py, 30/10/2026), where it was written against
the worked examples; the fixture tool now imports this one. The walk order
is the document order, so two runs report the same list in the same order,
and the vectors (spec/vectors/ui2/documents.json) pin that order for the
Kotlin and Swift ports.

Why the rules exist at all: a document is untrusted data on a plain-HTTP
LAN (SCREENS.md §11), and the renderers refuse what these rules refuse.
The server runs the same rules before it sends anything (Screen.json()),
so a bad document is caught in the app's tests rather than on a TV.

The problem codes, which the vectors and every port share:
bad_version, unknown_part, duplicate_id, bad_reference, bad_path,
absolute_url, cross_app_open, bad_action, bad_placeholder, key_route,
transmit_action, transmit_part_action, template_binding, bad_typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

# §3, the whole part set. A type outside it is a document for a newer
# client (it should carry `fallback`) or a typo; either way it is reported.
PART_TYPES = frozenset("""
    column row split stack frame scroll tabs grid list table panel section card spacer divider
    text field badge icon image qr progress meter chart skyplot map spectrum video banner empty hints tile
    button toggle choice slider number frequency textInput pin
    ptt txArm audio camera
""".split())

ACTION_KINDS = frozenset("call open back show hide focus part select local send link seq".split())
CALL_METHODS = frozenset({"POST", "PUT", "DELETE"})
LEVELS = frozenset({"view", "control", "transmit"})
PLACEHOLDERS = frozenset({"$value", "$hz", "$client"})
TRANSMIT_PARTS = frozenset({"ptt", "txArm"})
# The routes a transmit part keys through (§7.1); no other call may reach them.
KEY_ROUTE_FIELDS = {"ptt": ("key", "heartbeat", "keep"), "txArm": ("arm", "heartbeat")}

# The client's built-in third-party image hosts (§9, decision D10). A
# document cannot extend this: that is the point of it.
IMAGE_HOSTS = ("plnspttrs.net",)

# Properties that hold child parts. Anything else is a property, even when
# its value is a dict with a "type" key (a chart axis has `"type": "time"`).
PART_KEYS = ("child", "template", "empty", "fallback", "overlay")
# Properties of a part that hold one action.
ACTION_PROPS = ("action", "onActivate", "onHold", "onClose", "back", "tap", "use")

TYPED_KEYS = ("freq", "ago", "countdown", "since", "clock", "time", "cycle")
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
BRACED = re.compile(r"\{(\$[^}]*)\}")


@dataclass(frozen=True)
class Problem:
    code: str
    where: str
    msg: str

    def __str__(self):
        return f"{self.code} at {self.where}: {self.msg}"


@dataclass(frozen=True)
class Ctx:
    """Where a node sits. `item` is true where `{"$": field}` may be read
    (a template, a `for` container, a list's onActivate); `repeats` is true
    inside a `template`, whose ids repeat per item and so are not unique."""
    item: bool = False
    repeats: bool = False


def is_binding(v) -> bool:
    return isinstance(v, dict) and "$" in v


def is_part(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("type"), str)


# ---------------------------------------------------------------- the walk

def iter_parts(part, where="/root", ctx=Ctx()):
    """Every part under (and including) `part`, in document order, as
    (part, where, ctx). Item data (collections, a layer's `items`) is never
    entered: it is data, and a dict in it with a "type" is not a part."""
    if not is_part(part):
        return
    yield part, where, ctx
    inner = Ctx(item=ctx.item or "for" in part, repeats=ctx.repeats)
    for i, c in enumerate(part.get("children") or []):
        yield from iter_parts(c, f"{where}/children/{i}", inner)
    if part.get("type") == "tabs":
        for i, tab in enumerate(part.get("tabs") or []):
            if isinstance(tab, dict):
                yield from iter_parts(tab.get("child"), f"{where}/tabs/{i}/child", inner)
    for k in PART_KEYS:
        if k == "template":
            sub = Ctx(item=True, repeats=True)
        elif k == "child":
            sub = inner
        else:
            sub = ctx                     # empty/fallback/overlay are not per item
        yield from iter_parts(part.get(k), f"{where}/{k}", sub)


class _Index:
    """What the rules need to know about the whole document first: part
    ids and their types, collections, selections, and the key routes."""

    def __init__(self, doc: dict):
        self.collections = set((doc.get("collections") or {}).keys())
        self.selections = set((doc.get("selections") or {}).keys())
        self.parts: dict[str, dict] = {}
        self.dupes: list[tuple[str, str]] = []
        self.key_routes: dict[str, str] = {}
        for part, where, ctx in iter_parts(doc.get("root")):
            pid = part.get("id")
            if isinstance(pid, str) and not ctx.repeats:
                if pid in self.parts:
                    self.dupes.append((pid, where))
                else:
                    self.parts[pid] = part
            for f in KEY_ROUTE_FIELDS.get(part.get("type"), ()):
                if isinstance(part.get(f), str):
                    self.key_routes[_path_only(part[f])] = part.get("id") or part["type"]

    def lease_ok(self, lease) -> bool:
        return isinstance(lease, str) and self.parts.get(lease, {}).get("type") in TRANSMIT_PARTS


def _path_only(p: str) -> str:
    return p.split("?", 1)[0].strip("/")


# ---------------------------------------------------------------- rules

def rule_bad_version(doc, out):
    v = doc.get("ui")
    if not (isinstance(v, str) and re.fullmatch(r"2\.\d+", v)):
        out.append(Problem("bad_version", "/ui", f"ui must be \"2.<minor>\", not {v!r}"))


def rule_unknown_part(part, where, out):
    if part["type"] not in PART_TYPES:
        out.append(Problem("unknown_part", where, f"no part type {part['type']!r} in ui/2"))


def rule_duplicate_id(index, out):
    for pid, where in index.dupes:
        out.append(Problem("duplicate_id", where, f"part id {pid!r} is used twice"))


def check_path(value, where, out, kind="path"):
    """`kind`: "path" (relative only), "image" (relative, or https on the
    allow-list), "link" (an https URL a person may open). Bound values and
    nulls are the data's business and are not checked statically."""
    if not isinstance(value, str):
        return
    absolute = bool(SCHEME.match(value)) or value.startswith("//")
    if kind == "link":
        if urlsplit(value).scheme != "https" or not urlsplit(value).hostname:
            out.append(Problem("absolute_url", where, f"a link must be an https URL: {value!r}"))
        return
    if absolute:
        u = urlsplit(value)
        host = (u.hostname or "").lower()
        allowed = kind == "image" and u.scheme == "https" and any(
            host == h or host.endswith("." + h) for h in IMAGE_HOSTS)
        if not allowed:
            out.append(Problem("absolute_url", where, f"absolute URL not allowed here: {value!r}"))
        return
    # unquote first, so %2e%2e and %5c are caught however they are spelled.
    plain = unquote(value.split("?", 1)[0])
    if value.startswith("/"):
        out.append(Problem("bad_path", where, f"path must be relative: {value!r}"))
    elif "\\" in plain or "\\" in value:
        out.append(Problem("bad_path", where, f"backslash in path: {value!r}"))
    elif ".." in plain.split("/"):
        out.append(Problem("bad_path", where, f"'..' segment in path: {value!r}"))


def _placeholders(value, where, out):
    """Strings in a call's body or an open's params: a whole-string
    placeholder must be one of §5.2's three."""
    if isinstance(value, str):
        if value.startswith("$") and value not in PLACEHOLDERS:
            out.append(Problem("bad_placeholder", where, f"unknown placeholder {value!r}"))
    elif isinstance(value, dict) and not is_binding(value):
        for k, v in value.items():
            _placeholders(v, f"{where}/{k}", out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _placeholders(v, f"{where}/{i}", out)


def _path_placeholders(path, where, out):
    if not isinstance(path, str):
        return
    for m in BRACED.finditer(path):
        if m.group(1) not in PLACEHOLDERS:
            out.append(Problem("bad_placeholder", where, f"unknown placeholder {{{m.group(1)}}} in path"))
    if path.startswith("$") and path not in PLACEHOLDERS:
        out.append(Problem("bad_placeholder", where, f"unknown placeholder {path!r}"))


def check_action(a, where="/action", out=None, *, index=None, doc=None, base_kind="app",
                 ctx=Ctx(), data=False) -> list:
    """One action (§5). `index` (or `doc`) resolves part, selection and lease
    names; without either, references are not checked. `data`: the action
    is an item's own (§2.2), which may never be transmit (§7.2)."""
    out = [] if out is None else out
    if index is None and doc is not None:
        index = _Index(doc)
    if is_binding(a):
        if not ctx.item and not data:
            out.append(Problem("template_binding", where, "{\"$\": ...} outside a template"))
        return out
    if not isinstance(a, dict):
        out.append(Problem("bad_action", where, "an action must be an object"))
        return out
    kind = a.get("do")
    if kind not in ACTION_KINDS:
        out.append(Problem("bad_action", where, f"unknown action {kind!r}"))
        return out

    if kind == "call":
        if a.get("method") not in CALL_METHODS:
            out.append(Problem("bad_action", where, f"call method {a.get('method')!r}"))
        if not isinstance(a.get("path"), str) or not a.get("path"):
            out.append(Problem("bad_action", where, "call needs a path"))
        if a.get("level") not in LEVELS:
            out.append(Problem("bad_action", where, f"call level {a.get('level')!r}"))
        check_path(a.get("path"), f"{where}/path", out)
        _path_placeholders(a.get("path"), f"{where}/path", out)
        _placeholders(a.get("body"), f"{where}/body", out)
        if index is not None and isinstance(a.get("path"), str):
            owner = index.key_routes.get(_path_only(a["path"]))
            if owner is not None:
                out.append(Problem("key_route", where, f"call reaches {owner!r}'s key route {a['path']!r}"))
    elif kind == "open":
        if not isinstance(a.get("screen"), str):
            out.append(Problem("bad_action", where, "open needs a screen"))
        if "app" in a and base_kind != "platform":
            out.append(Problem("cross_app_open", where, "only the launcher and platform screens open another app"))
        _placeholders(a.get("params"), f"{where}/params", out)
    elif kind in ("show", "hide", "focus", "part"):
        if not isinstance(a.get("part"), str):
            out.append(Problem("bad_action", where, f"{kind} needs a part"))
        elif index is not None and a["part"] not in index.parts:
            out.append(Problem("bad_reference", where, f"no part {a['part']!r}"))
        if kind == "part" and not isinstance(a.get("verb"), str):
            out.append(Problem("bad_action", where, "part needs a verb"))
    elif kind == "select":
        if index is not None and a.get("sel") not in index.selections:
            out.append(Problem("bad_reference", where, f"no selection {a.get('sel')!r}"))
    elif kind == "local":
        if not isinstance(a.get("key"), str) or not any(k in a for k in ("set", "toggle", "cycle")):
            out.append(Problem("bad_action", where, "local needs a key and set, toggle or cycle"))
    elif kind == "send":
        if not isinstance(a.get("name"), str):
            out.append(Problem("bad_action", where, "send needs a name"))
    elif kind == "link":
        check_path(a.get("url"), f"{where}/url", out, kind="link")
    elif kind == "seq":
        acts = a.get("actions")
        if not isinstance(acts, list):
            out.append(Problem("bad_action", where, "seq needs actions"))
        else:
            for i, sub in enumerate(acts):
                check_action(sub, f"{where}/actions/{i}", out, index=index, base_kind=base_kind,
                             ctx=ctx, data=data)

    rule_transmit_action(a, where, out, index, data)
    if not data:
        _scan_bindings(a, where, out, ctx)
    return out


def rule_transmit_action(a, where, out, index, data):
    tx, lease = a.get("tx") is True, a.get("lease")
    if data and (tx or a.get("level") == "transmit"):
        out.append(Problem("transmit_action", where, "an item's own action may never transmit"))
        return
    if a.get("level") == "transmit" and not (tx and lease is not None):
        out.append(Problem("transmit_action", where, "level transmit needs tx: true and a lease"))
    elif tx and lease is None:
        out.append(Problem("transmit_action", where, "tx: true needs a lease"))
    if lease is not None and index is not None and not index.lease_ok(lease):
        out.append(Problem("bad_reference", where, f"lease {lease!r} is not a ptt or txArm part"))


def rule_transmit_part(part, where, out):
    t = part["type"]
    if t not in TRANSMIT_PARTS:
        return
    for k in ("action", "onHold", "onActivate", "tap"):
        if k in part:
            out.append(Problem("transmit_part_action", where, f"{t} may not carry {k!r}"))
    for k, v in part.items():
        if re.fullmatch(r"[a-z][A-Za-z]*(Ms|S)", k):
            if t == "ptt" and k == "holdMs":
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 1000:
                    out.append(Problem("transmit_part_action", where, f"holdMs {v!r} is below 1000"))
            else:
                out.append(Problem("transmit_part_action", where, f"{t} timing {k!r} is client code"))


def check_typed(value) -> list[str]:
    """Problems with one typed value (§2.1), as messages; [] when fine or
    when `value` is not a typed value at all."""
    if not isinstance(value, dict):
        return []
    kind = next((k for k in TYPED_KEYS if k in value), None)
    if kind is None:
        return []

    def iso_ok(v):
        return is_binding(v) or (isinstance(v, str) and ISO.match(v) is not None)

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    v = value[kind]
    errs = []
    if kind == "freq":
        if not (num(v) or is_binding(v)):
            errs.append("freq must be a number of Hz")
        if "decimals" in value and not (isinstance(value["decimals"], int) and value["decimals"] >= 0):
            errs.append("freq decimals must be a whole number")
    elif kind in ("ago", "since"):
        if not iso_ok(v):
            errs.append(f"{kind} must be an ISO time")
    elif kind == "time":
        if not iso_ok(v):
            errs.append("time must be an ISO time")
        if value.get("zone", "local") not in ("local", "utc", "local+utc"):
            errs.append(f"time zone {value.get('zone')!r}")
    elif kind == "clock":
        if v not in ("local", "utc", "local+utc"):
            errs.append(f"clock {v!r}")
        if value.get("style", "hm") not in ("hm", "hms"):
            errs.append(f"clock style {value.get('style')!r}")
    elif kind == "countdown":
        if not isinstance(v, dict) or not iso_ok(v.get("to")):
            errs.append("countdown needs an ISO `to`")
        else:
            texts = v.get("text")
            want = 3 if "then" in v else 2
            if "then" in v and not iso_ok(v["then"]):
                errs.append("countdown `then` must be an ISO time")
            if not (isinstance(texts, list) and len(texts) == want and all(isinstance(t, str) for t in texts)):
                errs.append(f"countdown {'with' if want == 3 else 'without'} `then` needs {want} texts")
    elif kind == "cycle":
        if not (isinstance(v, dict) and num(v.get("periodS")) and v["periodS"] > 0
                and num(v.get("offsetS", 0))):
            errs.append("cycle needs a positive periodS")
    return errs


def _scan_bindings(value, where, out, ctx, *, skip=()):
    """Bindings outside an item context, and typed values, anywhere inside
    a property value. Actions are left to check_action, and child parts to
    the walk."""
    if is_binding(value):
        if not ctx.item:
            out.append(Problem("template_binding", where, "{\"$\": ...} outside a template"))
        return
    if isinstance(value, dict):
        # An action is never a typed value, though `local`'s `cycle` field
        # has the name of one (SCREENS.md §5.1 and §2.1 share the word).
        for msg in ([] if "do" in value else check_typed(value)):
            out.append(Problem("bad_typed", where, msg))
        for k, v in value.items():
            if k in skip or (isinstance(v, dict) and "do" in v):
                continue
            if k == "actions" and isinstance(v, list):
                continue
            _scan_bindings(v, f"{where}/{k}", out, ctx)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            if isinstance(v, dict) and "do" in v:
                continue
            _scan_bindings(v, f"{where}/{i}", out, ctx)


def _actions_in(value, where):
    """Every action reachable in a property value without entering another
    action (a seq's actions are check_action's) or a part."""
    if isinstance(value, dict):
        if "do" in value:
            yield value, where
            return
        for k, v in value.items():
            yield from _actions_in(v, f"{where}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _actions_in(v, f"{where}/{i}")


def _part_paths(part, where, out):
    t = part["type"]
    if t == "image":
        check_path(part.get("src"), f"{where}/src", out, kind="image")
    if t in ("audio", "video"):
        check_path(part.get("src"), f"{where}/src", out)
    if t == "camera":
        check_path(part.get("srt"), f"{where}/srt", out)
    if t == "tile":
        check_path(part.get("art"), f"{where}/art", out)
    if t == "spectrum" and isinstance(part.get("stream"), dict):
        check_path(part["stream"].get("path"), f"{where}/stream/path", out)
    if t == "map" and isinstance(part.get("ground"), dict):
        for k in ("tiles", "land"):
            check_path(part["ground"].get(k), f"{where}/ground/{k}", out)
    if "link" in part:
        check_path(part.get("link"), f"{where}/link", out, kind="link")
    for k in ("poll", "page"):
        if isinstance(part.get(k), dict):
            check_path(part[k].get("path"), f"{where}/{k}/path", out)


def _part_references(part, where, out, index):
    if "from" in part and part["from"] not in index.collections:
        out.append(Problem("bad_reference", f"{where}/from", f"no collection {part['from']!r}"))
    for k in ("selection", "for"):
        if k in part and part[k] not in index.selections:
            out.append(Problem("bad_reference", f"{where}/{k}", f"no selection {part[k]!r}"))
    for i, mark in enumerate(part.get("marks") or [] if part["type"] == "chart" else []):
        if isinstance(mark, dict) and "from" in mark and mark["from"] not in index.collections:
            out.append(Problem("bad_reference", f"{where}/marks/{i}/from", f"no collection {mark['from']!r}"))


def _check_layers(part, where, out, index, ctx, base_kind):
    for i, layer in enumerate(part.get("layers") or []):
        if not isinstance(layer, dict):
            continue
        lw = f"{where}/layers/{i}"
        if "from" in layer and layer["from"] not in index.collections:
            out.append(Problem("bad_reference", f"{lw}/from", f"no collection {layer['from']!r}"))
        if "selection" in layer and layer["selection"] not in index.selections:
            out.append(Problem("bad_reference", f"{lw}/selection", f"no selection {layer['selection']!r}"))
        for j, fr in enumerate(layer.get("frames") or []):
            if isinstance(fr, dict):
                check_path(fr.get("src"), f"{lw}/frames/{j}/src", out, kind="image")
        for k, v in layer.items():
            if k in ("items", "frames"):      # inline item data
                continue
            for a, aw in _actions_in(v, f"{lw}/{k}"):
                check_action(a, aw, out, index=index, base_kind=base_kind, ctx=ctx)
            _scan_bindings(v, f"{lw}/{k}", out, ctx)


def _check_part(part, where, ctx, out, index, base_kind):
    rule_unknown_part(part, where, out)
    rule_transmit_part(part, where, out)
    _part_references(part, where, out, index)
    _part_paths(part, where, out)
    item_actions = "from" in part      # a list's OK may run the item's own action
    nested = set(PART_KEYS) | {"children", "layers"}
    for k, v in part.items():
        if k in nested or (k == "tabs" and part["type"] == "tabs"):
            continue
        sub = Ctx(item=True, repeats=ctx.repeats) if (
            (item_actions and k in ("onActivate", "onHold", "columns"))) else ctx
        if k in ACTION_PROPS:
            if v is not None:
                check_action(v, f"{where}/{k}", out, index=index, base_kind=base_kind, ctx=sub)
            continue
        for a, aw in _actions_in(v, f"{where}/{k}"):
            check_action(a, aw, out, index=index, base_kind=base_kind, ctx=sub)
        _scan_bindings(v, f"{where}/{k}", out, sub)
    if part["type"] == "tabs":
        for i, tab in enumerate(part.get("tabs") or []):
            if isinstance(tab, dict):
                rest = {k: v for k, v in tab.items() if k != "child"}
                _scan_bindings(rest, f"{where}/tabs/{i}", out, ctx)
    if part["type"] == "map":
        _check_layers(part, where, out, index, ctx, base_kind)


def check(doc: dict, *, base_kind: str = "app") -> list[Problem]:
    """Every problem in a screen document, in document order; [] is valid.
    `base_kind` is "platform" for the launcher and the platform's own
    screens (the only ones that may open another app), else "app"."""
    out: list[Problem] = []
    if not isinstance(doc, dict):
        return [Problem("bad_version", "", "a document is a JSON object")]
    index = _Index(doc)
    rule_bad_version(doc, out)
    for name, sel in (doc.get("selections") or {}).items():
        src = sel.get("from") if isinstance(sel, dict) else None
        if src not in index.collections:
            out.append(Problem("bad_reference", f"/selections/{name}/from", f"no collection {src!r}"))
    for k in ("live", "poll"):
        if isinstance(doc.get(k), dict):
            check_path(doc[k].get("path"), f"/{k}/path", out)
    if isinstance(doc.get("backdrop"), dict):
        check_path(doc["backdrop"].get("image"), "/backdrop/image", out, kind="image")
    for i, hold in enumerate(doc.get("holds") or []):
        for k in ("start", "stop"):
            if isinstance(hold, dict) and k in hold:
                check_action(hold[k], f"/holds/{i}/{k}", out, index=index, base_kind=base_kind)
    for key, a in (doc.get("keys") or {}).items():
        check_action(a, f"/keys/{key}", out, index=index, base_kind=base_kind)
    for name, coll in (doc.get("collections") or {}).items():
        for key, item in ((coll or {}).get("items") or {}).items():
            if isinstance(item, dict) and "action" in item:
                check_action(item["action"], f"/collections/{name}/items/{key}/action", out,
                             index=index, base_kind=base_kind, data=True)
    if not is_part(doc.get("root")):
        out.append(Problem("unknown_part", "/root", "root must be a part"))
    for part, where, ctx in iter_parts(doc.get("root")):
        _check_part(part, where, ctx, out, index, base_kind)
    rule_duplicate_id(index, out)
    return out
