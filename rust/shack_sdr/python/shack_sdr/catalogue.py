"""One catalogue of every decoder an app can offer, whichever library
decodes it (RADIO-SDR-PLAN §4.2).

The Rust decoders describe themselves (`_native.catalogue()`). The decoders
that are already Python and work keep running where they run today - CW in
shack_cw (the skimmer in its own child process), FT8, FT4, Q65 and JS8 in
ohsdigi's slot decoder driven by the engine - and are listed here with the
same fields, `engine` saying which library decodes. Brian, 22/10/2026: CW
is ported to Rust later (M5) and shack_cw stays live until the port beats
it, so this list is how both live side by side.

`fixed` says whether `Host.start` can run the decoder as a fixed session
(one independent of the dial). CW can, through shack_cw (adapters.py). The
slot modes are mode decoders only in M1: the engine schedules their slots.
An app can give one a session runner with `register`.

`available` and `why` say whether this install can decode it now: a code,
`not_installed`, when the library is not there.
"""

from __future__ import annotations

import copy
import importlib.util
from typing import Callable

from . import _native

#: Entries for the Python-backed decoders, in the native entries' shape.
_PYTHON = [
    {"id": "cw", "family": "amateur", "inputs": ["audio"], "rateHz": 12_000.0, "bandwidthHz": 500.0,
     "outputs": ["text"], "panel": "text",
     "params": [{"key": "pitchHz", "type": "number", "min": 200.0, "max": 3000.0, "default": 700.0}],
     "needs": [], "engine": "shack_cw"},
] + [
    {"id": mode, "family": "amateur", "inputs": ["audio"], "rateHz": 12_000.0, "bandwidthHz": 4_000.0,
     "outputs": ["text"], "panel": "slots", "params": [], "needs": [], "engine": "ohsdigi"}
    for mode in ("ft8", "ft4", "q65", "js8")
]

#: Session runners for Python-backed decoders: id -> factory(params, rate, center_hz) -> PythonSession.
_RUNNERS: dict[str, Callable] = {}


def _module_version(*names: str) -> tuple[bool, str | None]:
    for name in names:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            try:
                mod = importlib.import_module(name)
            except Exception:   # noqa: BLE001 - present but broken is "not installed" for the catalogue
                continue
            return True, getattr(mod, "__version__", None)
    return False, None


def _engine_modules(engine: str) -> tuple[str, ...]:
    # ohsdigi is a wheel of its own (ohs-ohsdigi) or, in NB's checkout and
    # bundles, the module ohscore.ohsdigi.
    return {"shack_cw": ("shack_cw",), "ohsdigi": ("ohsdigi", "ohscore.ohsdigi"),
            "shack_packet": ("shack_packet",)}.get(engine, (engine,))


def register(decoder_id: str, factory: Callable) -> None:
    """Give a Python-backed decoder a session runner, so Host.start can run
    it as a fixed session: `factory(params, rate, center_hz)` returns an
    object with feed(samples, rate, t0, index), poll() -> [result dict],
    status() -> dict, reset() and close() (adapters.PythonSession)."""
    if not any(e["id"] == decoder_id for e in _PYTHON):
        raise _native.DecoderError("unknown_decoder", decoder_id)
    _RUNNERS[decoder_id] = factory


def runner(decoder_id: str) -> Callable | None:
    if decoder_id not in _RUNNERS and decoder_id == "cw":
        from .adapters import cw_session
        _RUNNERS["cw"] = cw_session
    return _RUNNERS.get(decoder_id)


def catalogue() -> list[dict]:
    """Every decoder, Rust first, each with `available`, `why`, `version`
    and `fixed`."""
    out = []
    for e in _native.catalogue():
        e.update({"available": True, "why": None, "fixed": True})
        out.append(e)
    for e in _PYTHON:
        e = copy.deepcopy(e)
        ok, version = _module_version(*_engine_modules(e["engine"]))
        e["available"] = ok
        e["why"] = None if ok else "not_installed"
        e["version"] = version
        e["fixed"] = ok and runner(e["id"]) is not None
        out.append(e)
    return out


def entry(decoder_id: str) -> dict | None:
    return next((e for e in catalogue() if e["id"] == decoder_id), None)
