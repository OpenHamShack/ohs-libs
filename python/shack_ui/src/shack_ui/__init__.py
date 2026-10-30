"""shack_ui: server-driven screens, ui/2 (openhamshack docs/contract/SCREENS.md).

An app describes each screen as a document of generic parts with data
(collections, templates, typed values), keeps it current over a live
socket of patch ops, and declares what it holds on the box while the
screen is open. The OpenHamShack client draws it natively, so a new screen,
map layer or symbol ships with the box app and needs no client release.

    build.py   Screen, the part builders `p`, actions `A`, typed values `T`,
               item(), local(), and the downgrade for older clients
    serve.py   Req (what the client is) and serve_document (ETag, 304, 406)
    live.py    LiveHub: channels, coalesced frames at maxHz, snaps, extras
    check.py   the validator: the rules every renderer also enforces
    ops.py     the reference op applier: what a renderer holds
    symbols.py the shared symbol sets and the client's icon names
    schema.py  ui2.schema.json, packaged, and schema_errors()
"""

from __future__ import annotations

from .build import (UI_MINOR, UI_VERSION, A, Collection, Screen, T, TransmitActionError, UIError, descriptor,
                    downgrade, entry, item, local, p, part, rev, status)
from .check import Problem, check, check_action, check_path, check_typed, iter_parts
from .live import Channel, LiveHub, Session
from .ops import apply_frame, apply_ops, drawn_keys, public_state, state_from_document
from .schema import schema, schema_errors
from .serve import Req, Response, Unsupported, serve_document, serve_json
from .symbols import Symbols, icon_names

__version__ = "0.3.0.dev1"

__all__ = ["A", "Channel", "Collection", "LiveHub", "Problem", "Req", "Response", "Screen", "Session", "Symbols", "T",
           "TransmitActionError", "UIError", "UI_MINOR", "UI_VERSION", "Unsupported", "apply_frame", "apply_ops",
           "check", "check_action", "check_path", "check_typed", "descriptor", "downgrade", "drawn_keys", "entry",
           "icon_names", "item", "iter_parts", "local", "p", "part", "public_state", "rev", "schema", "schema_errors", "serve_document",
           "serve_json", "state_from_document", "status"]
