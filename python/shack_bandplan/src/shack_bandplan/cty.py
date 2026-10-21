"""Which DXCC entity a callsign is in, from cty.dat: a port of openhamshack-nb
ohscore/dxcc.py and ohscore/callsign.py, reduced to what the guard needs
(where the station is, and so whose licence applies).

Why ported rather than imported: shack_bandplan is stdlib-only and shared
by every app, and ohscore is one app family's engine. The rules are the
same and so are the answers: EA8/GM4BRI is in the Canary Islands, so
under Spain's rules; GM4BRI/MM is at sea, in no country.

cty.dat is AD1C's country file (data/bandplan/cty/, with its copyright
file). Format: https://www.country-files.com/cty-dat-format/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entity:
    name: str
    continent: str
    prefix: str
    wae: bool = False
    lat: float | None = None
    lon: float | None = None      # degrees east (cty.dat's are west-positive)


_OVERRIDES = re.compile(r"\(.*?\)|\[.*?\]|<.*?>|\{.*?\}|~.*?~")
# Operating suffixes, not places; /MM and /AM are in no country at all.
_NOT_A_PLACE = {"P", "M", "QRP", "QRPP", "A", "LH", "LGT"}
_NOWHERE = {"MM", "AM"}
# A home call: letters and digits, a digit within the first five, ending in
# a letter (which tells a call from a bare prefix such as EA8).
_HOME = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{0,4}[0-9][A-Z0-9]{0,5}[A-Z]$")
_PART = re.compile(r"^[A-Z0-9]{1,6}$")


def _f(s: str, sign: float = 1.0) -> float | None:
    try:
        return sign * float(s)
    except ValueError:
        return None


def normalise(call) -> str:
    return str(call or "").strip().upper().strip("<>")


def valid(call) -> bool:
    """A callsign, home or portable (G4ABC, EA8/GM4BRI/P, W1AW/4, GM4BRI/MM)."""
    c = normalise(call)
    if not c or len(c) > 20 or c.startswith("/") or c.endswith("/") or "//" in c:
        return False
    bits = c.split("/")
    if len(bits) > 4:
        return False
    homes = [i for i, b in enumerate(bits) if 3 <= len(b) <= 10 and _HOME.match(b)]
    if not homes or not all(_PART.match(b) or i in homes for i, b in enumerate(bits)):
        return False
    i = max(homes, key=lambda j: (len(bits[j]), j))
    return i <= 1 and len(bits) - i - 1 <= 2


def location_part(call: str) -> str | None:
    """The part of a call that says where the station is; None at sea or
    in the air. EA8/G4ABC -> EA8, G4ABC/P -> G4ABC, EA1ABC/8 -> EA8."""
    parts = [p for p in normalise(call).split("/") if p]
    if not parts:
        return ""
    if any(p in _NOWHERE for p in parts[1:]):
        return None
    parts = [parts[0]] + [p for p in parts[1:] if p not in _NOT_A_PLACE]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 1:
        m = re.match(r"^([A-Z0-9]*?[A-Z])(\d)", parts[0])
        return (m.group(1) if m else parts[0]) + parts[1]
    return min(parts[:2], key=len)


class Countries:
    def __init__(self, text: str):
        self.exact: dict[str, Entity] = {}
        self.prefixes: dict[str, Entity] = {}
        self.by_prefix: dict[str, Entity] = {}
        for record in text.split(";"):
            record = record.strip()
            if not record:
                continue
            head, _sep, body = record.partition("\n")
            f = [x.strip() for x in head.split(":")]
            if len(f) < 8:
                continue
            ent = Entity(f[0], f[3], f[7].lstrip("*"), f[7].startswith("*"), _f(f[4]), _f(f[5], -1.0))
            self.by_prefix.setdefault(ent.prefix, ent)
            for alias in body.replace("\n", "").split(","):
                alias = _OVERRIDES.sub("", alias).strip().upper()
                if not alias:
                    continue
                if alias.startswith("="):
                    self.exact[alias[1:]] = ent
                else:
                    self.prefixes[alias] = ent
        self.longest = max((len(p) for p in self.prefixes), default=0)

    def lookup(self, call: str) -> Entity | None:
        call = normalise(call)
        if not call:
            return None
        if call in self.exact:
            return self.exact[call]
        where = location_part(call)
        if not where:
            return None
        if where in self.exact:
            return self.exact[where]
        for n in range(min(len(where), self.longest), 0, -1):
            ent = self.prefixes.get(where[:n])
            if ent is not None:
                return ent
        return None


_cache: dict[str, Countries] = {}


def load(path: Path) -> Countries:
    key = str(path)
    if key not in _cache:
        _cache[key] = Countries(Path(path).read_text(encoding="latin-1"))
    return _cache[key]
