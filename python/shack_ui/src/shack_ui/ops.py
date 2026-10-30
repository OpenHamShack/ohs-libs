"""The reference op applier (SCREENS.md §4.2): what a renderer holds, and
how each live frame or poll answer changes it.

    state_from_document(doc)      -> state   what a renderer starts from
    apply_ops(state, ops)         -> state   one frame, without mutating
    apply_frame(state, frame, doc_state) -> state   a `snap` or `p` frame
    drawn_keys(state, c)          -> [key]   what a list or map draws, in order
    public_state(state)           -> dict    the language-neutral form the
                                             vectors compare (ops.json)

Moved from the contract's fixture tool (uidoc.py) with check.py. App tests
use it to assert what a client would see after their LiveHub's frames, and
the Kotlin and Swift renderers are held to it by spec/vectors/ui2/ops.json.
"""

from __future__ import annotations

import copy

from .check import PART_KEYS, is_part, iter_parts

def _props(part: dict) -> dict:
    """A part's own properties, without its child parts: the state keeps
    each id'd part flat, as a renderer's store does, so a `set` on a child
    never has to find it inside its parent."""
    # A child-part key holding something that is not a part is an ordinary
    # property: a spectrum's `overlay: true` (§3.7) is a flag, where a
    # video's `overlay` (§9) is a part.
    p = {k: copy.deepcopy(v) for k, v in part.items()
         if not ((k in PART_KEYS and is_part(v)) or k == "children")}
    if part.get("type") == "tabs" and isinstance(p.get("tabs"), list):
        p["tabs"] = [{k: v for k, v in t.items() if k != "child"} if isinstance(t, dict) else t
                     for t in p["tabs"]]
    return p


def _index_parts(root, parts: dict, subtrees: dict, charts: dict):
    ids = []
    for part, _, ctx in iter_parts(root):
        pid = part.get("id")
        if not isinstance(pid, str) or ctx.repeats:
            continue
        parts[pid] = _props(part)
        ids.append(pid)
        if part.get("type") == "chart":
            charts[pid] = {i: copy.deepcopy(m.get("points", [])) if isinstance(m, dict) else []
                           for i, m in enumerate(part.get("marks") or [])}
    for part, _, ctx in iter_parts(root):
        pid = part.get("id")
        if isinstance(pid, str) and not ctx.repeats:
            subtrees[pid] = [d.get("id") for d, _, c in iter_parts(part)
                             if d is not part and isinstance(d.get("id"), str) and not c.repeats]
    return ids


def state_from_document(doc: dict) -> dict:
    """The state a renderer starts from: id'd parts (flat, their own
    properties), the collections, chart marks' points by index."""
    parts, subtrees, charts = {}, {}, {}
    _index_parts(doc.get("root"), parts, subtrees, charts)
    colls = {name: {"items": copy.deepcopy((c or {}).get("items") or {}),
                    "order": list((c or {}).get("order") or [])}
             for name, c in (doc.get("collections") or {}).items()}
    return {"parts": parts, "collections": colls, "charts": charts, "toasts": [], "reload": False,
            "subtrees": subtrees}


def drawn_keys(state: dict, c: str) -> list:
    """The keys a renderer draws for collection `c`, in order (§2.2): those
    in `order` that have an item, then any item missing from `order`,
    sorted by key. Sorted, not in the order the items object lists them:
    Swift's JSON objects keep no order, and a box that removed and
    re-added an item between two frames cannot say where it now sits, so
    "object order" would differ between a client and the box, and between
    platforms."""
    coll = state["collections"].get(c) or {"items": {}, "order": []}
    items, order = coll["items"], coll["order"]
    first = [k for k in order if k in items]
    seen = set(first)
    return first + sorted(k for k in items if k not in seen)


def _merge(target: dict, fields: dict):
    for k, v in fields.items():
        if v is None:
            target.pop(k, None)
        else:
            target[k] = copy.deepcopy(v)


def apply_ops(state: dict, ops: list) -> dict:
    """§4.2: apply one frame's ops in order and return the new state; the
    given state is not changed. Unknown ops, parts, collections, keys and
    chart marks are ignored, as the renderer must ignore them."""
    s = copy.deepcopy(state)
    s.setdefault("subtrees", {})
    parts, colls, charts = s["parts"], s["collections"], s["charts"]
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "set":
            if op.get("id") in parts and isinstance(op.get("v"), dict):
                _merge(parts[op["id"]], op["v"])
        elif kind == "replace":
            pid, new = op.get("id"), op.get("part")
            if pid in parts and is_part(new):
                # The old subtree goes with it; the new one is indexed, and
                # keeps the id it replaces whatever the new part says.
                for gone in s["subtrees"].pop(pid, []):
                    parts.pop(gone, None)
                    s["subtrees"].pop(gone, None)
                    charts.pop(gone, None)
                new = dict(new, id=pid)
                _index_parts(new, parts, s["subtrees"], charts)
        elif kind == "up":
            coll = colls.get(op.get("c"))
            if coll is not None and isinstance(op.get("items"), dict):
                for key, fields in op["items"].items():
                    if isinstance(fields, dict):
                        _merge(coll["items"].setdefault(key, {}), fields)
        elif kind == "rm":
            coll = colls.get(op.get("c"))
            if coll is not None:
                gone = set(op.get("keys") or [])
                for key in gone:
                    coll["items"].pop(key, None)
                # Dropped from the order too: a key in `order` with no item
                # is ignored when drawn anyway, and this keeps it from
                # coming back in the right place if an `up` recreates it.
                coll["order"] = [k for k in coll["order"] if k not in gone]
        elif kind == "order":
            coll = colls.get(op.get("c"))
            if coll is not None and isinstance(op.get("keys"), list):
                coll["order"] = list(op["keys"])
        elif kind == "push":
            coll = colls.get(op.get("c"))
            item = coll["items"].get(op.get("key")) if coll is not None else None
            if item is not None and isinstance(op.get("f"), str) and isinstance(op.get("v"), list):
                arr = item.get(op["f"])
                arr = list(arr) if isinstance(arr, list) else []
                arr.extend(copy.deepcopy(op["v"]))
                if isinstance(op.get("max"), int) and op["max"] >= 0:
                    arr = arr[len(arr) - op["max"]:] if len(arr) > op["max"] else arr
                item[op["f"]] = arr
        elif kind == "append":
            chart = charts.get(op.get("id"))
            mark = op.get("mark", 0)
            if chart is not None and mark in chart and isinstance(op.get("v"), list):
                pts = chart[mark] + copy.deepcopy(op["v"])
                if isinstance(op.get("max"), int) and op["max"] >= 0 and len(pts) > op["max"]:
                    pts = pts[len(pts) - op["max"]:]
                chart[mark] = pts
        elif kind == "reload":
            s["reload"] = True
        elif kind == "toast":
            s["toasts"].append({k: op.get(k) for k in ("text", "tone", "s")})
    return s


def apply_frame(state: dict, frame: dict, doc_state: dict) -> dict:
    """One frame from the live socket (§4.1). A `snap` is applied to the
    document's own state, not on top of what the client held: it is every
    collection and patched part in full, so an item removed while the
    socket was down must not survive it, and the snap has no op that says
    "forget everything else". A `p` frame is applied on top. Anything else
    (hello, ping) leaves the state alone."""
    t = frame.get("t") if isinstance(frame, dict) else None
    if t == "snap":
        return apply_ops(doc_state, frame.get("ops"))
    if t == "p":
        return apply_ops(state, frame.get("ops"))
    return state


def public_state(state: dict) -> dict:
    """The state in the form the vectors write down: parts and collections
    as held, `drawn` (each collection's drawn keys, §2.2), chart marks'
    points as a list per chart (JSON has no integer keys), toasts and
    reload. `subtrees` is this applier's own bookkeeping and is left out:
    a port may track replaced subtrees however it likes."""
    charts = {}
    for cid, marks in state.get("charts", {}).items():
        n = max(marks) + 1 if marks else 0
        charts[cid] = [copy.deepcopy(marks.get(i, [])) for i in range(n)]
    return {"parts": copy.deepcopy(state["parts"]),
            "collections": copy.deepcopy(state["collections"]),
            "drawn": {c: drawn_keys(state, c) for c in state["collections"]},
            "charts": charts,
            "toasts": copy.deepcopy(state["toasts"]),
            "reload": state["reload"]}
