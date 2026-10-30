"""shack_ui: server-driven screens, ui/2 (openhamshack docs/contract/SCREENS.md).

An app describes each screen as a document of generic parts with data
(collections, templates, typed values), keeps it current over a live
socket of patch ops, and declares what it holds on the box while the
screen is open. The OpenHamShack client draws it natively, so a new screen,
map layer or symbol ships with the box app and needs no client release.

    check.py   the validator: the rules every renderer also enforces
    ops.py     the reference op applier: what a renderer holds
"""

from __future__ import annotations

from .check import Problem, check, check_action, check_path, check_typed, iter_parts
from .ops import apply_frame, apply_ops, drawn_keys, public_state, state_from_document

__version__ = "0.2.0"

__all__ = ["Problem", "apply_frame", "apply_ops", "check", "check_action", "check_path", "check_typed",
           "drawn_keys", "iter_parts", "public_state", "state_from_document"]
