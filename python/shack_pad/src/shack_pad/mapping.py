"""Which action each control runs: an app's default layout, the operator's
changes on top, up to MAX_PAGES pages.

Moved from the box (pad.py resolve/set_binding/set_pages/reset) with the
rules unchanged:
- Every check is made on read, so a stored map from another version, a
  restore or a hand edit "can only fall back, never misfire".
- Transmit (`ptt`/`transmit`) and the page key are `single`: one control,
  page 1 only, and the same control on every page - the stop key never
  moves.
- The transmit key cannot be moved or removed while that app is on air.

What changed: the default comes from the app's pad-layout.json instead of
the driver, and the operator's changes are one entry per app kind and model
in the station profile (CONTROLLER-PAD.md §5):

    controllerMaps.<kind>.<model> = {"pages": [{control: {slot: action}}, ...]}

Storage is shack_client's job, so the functions here take that entry as a
dict and return a new one; nothing is written from here.
"""

from __future__ import annotations

from .actions import Action
from .drivers.base import Driver, slots
from .i18n import _

MAX_PAGES = 4
LAYOUT_SCHEMA = 1


def _blank(kind: str) -> dict[str, str]:
    return {s: ("none" if s == "press" else "none-turn") for s in slots(kind)}


def stored_pages(entry: dict | None) -> list[dict]:
    pages = entry.get("pages") if isinstance(entry, dict) else None
    if not isinstance(pages, list) or not pages:
        return [{}]
    return [p if isinstance(p, dict) else {} for p in pages[:MAX_PAGES]]


def entry_of(maps: dict | None, kind: str, model: str) -> dict:
    """controllerMaps.<kind>.<model>, or {} for anything missing or junk."""
    if not isinstance(maps, dict) or not isinstance(maps.get(kind), dict):
        return {}
    e = maps[kind].get(model)
    return e if isinstance(e, dict) else {}


def with_entry(maps: dict | None, kind: str, model: str, entry: dict | None) -> dict:
    """A copy of controllerMaps with one entry replaced (None removes it)."""
    out = {k: dict(v) for k, v in (maps or {}).items() if isinstance(v, dict)}
    mine = out.setdefault(kind, {})
    if entry is None:
        mine.pop(model, None)
        if not mine:
            out.pop(kind)
    else:
        mine[model] = entry
    return out


def default_page(drv: type[Driver], layout: dict | None, acts: dict[str, Action]) -> tuple[dict, list[str]]:
    """Page 1 as the app ships it, checked: anything the layout names that
    cannot be used becomes nothing, with a note (CONTROLLER-PAD.md §4)."""
    notes: list[str] = []
    page = {c["id"]: _blank(c["kind"]) for c in drv.controls}
    mine = None
    if isinstance(layout, dict):
        # A model may borrow another's layout: a desktop window's keys
        # (`keyboard`) use the app's keypad layout, same key names.
        mine = layout.get(drv.model)
        if mine is None and getattr(drv, "layout_from", None):
            mine = layout.get(drv.layout_from)
    if isinstance(mine, dict):
        for cid, binding in mine.items():
            kind = drv.control_kind(cid) if isinstance(cid, str) else None
            if not kind:
                notes.append(_("{cid}: not a control on the {model}; left out").format(cid=cid, model=drv.name))
                continue
            if not isinstance(binding, dict):
                continue
            page.setdefault(cid, _blank(kind))
            for slot, aid in binding.items():
                why = _unusable(kind, slot, aid, acts)
                if why:
                    notes.append(f"{cid}: {why}")
                else:
                    page[cid][slot] = aid
    else:
        # No layout for this model: only transmit has a standard place
        # (K1 / KPENTER), so a new model still has its stop key where every
        # other app puts it.
        tx = [a for a in acts.values() if a.tx]
        home = getattr(drv, "tx_home", None)
        if tx and home and drv.control_kind(home):
            page.setdefault(home, _blank(drv.control_kind(home)))["press"] = tx[0].id
    return page, notes


def _unusable(kind: str, slot: str, aid, acts: dict[str, Action], stored: bool = False) -> str | None:
    """Why aid cannot go in this slot, or None. A stored change that cannot
    be used leaves the default in place; a layout entry leaves nothing."""
    if slot not in slots(kind):
        return _("a {kind} cannot {slot}; left out").format(kind=kind, slot=slot)
    instead = _("using the default") if stored else _("nothing instead")
    if aid not in acts:
        return _("{aid!r} is not an action of this app here; {instead}").format(aid=aid, instead=instead)
    if acts[aid].kind != slot:
        return _("{aid} is a {kind} action; {instead}").format(aid=aid, kind=acts[aid].kind, instead=instead)
    return None


def resolve(drv: type[Driver], layout: dict | None, entry: dict | None,
            acts: dict[str, Action]) -> tuple[list[dict], list[str]]:
    """The mapping in force: one {control: {slot: action}} per page, and notes
    on anything that could not be used. Page 1 is the app's default with the
    operator's changes; a later page starts as page 1 and has its own."""
    first, notes = default_page(drv, layout, acts)
    raw = stored_pages(entry)
    pages: list[dict] = []
    explicit: set[str] = set()
    singles = [a for a in acts if acts[a].single]
    for i, over in enumerate(raw):
        page = {cid: dict(b) for cid, b in (first if i == 0 else pages[0]).items()}
        for cid, binding in over.items():
            where = cid if not i else _("{cid} on page {n}").format(cid=cid, n=i + 1)
            kind = drv.control_kind(cid) if isinstance(cid, str) else None
            if not kind:
                notes.append(_("{cid}: not a control on the {model}; left out").format(cid=cid, model=drv.name))
                continue
            if not isinstance(binding, dict):
                continue
            for slot, aid in binding.items():
                why = _unusable(kind, slot, aid, acts, stored=True)
                if why:
                    notes.append(f"{where}: {why}")
                elif acts[aid].single and i:
                    notes.append(_("{where}: {aid} can only be set on page 1; left out").format(where=where, aid=aid))
                else:
                    page.setdefault(cid, _blank(kind))[slot] = aid
                    if i == 0:
                        explicit.add(cid)
        if i == 0:
            # One control each for a single action. A default holder gives
            # way to the operator's choice (moving PTT to B1 clears K1); two
            # explicit holders keep the first.
            for aid in singles:
                holders = [cid for cid, b in page.items() if b.get("press") == aid]
                if len(holders) > 1:
                    chosen = [c for c in holders if c in explicit] or holders
                    keep = chosen[0]
                    if len([c for c in holders if c in explicit]) > 1:
                        notes.append(_("{aid} was on {where}; kept on {keep}").format(
                            aid=aid, where=", ".join(holders), keep=keep))
                    for c in holders:
                        if c != keep:
                            page[c]["press"] = "none"
        else:
            # Transmit and the page key are on the same control on every
            # page, whatever the page says.
            for cid, b in pages[0].items():
                held = b.get("press", "")
                if held in acts and acts[held].single:
                    if page[cid].get("press") != held:
                        notes.append(_("{cid} on page {n}: keeps {aid} on every page").format(cid=cid, n=i + 1, aid=held))
                    page[cid]["press"] = held
                    for c2, b2 in page.items():
                        if c2 != cid and b2.get("press") == held:
                            b2["press"] = "none"
        pages.append(page)
    if len(pages) > 1 and not any(b.get("press") == "page" for b in pages[0].values()):
        notes.append(_("Pages 2-{n} cannot be reached: no control is set to Next page").format(n=len(pages)))
    return pages, notes


def find_holder(page: dict, aid: str) -> str | None:
    return next((cid for cid, b in page.items() if b.get("press") == aid), None)


def tx_holders(page: dict, acts: dict[str, Action]) -> dict[str, str]:
    """{control: tx action} on a page."""
    return {cid: b["press"] for cid, b in page.items() if b.get("press") in acts and acts[b["press"]].tx}


def _guard(before: dict, after: dict, acts: dict[str, Action], on_air: bool) -> None:
    """While on air the transmit key stays exactly where it is: moving or
    removing it then would leave the operator pressing a key that no longer
    stops the carrier (the box's _on_air_guard)."""
    if on_air and tx_holders(before, acts) != tx_holders(after, acts):
        raise ValueError(_("Not while on air: the transmit key stays where it is until you are off air"))


def set_binding(drv: type[Driver], layout: dict | None, entry: dict | None, acts: dict[str, Action],
                page: int, cid: str, slot: str, aid: str, on_air: bool = False) -> dict:
    """One control's action on one page: the new entry. ValueError, before
    anything changes, for something the mapping could not use."""
    pages, _notes = resolve(drv, layout, entry, acts)
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < len(pages):
        raise ValueError(_("No such page"))
    kind = drv.control_kind(cid) if isinstance(cid, str) else None
    if not kind:
        raise ValueError(_("{cid} is not a control on the {model}").format(cid=cid, model=drv.name))
    if slot not in slots(kind):
        raise ValueError(_("A {kind} cannot {slot}").format(kind=kind, slot=slot))
    if aid not in acts:
        raise ValueError(_("Not an action this app offers"))
    a = acts[aid]
    if a.kind != slot:
        raise ValueError(_("{aid} is a {kind} action").format(aid=aid, kind=a.kind))
    if a.single and page:
        raise ValueError(_("{aid} can only be set on page 1; it is the same key on every page").format(aid=aid))
    held = pages[0].get(cid, {}).get(slot)
    if page and held in acts and acts[held].single and held != aid:
        raise ValueError(_("{cid} is {aid} on every page; change it on page 1").format(cid=cid, aid=held))
    over = [dict(p) for p in stored_pages(entry)]
    while len(over) < len(pages):
        over.append({})
    if a.single:
        # Moving it: wherever it was becomes nothing, explicitly, so the
        # default holder does not come back with it.
        for c, b in pages[0].items():
            if c != cid and b.get("press") == aid:
                over[0][c] = {**over[0].get(c, {}), "press": "none"}
    over[page][cid] = {**(over[page].get(cid) or {}), slot: aid}
    new = {**(entry or {}), "pages": over}
    _guard(pages[0], resolve(drv, layout, new, acts)[0][0], acts, on_air)
    return new


def set_pages(entry: dict | None, count: int) -> dict:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PAGES:
        raise ValueError(_("Between 1 and {n} pages").format(n=MAX_PAGES))
    over = [dict(p) for p in stored_pages(entry)][:count]
    while len(over) < count:
        over.append({})
    return {**(entry or {}), "pages": over}


def reset(drv: type[Driver], layout: dict | None, entry: dict | None, acts: dict[str, Action],
          on_air: bool = False) -> None:
    """Back to the app's own layout on one page: the entry goes (None), or
    ValueError if that would move the transmit key while on air."""
    _guard(resolve(drv, layout, entry, acts)[0][0], resolve(drv, layout, None, acts)[0][0], acts, on_air)
    return None
