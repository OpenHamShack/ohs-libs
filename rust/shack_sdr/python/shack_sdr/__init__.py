"""shack_sdr: OpenHamShack's SDR decoders, the host that runs them, the
runtime codec loader and the receive-only IQ sources (openhamshack
docs/RADIO-SDR-PLAN.md §4.2).

    from shack_sdr import Host, catalogue, codecs

    host = Host(threads=1, cpu_max=0.25)
    catalogue()          # [{"id", "family", "inputs", "rateHz", "bandwidthHz", "outputs", "panel",
                         #   "params", "needs", "engine", "available", "why", "version", "fixed"}, ...]
    sid = host.start("rtty", params={"baud": 45.45, "shift": 170, "reverse": False}, input="iq", rate=12_000)
    host.feed(sid, samples, t0, index)      # numpy complex64 or float32, read in place; never blocks
    host.poll(max_items=500)                # [{"session", "rev", "t", "type", ...}, ...]; never blocks
    host.status(sid) / host.stats()         # state, why (a code), counters, cpu share
    host.reset(sid); host.stop(sid); host.close()
    codecs.status(rescan=False)             # each codec found, missing or refused, with a code

The decoders are Rust (shack_sdr._native) and run on the host's own
threads with the GIL released; Python polls for their results. Decoders
that are already Python and work (shack_cw's CW, ohsdigi's slot modes)
appear in the same catalogue, marked with the library that decodes them
(`engine`), so an app and its clients see one list (catalogue.py).

Receive only by construction: nothing in this package has a transmit
method, and it imports no radio link or rig library (a test checks).
Every refusal is `DecoderError(code, detail)`: codes, never sentences; the
app words them from its catalog.
"""

from __future__ import annotations

from ._native import DecoderError, __version__
from . import codecs
from .catalogue import catalogue, register
from .host import Host

__all__ = ["DecoderError", "Host", "__version__", "catalogue", "codecs", "register"]
