"""shack_bandplan: the shared band-plan and licence dataset and the question
every OpenHamShack app asks before keying, "may I transmit this?"
(BANDPLAN-DATA.md §6.1).

    import shack_bandplan as bp
    data = bp.load()
    ctx = bp.context(profile, data, today="2026-10-09")
    verdict = bp.check(ctx, bp.Signal(centre_hz=145_500_000, occupied_bw_hz=12_000, mode="fm", power_w=50))

Pure functions over data loaded once; stdlib only.
"""
from __future__ import annotations

from .check import (OCCUPIED_FACTOR, Context, Signal, band_of, bands, channels, check, context,
                    dvb_occupied_hz, in_force, limits, presets)
from .data import DataError, Dataset, bundled, load
from .messages import translate

__all__ = ["OCCUPIED_FACTOR", "Context", "DataError", "Dataset", "Signal", "band_of", "bands", "bundled",
           "channels", "check", "context", "dvb_occupied_hz", "in_force", "limits", "load", "presets",
           "translate"]
__version__ = "0.1.0"
