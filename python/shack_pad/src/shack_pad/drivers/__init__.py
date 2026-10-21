"""Every model of controller the library drives.

A driver knows one model and nothing about OpenHamShack apps: its controls
and where they sit, how to find it, its raw reports turned into events, and
drawing a face on a screen key. Which action a control runs is mapping.py's
business and what an action does is the app's, so a new model never touches
the transmit logic.
"""

from __future__ import annotations

from .ajazz import AKP03E
from .base import Driver
from .keypad import Keyboard, Keypad

DRIVERS: list[type[Driver]] = [AKP03E, Keypad]
MODELS: dict[str, type[Driver]] = {d.model: d for d in (AKP03E, Keypad, Keyboard)}


def by_model(model: str) -> type[Driver] | None:
    return MODELS.get(model)


__all__ = ["AKP03E", "DRIVERS", "Driver", "Keyboard", "Keypad", "MODELS", "by_model"]
