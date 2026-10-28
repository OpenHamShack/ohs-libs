"""shack_ui: server-driven screens for OpenHamShack clients (CLIENT-UI-PLAN.md §5).

An app describes a screen as a tree from the closed ui/1 vocabulary; the
OpenHamShack client draws it natively. `shack_ui.build` makes documents,
`shack_ui.validate` checks them against spec/schemas/ui.schema.json and
refuses, by name, anything that would let a generic action transmit
(§5.6): keying is only ever the native ptt/enableTx components.
"""

from __future__ import annotations

from .build import UI_VERSION, Action, Bind, Document, Node, native, node
from .validate import TransmitActionError, UIError, validate

__version__ = "0.2.0"

__all__ = ["UI_VERSION", "Action", "Bind", "Document", "Node", "TransmitActionError", "UIError", "native", "node",
           "validate"]
