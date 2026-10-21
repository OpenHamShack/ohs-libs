"""shack_pad: the OpenHamShack controller library (CONTROLLER-PAD.md).

One library for the Ajazz AKP03E and USB keypads in every OpenHamShack app:
drivers and transports, key faces, action declarations, the operator's
remaps, routing to the app in front, desktop focus hand-over, and the pad
as a client of the app's transmit lease. docs/PAD.md is the five-minute
guide for an app adopting it.
"""

from __future__ import annotations

from .actions import Action, DeclarationError, load as load_actions, validate as validate_actions
from .faces import Face
from .focus import DesktopPad, PadFocus
from .router import InProcessApp, InputHost, PadAction, PadApp
from .tx import LocalLease, PadTx, TxLease

__version__ = "0.1.0"

__all__ = ["Action", "DeclarationError", "DesktopPad", "Face", "InProcessApp", "InputHost", "LocalLease",
           "PadAction", "PadApp", "PadFocus", "PadTx", "TxLease", "load_actions", "validate_actions"]
