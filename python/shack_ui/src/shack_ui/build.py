"""Building ui/2 screen documents in Python (SCREENS.md §2-§9, §14).

    s = Screen("radio/aircraft", title=_("Aircraft"), req=req)
    s.symbols(Symbols.load("aircraft"))
    s.collection("aircraft", filter={"hide": local("adsb.hide", [])})
    s.hold("adsb", start=A.post("api/adsb/session", {"client": A.CLIENT}, level="control", timeout_s=30),
                   stop=A.delete("api/adsb/session?client={$client}", level="control"))
    s.live("ui/v2/live/radio/aircraft")
    s.root = p.stack(p.map("map", ground=..., layers=[...]),
                     p.panel("nearest", p.list("list", from_="aircraft", template=...), slot="end", w=300))
    doc = s.json()          # validated, downgraded for req's client, rev computed

Everything a builder returns is a plain dict, the JSON itself: a part, an
action or a typed value can be built here, written by hand, or tweaked
after building, and the document is just those dicts put together. What
the helpers add is what is easy to get wrong by hand:

- **Names.** Keyword arguments are the contract's names in snake case:
  `on_activate=` is `onActivate`, `timeout_s=` is `timeoutS`, `from_=`
  is `from` (Python keywords take a trailing underscore). A camelCase
  keyword passes through unchanged, so the contract's tables can be
  copied as they are.
- **Checks where the line is written.** Every action is checked as it is
  made (a transmit level without `tx` and a lease, a path with `..`, an
  unknown placeholder), so the error names the app's own line rather than
  a test that later validates the whole screen. Screen.json() then runs
  every rule of check.py over the whole document.
- **Clients older than the document.** Screen.json() replaces each part
  newer than the client (its `since`, or the minor its type came in) with
  its `fallback`, or leaves it out (SCREENS.md §2.3).

Item fields (a collection's items, `put`'s fields) are data and keep the
names they are given: no case conversion happens inside data.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .check import PART_KEYS, Problem, check, check_action, is_part

UI_MAJOR = 2
# The newest minor this package builds. Each minor adds parts,
# properties, ops or actions and never changes one (§2.3).
UI_MINOR = 0
UI_VERSION = f"{UI_MAJOR}.{UI_MINOR}"

# The minor each part type came in. Every type is 2.0 today; a part added
# in 2.1 goes here as 1, and Screen.json() then sends its fallback to a
# 2.0 client without the app having to write `since` on every use.
PART_SINCE: dict[str, int] = {}

CLIENT = "$client"
VALUE = "$value"
HZ = "$hz"


class UIError(ValueError):
    """A document (or one action) that must not be served. `problems` are
    check.py's, in document order; `code` is the first one's."""

    def __init__(self, problems: list[Problem]):
        self.problems = list(problems)
        self.code = self.problems[0].code if self.problems else "invalid"
        more = f" (and {len(self.problems) - 1} more)" if len(self.problems) > 1 else ""
        super().__init__(f"{self.problems[0] if self.problems else 'invalid'}{more}")


class TransmitActionError(UIError):
    """Something tried to transmit outside a ptt/txArm lease (SCREENS.md §7)."""


def _raise(problems: list[Problem]) -> None:
    if problems:
        cls = TransmitActionError if problems[0].code.startswith("transmit") else UIError
        raise cls(problems)


# ---------------------------------------------------------------- names, values

def camel(name: str) -> str:
    """`on_activate` -> `onActivate`, `timeout_s` -> `timeoutS`,
    `from_` -> `from`; `keepFocus` stays `keepFocus`."""
    head, *rest = name.rstrip("_").split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def iso(t: datetime) -> str:
    """An aware datetime as the contract's ISO time, UTC with a Z."""
    if t.tzinfo is None:
        raise ValueError("a naive datetime has no zone: give it one (the box's clock is UTC)")
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond // 1000:03d}" if t.microsecond else "") + "Z"


def _value(v: Any) -> Any:
    """A property value as JSON: datetimes become ISO times, tuples lists."""
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, (list, tuple)):
        return [_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _value(x) for k, x in v.items()}
    return v


def _props(props: dict) -> dict:
    return {camel(k): _value(v) for k, v in props.items()}


def item(field: str) -> dict:
    """The current item's field, inside a template (§2.1): `{"$": field}`.
    Dotted paths reach into nested objects: `item("photo.src")`."""
    return {"$": field}


def local(key: str, default: Any = None, *, has: Any = None, not_: bool = False) -> dict:
    """A per-device view preference (§2.1), set by `A.local`. `has` reads
    a list value as "contains this"; `not_` inverts a boolean."""
    v = {"local": key, "default": _value(default)}
    if has is not None:
        v["has"] = has
    if not_:
        v["not"] = True
    return v


class T:
    """Typed values (§2.1): what the client ticks itself from one shared
    clock, so a countdown needs no patch a second. Times are datetimes,
    ISO strings or `item(...)`. `text=` is what a client that does not
    know the kind draws instead."""

    @staticmethod
    def _t(v: dict, text: str | None) -> dict:
        return v | ({"text": text} if text is not None else {})

    @staticmethod
    def freq(hz: float | dict, unit: str = "MHz", decimals: int | None = None, *, text: str | None = None) -> dict:
        return T._t({"freq": hz, "unit": unit} | ({"decimals": decimals} if decimals is not None else {}), text)

    @staticmethod
    def ago(at, *, text: str | None = None) -> dict:
        return T._t({"ago": _value(at)}, text)

    @staticmethod
    def since(at, *, text: str | None = None) -> dict:
        return T._t({"since": _value(at)}, text)

    @staticmethod
    def clock(zone: str = "local", style: str = "hm", *, text: str | None = None) -> dict:
        return T._t({"clock": zone, "style": style}, text)

    @staticmethod
    def time(at, zone: str = "local", style: str = "hm", *, text: str | None = None) -> dict:
        return T._t({"time": _value(at), "zone": zone, "style": style}, text)

    @staticmethod
    def countdown(to, texts: list[str], then=None, *, text: str | None = None) -> dict:
        """`texts`: before and after (two), or before, between `to` and
        `then`, and after (three); `{d}` is the ticking duration."""
        c = {"to": _value(to), "text": list(texts)}
        if then is not None:
            c["then"] = _value(then)
        return T._t({"countdown": c}, text)

    @staticmethod
    def cycle(period_s: float, offset_s: float = 0, *, text: str | None = None) -> dict:
        return T._t({"cycle": {"periodS": period_s, "offsetS": offset_s}}, text)


# ---------------------------------------------------------------- actions

def _checked(a: dict) -> dict:
    # Checked without the document: references (parts, leases) are the
    # whole document's business and are checked by Screen.json(). Bindings
    # are allowed here because the action may sit in a template.
    from .check import Ctx
    _raise(check_action(a, ctx=Ctx(item=True)))
    return a


class A:
    """Actions (§5). Every `call` names its level: a default would be one
    more place a level is decided without anyone reading it."""

    CLIENT = CLIENT
    VALUE = VALUE
    HZ = HZ

    @staticmethod
    def _common(a: dict, extra: dict) -> dict:
        return _checked(a | _props(extra))

    @staticmethod
    def call(method: str, path: str, body: Any = None, *, level: str, tx: bool = False,
             lease: str | None = None, **kw) -> dict:
        """An HTTP call under the app's base. `kw`: text, hint, confirm,
        timeout_s. A transmit-capable call (it changes or queues what an
        armed or keyed transmitter sends) is `tx=True` with the `lease` of
        a ptt or txArm part on the screen (§7.2)."""
        a = {"do": "call", "method": method, "path": path}
        if body is not None:
            a["body"] = _value(body)
        a["level"] = level
        if tx:
            a["tx"] = True
        if lease is not None:
            a["lease"] = lease
        return A._common(a, kw)

    @staticmethod
    def post(path: str, body: Any = None, *, level: str, **kw) -> dict:
        return A.call("POST", path, body, level=level, **kw)

    @staticmethod
    def put(path: str, body: Any = None, *, level: str, **kw) -> dict:
        return A.call("PUT", path, body, level=level, **kw)

    @staticmethod
    def delete(path: str, body: Any = None, *, level: str, **kw) -> dict:
        return A.call("DELETE", path, body, level=level, **kw)

    @staticmethod
    def open(screen: str, params: dict | None = None, *, app: str | None = None, **kw) -> dict:
        a = {"do": "open", "screen": screen}
        if params:
            a["params"] = _value(params)
        if app is not None:
            a["app"] = app
        return A._common(a, kw)

    @staticmethod
    def back(**kw) -> dict:
        return A._common({"do": "back"}, kw)

    @staticmethod
    def show(part: str, *, tab: str | None = None, **kw) -> dict:
        return A._common({"do": "show", "part": part} | ({"tab": tab} if tab else {}), kw)

    @staticmethod
    def hide(part: str, **kw) -> dict:
        return A._common({"do": "hide", "part": part}, kw)

    @staticmethod
    def focus(part: str, **kw) -> dict:
        return A._common({"do": "focus", "part": part}, kw)

    @staticmethod
    def part(part: str, verb: str, **kw) -> dict:
        """A part's own verb: a map's zoomIn/fit/follow/..., audio's mute."""
        return A._common({"do": "part", "part": part, "verb": verb}, kw)

    @staticmethod
    def select(sel: str, key: Any, **kw) -> dict:
        return A._common({"do": "select", "sel": sel, "key": key}, kw)

    _MISSING = object()

    @staticmethod
    def local(key: str, *, set: Any = _MISSING, toggle: Any = _MISSING, cycle: list | None = None, **kw) -> dict:
        a = {"do": "local", "key": key}
        if set is not A._MISSING:
            a["set"] = _value(set)
        if toggle is not A._MISSING:
            a["toggle"] = toggle
        if cycle is not None:
            a["cycle"] = _value(cycle)
        return A._common(a, kw)

    @staticmethod
    def send(name: str, value: Any = None, **kw) -> dict:
        return A._common({"do": "send", "name": name} | ({"value": _value(value)} if value is not None else {}), kw)

    @staticmethod
    def link(url: str, *, qr: bool | None = None, **kw) -> dict:
        return A._common({"do": "link", "url": url} | ({"qr": qr} if qr is not None else {}), kw)

    @staticmethod
    def seq(*actions: dict, **kw) -> dict:
        return A._common({"do": "seq", "actions": list(actions)}, kw)

    @staticmethod
    def ask(title: str, text: str, *, tone: str | None = None, yes: str | None = None, no: str | None = None) -> dict:
        """The `confirm=` of an action (§5.4); focus starts on `no`."""
        c = {"title": title, "text": text}
        for k, v in (("tone", tone), ("yes", yes), ("no", no)):
            if v is not None:
                c[k] = v
        return c


# ---------------------------------------------------------------- parts

def part(type_: str, **props) -> dict:
    """Any part by its type, the escape hatch for a type newer than this
    package (give it `since=` and a `fallback=`)."""
    return {"type": type_} | _props(props)


class p:
    """One builder per part of §3. Containers take their children
    positionally; parts that are patched or addressed by name take their
    id first; the rest take their main value first. Every other property
    is a keyword, and `id=` works everywhere."""

    part = staticmethod(part)

    # ---- layout (§3.1)
    @staticmethod
    def column(*children: dict, **props) -> dict:
        return part("column", children=list(children), **props)

    @staticmethod
    def row(*children: dict, **props) -> dict:
        return part("row", children=list(children), **props)

    @staticmethod
    def stack(*children: dict, **props) -> dict:
        return part("stack", children=list(children), **props)

    @staticmethod
    def split(first: dict, second: dict, **props) -> dict:
        return part("split", children=[first, second], **props)

    @staticmethod
    def frame(w: float, h: float, *children: dict, **props) -> dict:
        """A fixed design in `w` x `h` units; each child has `rect=[x, y, w, h]`."""
        return part("frame", w=w, h=h, children=list(children), **props)

    @staticmethod
    def scroll(child: dict, **props) -> dict:
        return part("scroll", child=child, **props)

    @staticmethod
    def tabs(id: str, tabs: list[dict], **props) -> dict:
        return part("tabs", id=id, tabs=tabs, **props)

    @staticmethod
    def tab(id: str, label: str, child: dict, **props) -> dict:
        """One entry of `tabs` (not a part itself)."""
        return {"id": id, "label": label} | _props(props) | {"child": child}

    @staticmethod
    def grid(*children: dict, **props) -> dict:
        """Fixed children, or `from_=` + `template=` for a collection."""
        return part("grid", **({"children": list(children)} if children else {}), **props)

    @staticmethod
    def list(id: str, **props) -> dict:
        return part("list", id=id, **props)

    @staticmethod
    def table(id: str, columns: list[dict], **props) -> dict:
        return part("table", id=id, columns=columns, **props)

    @staticmethod
    def column_of(label: str, value: Any, **props) -> dict:
        """One entry of a table's `columns` (not a part itself)."""
        return {"label": label, "value": value} | _props(props)

    @staticmethod
    def panel(id: str, child: dict | None = None, **props) -> dict:
        return part("panel", id=id, **props, **({"child": child} if child is not None else {}))

    @staticmethod
    def section(title: str, *children: dict, **props) -> dict:
        return part("section", title=title, children=list(children), **props)

    @staticmethod
    def card(child: dict, **props) -> dict:
        return part("card", child=child, **props)

    @staticmethod
    def spacer(**props) -> dict:
        return part("spacer", **props)

    @staticmethod
    def divider(**props) -> dict:
        return part("divider", **props)

    # ---- showing things (§3.2)
    @staticmethod
    def text(value: Any, **props) -> dict:
        return part("text", value=value, **props)

    @staticmethod
    def field(label: str, value: Any, **props) -> dict:
        return part("field", label=label, value=value, **props)

    @staticmethod
    def badge(text: Any = None, **props) -> dict:
        return part("badge", **({"text": text} if text is not None else {}), **props)

    @staticmethod
    def icon(name: str | None = None, **props) -> dict:
        """`name` from the client's icon set, or `sym=` from the document's symbols."""
        return part("icon", **({"name": name} if name is not None else {}), **props)

    @staticmethod
    def image(src: Any, **props) -> dict:
        return part("image", src=src, **props)

    @staticmethod
    def qr(data: Any, **props) -> dict:
        return part("qr", data=data, **props)

    @staticmethod
    def progress(value: Any, **props) -> dict:
        return part("progress", value=value, **props)

    @staticmethod
    def meter(value: Any = None, **props) -> dict:
        return part("meter", **({"value": value} if value is not None else {}), **props)

    @staticmethod
    def chart(id: str, marks: list[dict], **props) -> dict:
        return part("chart", id=id, marks=marks, **props)

    @staticmethod
    def skyplot(id: str, **props) -> dict:
        return part("skyplot", id=id, **props)

    @staticmethod
    def map(id: str, **props) -> dict:
        return part("map", id=id, **props)

    @staticmethod
    def layer(id: str, kind: str, **props) -> dict:
        """One entry of a map's `layers` (not a part): markers, lines,
        polygons, rings or images (§3.6)."""
        return {"id": id, "kind": kind} | _props(props)

    @staticmethod
    def spectrum(id: str, stream: dict, **props) -> dict:
        return part("spectrum", id=id, stream=stream, **props)

    @staticmethod
    def video(id: str, src: str, **props) -> dict:
        return part("video", id=id, src=src, **props)

    @staticmethod
    def banner(text: Any, **props) -> dict:
        return part("banner", text=text, **props)

    @staticmethod
    def empty(text: Any, **props) -> dict:
        return part("empty", text=text, **props)

    @staticmethod
    def hints(**props) -> dict:
        return part("hints", **props)

    @staticmethod
    def tile(id: str, title: Any, **props) -> dict:
        return part("tile", id=id, title=title, **props)

    # ---- input (§3.8)
    @staticmethod
    def button(text: Any, action: dict | None = None, **props) -> dict:
        return part("button", text=text, **props, **({"action": action} if action is not None else {}))

    @staticmethod
    def toggle(text: Any, value: Any, action: dict, **props) -> dict:
        return part("toggle", text=text, value=value, action=action, **props)

    @staticmethod
    def choice(options: list[dict], value: Any, action: dict, **props) -> dict:
        return part("choice", options=options, value=value, action=action, **props)

    @staticmethod
    def option(value: Any, text: Any, **props) -> dict:
        """One entry of a choice's `options` (not a part)."""
        return {"value": _value(value), "text": text} | _props(props)

    @staticmethod
    def slider(value: Any, min: float, max: float, action: dict, **props) -> dict:
        return part("slider", value=value, min=min, max=max, action=action, **props)

    @staticmethod
    def number(value: Any, action: dict, **props) -> dict:
        return part("number", value=value, action=action, **props)

    @staticmethod
    def frequency(id: str, value: Any, action: dict, **props) -> dict:
        return part("frequency", id=id, value=value, action=action, **props)

    @staticmethod
    def text_input(value: Any, action: dict, **props) -> dict:
        return part("textInput", value=value, action=action, **props)

    @staticmethod
    def pin(digits: int, action: dict, **props) -> dict:
        return part("pin", digits=digits, action=action, **props)

    # ---- transmit (§7): placed by the box, keyed only by client code
    @staticmethod
    def ptt(id: str, key: str, heartbeat: str, keep: str | None = None, *, hold_ms: int = 1000, **props) -> dict:
        """Hold to key. Its timings are client code; only `holdMs` may be
        raised, never below 1000, and it takes no action of any kind."""
        return part("ptt", id=id, key=key, heartbeat=heartbeat, **({"keep": keep} if keep else {}),
                    hold_ms=hold_ms, **props)

    @staticmethod
    def tx_arm(id: str, arm: str, heartbeat: str, **props) -> dict:
        """Enable TX. `digital=True` lets a TV draw it when an administrator
        allowed digital transmit for this app (§7.4)."""
        return part("txArm", id=id, arm=arm, heartbeat=heartbeat, **props)

    # ---- media (§9)
    @staticmethod
    def audio(id: str, src: str, **props) -> dict:
        return part("audio", id=id, src=src, **props)

    @staticmethod
    def camera(id: str, srt: str, **props) -> dict:
        return part("camera", id=id, srt=srt, **props)


# ---------------------------------------------------------------- the document

class Collection:
    """A keyed item set (§2.2). Items are data: their field names are kept
    as given. `order` is the display order the box chose."""

    def __init__(self, items: dict | None = None, order: list | None = None, filter: dict | None = None):
        self.items: dict[str, dict] = {k: dict(v) for k, v in (items or {}).items()}
        self.order: list[str] = list(order) if order is not None else list(self.items)
        self.filter = filter

    def put(self, key: str, fields: dict | None = None, **more) -> Collection:
        """Add or replace one item; it goes last in the order if new."""
        if key not in self.items:
            self.order.append(key)
        self.items[key] = _value(dict(fields or {}, **more))
        return self

    def to_json(self) -> dict:
        c = {"items": copy.deepcopy(self.items), "order": list(self.order)}
        if self.filter is not None:
            c["filter"] = _value(self.filter)
        return c


class Screen:
    """One screen document (§2). `req` (serve.Req) supplies the form, the
    device's level and the client's ui minor; `form` and `level` override
    it, and a Screen built without either is for a TV at view level (the
    most restricted case, so a forgotten argument errs safe)."""

    def __init__(self, screen: str, *, title: Any = None, req=None, form: str | None = None,
                 level: str | None = None, base_kind: str = "app", root: dict | None = None):
        self.screen = screen
        self.title = title
        self.req = req
        self.form = form or (req.form if req is not None else "tv")
        self.level = level or (req.level if req is not None else "view")
        self.base_kind = base_kind
        self.root = root
        self.lang = req.lang if req is not None else None
        self._fields: dict[str, Any] = {}
        self._collections: dict[str, Collection] = {}
        self._holds: list[dict] = []
        self._keys: dict[str, dict] = {}

    def _merge(self, name: str, values: dict) -> Screen:
        self._fields.setdefault(name, {}).update(_value(values))
        return self

    def backdrop(self, style: str | None = None, *, image: str | None = None, dim: float | None = None) -> Screen:
        b = {"style": style} if style else {}
        if image is not None:
            b["image"] = image
        if dim is not None:
            b["dim"] = dim
        self._fields["backdrop"] = b
        return self

    def words(self, words: dict) -> Screen:
        """Unit words for ticking values, already in the screen's language."""
        return self._merge("words", words)

    def colors(self, colors: dict) -> Screen:
        """Data colours by name, each {"dark": ..., "light": ...} (§8.4)."""
        return self._merge("colors", colors)

    def symbols(self, symbols) -> Screen:
        """A Symbols set, or a dict of symbols; several calls add up."""
        return self._merge("symbols", symbols.to_json() if hasattr(symbols, "to_json") else symbols)

    def collection(self, name: str, items: dict | None = None, order: list | None = None,
                   filter: dict | None = None) -> Collection:
        c = Collection(items, order, filter)
        self._collections[name] = c
        return c

    def selection(self, name: str, from_: str) -> Screen:
        return self._merge("selections", {name: {"from": from_}})

    def hold(self, name: str, start: dict, stop: dict, renew_s: float | None = None) -> Screen:
        """What the screen keeps on the box while it is open (§6): `start`
        on push, return and reconnect; `stop` on pop and background."""
        h = {"name": name, "start": start, "stop": stop}
        if renew_s is not None:
            h["renewS"] = renew_s
        self._holds.append(h)
        return self

    def live(self, path: str) -> Screen:
        self._fields["live"] = {"path": path}
        return self

    def poll(self, path: str, every_s: float) -> Screen:
        self._fields["poll"] = {"path": path, "everyS": every_s}
        return self

    def key(self, name: str, action: dict) -> Screen:
        """A screen-wide remote key (§8.2): ok, menu, play, chUp, ..."""
        self._keys[name] = action
        return self

    def document(self) -> dict:
        """The document as built: not validated, not downgraded, no rev."""
        d: dict[str, Any] = {"ui": UI_VERSION, "screen": self.screen}
        if self.title is not None:
            d["title"] = self.title
        d["form"] = self.form
        d["level"] = self.level
        for k in ("backdrop", "words", "colors", "symbols"):
            if k in self._fields:
                d[k] = copy.deepcopy(self._fields[k])
        if self._collections:
            d["collections"] = {n: c.to_json() for n, c in self._collections.items()}
        if "selections" in self._fields:
            d["selections"] = copy.deepcopy(self._fields["selections"])
        if self._holds:
            d["holds"] = copy.deepcopy(self._holds)
        for k in ("live", "poll"):
            if k in self._fields:
                d[k] = dict(self._fields[k])
        if self._keys:
            d["keys"] = copy.deepcopy(self._keys)
        d["root"] = copy.deepcopy(self.root)
        return d

    def json(self) -> dict:
        """The document to send: every rule of check.py (UIError, or
        TransmitActionError, naming each problem), then the parts newer
        than the client replaced by their fallbacks, then `rev`."""
        d = self.document()
        _raise(check(d, base_kind=self.base_kind))
        minor = self.req.minor if self.req is not None else UI_MINOR
        d = downgrade(d, minor)
        d["rev"] = rev(d)
        return d


def rev(doc: dict) -> str:
    """The document's hash (§2 `rev`, also its ETag): 12 hex digits of the
    SHA-256 of its canonical JSON, `rev` itself left out. The same
    document gives the same rev in any process, so a client's cached
    ETag survives a restart of the app."""
    body = {k: v for k, v in doc.items() if k != "rev"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- older clients

def part_minor(part: dict) -> int:
    """The minor a part needs: its own `since` or its type's, whichever is newer."""
    m = PART_SINCE.get(part.get("type"), 0)
    since = part.get("since")
    if isinstance(since, str) and since.startswith(f"{UI_MAJOR}."):
        try:
            m = max(m, int(since.split(".", 1)[1]))
        except ValueError:
            pass
    return m


def _down(part: Any, minor: int) -> dict | None:
    """The part as a client of `minor` gets it: itself, its fallback, or
    nothing. A fallback is downgraded too (it may be newer still)."""
    if not is_part(part):
        return part
    if part_minor(part) > minor:
        return _down(part.get("fallback"), minor) if is_part(part.get("fallback")) else None
    out = dict(part)
    if isinstance(part.get("children"), list):
        out["children"] = [c for c in (_down(c, minor) for c in part["children"]) if c is not None]
    if part.get("type") == "tabs" and isinstance(part.get("tabs"), list):
        tabs = []
        for t in part["tabs"]:
            if isinstance(t, dict) and "child" in t:
                child = _down(t["child"], minor)
                t = {k: v for k, v in t.items() if k != "child"} | ({"child": child} if child is not None else {})
            tabs.append(t)
        out["tabs"] = tabs
    for k in PART_KEYS:
        if k in part:
            sub = _down(part[k], minor)
            if sub is None:
                del out[k]
            else:
                out[k] = sub
    return out


def downgrade(doc: dict, minor: int) -> dict:
    """§2.3: every part newer than a client of ui 2.`minor` becomes its
    `fallback`, or goes. A root with nothing to fall back to becomes an
    `empty` part rather than no document at all. The `ui` field says
    what was sent."""
    d = dict(doc)
    if minor >= UI_MINOR:
        return d
    root = _down(doc.get("root"), minor)
    d["root"] = root if root is not None else {"type": "empty", "text": ""}
    d["ui"] = f"{UI_MAJOR}.{minor}"
    return d


def descriptor(entries: list[dict], *, min_client: str = UI_VERSION, **fields) -> dict:
    """An app's `ui/v2/app` answer (§1): the launcher entries this app has
    now. `fields`: app, kind, name, version, art (the descriptor's own)."""
    return {"ui": UI_VERSION} | _props(fields) | {"minClient": min_client, "entries": entries}


def entry(id: str, title: Any, screen: str, *, art: str | None = None, icon: str | None = None,
          params: dict | None = None, forms: list[str] | None = None) -> dict:
    """One launcher entry of a descriptor; its tile's id is `<app>/<id>`."""
    e = {"id": id, "title": title}
    if art is not None:
        e["art"] = art
    if icon is not None:
        e["icon"] = icon
    e["screen"] = screen
    if params:
        e["params"] = params
    if forms is not None:
        e["forms"] = list(forms)
    return e


def status(entries: dict[str, tuple[Any, str] | dict]) -> dict:
    """An app's `ui/v2/status` answer: {entry: (status, tone)} per entry."""
    out = {}
    for k, v in entries.items():
        out[k] = v if isinstance(v, dict) else {"status": v[0], "tone": v[1]}
    return {"entries": out}
