"""The shared symbol sets and the client's icon names (SCREENS.md §3.2, §3.6).

    s.symbols(Symbols.load("aircraft"))              # every aircraft mark
    s.symbols(Symbols.load("aprs").only("car", "house", "default"))
    sym = Symbols.load("aprs").for_aprs("/>")        # "car"
    "ship" in icon_names()                           # the client's closed set

Symbols are vector marks the box sends in the document, so a new marker is
a box change and never a client release (decision D2). One set per kind of
thing on a map, so Ships, Aircraft, APRS and Sondes draw alike in every
app that shows them: spec/ui2/symbols/<set>.json in ohs-libs, converted
from the hand-drawn marks of today's client (each file says from where).
The package carries copies, because a wheel cannot reach spec/;
tools/gen-ui2-vectors.py refreshes them and a test fails when they differ.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from importlib import resources

SETS = ("aircraft", "aprs", "ships", "sondes", "station")


@cache
def _read(rel: str) -> dict:
    return json.loads(resources.files("shack_ui").joinpath("data", "ui2", rel).read_text(encoding="utf-8"))


class Symbols:
    """One symbol set, or a selection from one. `to_json()` is what goes in
    a document's `symbols`."""

    def __init__(self, symbols: dict, table: dict | None = None):
        self._symbols = symbols
        self.table = table or {}

    @classmethod
    def load(cls, name: str) -> Symbols:
        if name not in SETS:
            raise KeyError(f"no symbol set {name!r}; there are {', '.join(SETS)}")
        d = _read(f"symbols/{name}.json")
        return cls(copy.deepcopy(d["symbols"]), copy.deepcopy(d.get("table")))

    def names(self) -> list[str]:
        return list(self._symbols)

    def only(self, *names: str) -> Symbols:
        """The named symbols alone, for a screen that draws a few of a big
        set: a document carries only what it uses on a slow link."""
        missing = [n for n in names if n not in self._symbols]
        if missing:
            raise KeyError(f"no symbol {', '.join(missing)} in this set")
        return Symbols({n: self._symbols[n] for n in names}, self.table)

    def __add__(self, other: Symbols) -> Symbols:
        return Symbols(self._symbols | other._symbols, self.table | other.table)

    def for_aprs(self, code: str) -> str:
        """The symbol name for an APRS symbol (table character and code,
        e.g. "/>"), as today's client maps it; `default` for any other."""
        return self.table.get(code, "default")

    def to_json(self) -> dict:
        return copy.deepcopy(self._symbols)


def icon_names() -> frozenset[str]:
    """The client's closed icon set (spec/ui2/icons.json). A name outside
    it is drawn as the client's default icon (§2.3), so an app's tests
    should keep to these."""
    return frozenset(i["name"] for i in _read("icons.json")["icons"])
