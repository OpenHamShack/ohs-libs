"""Loading the band-plan dataset (BANDPLAN-DATA.md §2).

The dataset is JSON files under data/bandplan/ in ohs-libs. A wheel
carries a copy inside the package (`shack_bandplan/data/bandplan`, put
there at build time by setup.py); a source checkout reads the repository's
copy directly, so a test run and an installed app read the same bytes and
there is never a second copy in git to drift from the first.

`load()` refuses a dataset whose schema it does not know (§2: "a reader
refuses a schema it does not know and keeps its bundled data") and one
whose files do not match the manifest's checksums: a half-edited or
half-copied dataset must fail loudly at start-up, not answer "may I
transmit" from a mixture of two versions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1

_HERE = Path(__file__).resolve().parent
_BUNDLED = _HERE / "data" / "bandplan"
# src/shack_bandplan -> python/shack_bandplan/src -> python/shack_bandplan -> python -> repo root
_REPO = _HERE.parents[3] / "data" / "bandplan"


class DataError(Exception):
    """The dataset is unusable: unknown schema, a missing file, or a
    checksum that does not match the manifest."""


def default_path() -> Path:
    for p in (_BUNDLED, _REPO):
        if (p / "manifest.json").is_file():
            return p
    raise DataError("No band-plan dataset found: the shack-bandplan package is missing its data")


@dataclass
class Dataset:
    path: Path
    manifest: dict
    vocab: dict
    countries: list
    regions: dict
    regimes: dict
    plans: dict
    satellites: dict
    repeaters: dict
    presets: list
    by_iso: dict = field(default_factory=dict)
    by_entity: dict = field(default_factory=dict)

    @property
    def data_version(self) -> str:
        return self.manifest["dataVersion"]

    @property
    def cty_path(self) -> Path:
        return self.path / "cty" / "cty.dat"

    def mode_class(self, mode: str | None) -> str | None:
        if not mode:
            return None
        m = mode.lower()
        if m in self.vocab["modeClasses"]:
            return m
        return self.vocab["modeClass"].get(m)


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DataError(f"The band-plan dataset is missing {p.name}") from None
    except json.JSONDecodeError as e:
        raise DataError(f"{p.name} in the band-plan dataset is not valid JSON ({e})") from None


def load(path: str | Path | None = None, verify: bool = True) -> Dataset:
    """The dataset at `path` (default: the bundled one). Pure after this:
    nothing else in the library reads a file except cty.dat, once."""
    root = Path(path) if path is not None else default_path()
    manifest = _read(root / "manifest.json")
    if manifest.get("schema") != SCHEMA:
        raise DataError(f"The band-plan dataset has schema {manifest.get('schema')!r}; this library reads "
                        f"schema {SCHEMA} only")
    if verify:
        for rel, digest in manifest.get("files", {}).items():
            p = root / rel
            try:
                got = hashlib.sha256(p.read_bytes()).hexdigest()
            except FileNotFoundError:
                raise DataError(f"The band-plan dataset is missing {rel}") from None
            if got != digest:
                raise DataError(f"{rel} does not match the band-plan manifest: the dataset was changed without "
                                f"regenerating manifest.json")
    regimes = {rid: _read(root / "regimes" / f"{rid}.json") for rid in manifest["regimes"]}
    plans = {pid: _read(root / "plans" / f"{pid}.json") for pid in manifest["plans"]}
    sats = {sid: _read(root / "satellite" / f"{sid}.json") for sid in manifest["satellites"]}
    for kind, items in (("regime", regimes), ("plan", plans), ("satellite", sats)):
        for k, v in items.items():
            if v.get("schema") != SCHEMA:
                raise DataError(f"The {kind} {k} has schema {v.get('schema')!r}; this library reads schema {SCHEMA}")
    countries = _read(root / "countries.json")
    ds = Dataset(root, manifest, _read(root / "vocab.json"), countries, _read(root / "regions.json"), regimes,
                 plans, sats, _read(root / "repeaters.json"), _read(root / "presets.json"))
    ds.by_iso = {c["iso"]: c for c in countries}
    ds.by_entity = {e: c for c in countries for e in c["entities"]}
    return ds


_cache: dict[str, Dataset] = {}


def bundled() -> Dataset:
    """The bundled dataset, loaded once per process."""
    key = str(default_path())
    if key not in _cache:
        _cache[key] = load()
    return _cache[key]
