"""Any USB keypad or keyboard as a controller: keys only, no screens.

Moved from the box (~/openhamshack/appliance/controllers/keypad.py). On Linux a
keypad is read from its /dev/input/event node (struct input_event, stdlib
only), and two things keep that safe on a machine whose screen has keyboard
focus:

- Only a device the operator has chosen is used. udev/91-openhamshack-pad.rules
  asks udev/shack-keypad (root, run by udev) whether the device is on the
  chosen list (/etc/openhamshack/keypads) and only then gives group
  shack-input the node and tells libinput to leave it alone.
- The device is grabbed (EVIOCGRAB, transport/evdev.py) while read, so its
  keys reach nothing else even if libinput had it open first.

A device is known by vendor:product:name rather than its event number (which
changes on every replug) or its serial (most of these have none).

Windows and macOS have no grab; there a desktop app turns its own window's
key events into the same events (model `keyboard`, Keyboard below).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from ..transport.evdev import EvdevTransport
from .base import Driver, Event, control

EV_KEY = 1
EVENT = struct.Struct("llHHi")     # struct input_event on 64-bit: timeval, type, code, value

# Linux input-event-codes.h: the keys a keypad, a macro pad or a keyboard
# sends. Macro pads usually send F13-F24, letters or media keys, and the
# knobs on some of them send volume up/down; all of those map like any key.
KEY_CODES = {
    1: "ESC", **{2 + i: str((i + 1) % 10) for i in range(10)}, 12: "MINUS", 13: "EQUAL", 14: "BACKSPACE",
    15: "TAB", **dict(zip(range(16, 26), "QWERTYUIOP")), 26: "LEFTBRACE", 27: "RIGHTBRACE", 28: "ENTER",
    29: "LEFTCTRL", **dict(zip(range(30, 39), "ASDFGHJKL")), 39: "SEMICOLON", 40: "APOSTROPHE",
    41: "GRAVE", 42: "LEFTSHIFT", 43: "BACKSLASH", **dict(zip(range(44, 51), "ZXCVBNM")), 51: "COMMA",
    52: "DOT", 53: "SLASH", 54: "RIGHTSHIFT", 55: "KPASTERISK", 56: "LEFTALT", 57: "SPACE", 58: "CAPSLOCK",
    **{59 + i: f"F{i + 1}" for i in range(10)}, 69: "NUMLOCK", 70: "SCROLLLOCK", 71: "KP7", 72: "KP8",
    73: "KP9", 74: "KPMINUS", 75: "KP4", 76: "KP5", 77: "KP6", 78: "KPPLUS", 79: "KP1", 80: "KP2",
    81: "KP3", 82: "KP0", 83: "KPDOT", 87: "F11", 88: "F12", 96: "KPENTER", 97: "RIGHTCTRL",
    98: "KPSLASH", 100: "RIGHTALT", 102: "HOME", 103: "UP", 104: "PAGEUP", 105: "LEFT", 106: "RIGHT",
    107: "END", 108: "DOWN", 109: "PAGEDOWN", 110: "INSERT", 111: "DELETE", 113: "MUTE",
    114: "VOLUMEDOWN", 115: "VOLUMEUP", 117: "KPEQUAL", 119: "PAUSE", 121: "KPCOMMA", 125: "LEFTMETA",
    126: "RIGHTMETA", 163: "NEXTSONG", 164: "PLAYPAUSE", 165: "PREVIOUSSONG", 166: "STOPCD",
    **{183 + i: f"F{i + 13}" for i in range(12)},
}
KEY_NAMES = set(KEY_CODES.values())
# Models another driver drives: their keyboard faces are never offered or
# opened as keypads (the AKP03E has a keyboard face of its own, input1).
SKIP_USB: set[tuple[str, str]] = {("0300", "3002")}


def decode(raw: bytes) -> list[Event]:
    """Events in a read from an event node. Auto-repeat (value 2) is not a
    press: holding a key must not step a preset over and over, nor arm and
    then key the transmitter by itself."""
    out = []
    for at in range(0, len(raw) - EVENT.size + 1, EVENT.size):
        _s, _us, etype, code, value = EVENT.unpack_from(raw, at)
        if etype != EV_KEY or code not in KEY_CODES or value not in (0, 1):
            continue
        out.append(("press" if value else "release", KEY_CODES[code], 0))
    return out


def device_id(node: Path) -> str | None:
    """vendor:product:name for an event node's sysfs directory - the same
    string udev/shack-keypad builds, byte for byte (name: anything outside
    A-Za-z0-9 space . _ - becomes _, at most 60 bytes)."""
    try:
        vendor = (node / "device" / "id" / "vendor").read_text().strip()
        product = (node / "device" / "id" / "product").read_text().strip()
        raw = (node / "device" / "name").read_bytes()
    except OSError:
        return None
    ok = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-\n"
    name = bytes(c if c in ok else ord("_") for c in raw)[:60].replace(b"\n", b"").decode()
    return f"{vendor}:{product}:{name}"


def _bits(text: str) -> int:
    """A sysfs capability bitmap (hex longs, most significant first)."""
    value = 0
    for word in text.split():
        value = (value << 64) | int(word, 16)
    return value


def candidates(sysfs: Path = Path("/sys"), skip_usb: set | None = None) -> list[dict]:
    """USB devices that send keyboard keys: what a settings screen offers to
    choose. Not a touchscreen or a mouse (absolute axes, or no keyboard
    keys), and not a model another driver drives."""
    out, seen = [], set()
    try:
        nodes = sorted((sysfs / "class" / "input").glob("event*"), key=lambda p: int(p.name[5:] or 0))
    except OSError:
        return out
    for node in nodes:
        dev = node / "device"
        try:
            if (dev / "id" / "bustype").read_text().strip() != "0003":
                continue
            if _bits((dev / "capabilities" / "abs").read_text()):
                continue
            keys = _bits((dev / "capabilities" / "key").read_text())
        except (OSError, ValueError):
            continue
        if not keys & ((1 << 128) - 2):          # no key in 1..127: not a keyboard
            continue
        did = device_id(node)
        if not did or did in seen:
            continue
        vendor, product = did.split(":")[:2]
        if (vendor, product) in (SKIP_USB if skip_usb is None else skip_usb):
            continue
        seen.add(did)
        out.append({"id": did, "name": did.split(":", 2)[2] or "USB keyboard", "node": f"/dev/input/{node.name}"})
    return out


class Keypad(Driver):
    model = "keypad"
    name = "USB keypad"
    open_layout = True
    # Drawn as a standard numeric keypad; any other key a macro pad sends is
    # a control too (open_layout).
    controls = [
        control("NUMLOCK", "key", 0, 0, "Num"), control("KPSLASH", "key", 0, 1, "/"),
        control("KPASTERISK", "key", 0, 2, "*"), control("KPMINUS", "key", 0, 3, "−"),
        control("KP7", "key", 1, 0, "7"), control("KP8", "key", 1, 1, "8"), control("KP9", "key", 1, 2, "9"),
        control("KPPLUS", "key", 1, 3, "+", h=2),
        control("KP4", "key", 2, 0, "4"), control("KP5", "key", 2, 1, "5"), control("KP6", "key", 2, 2, "6"),
        control("KP1", "key", 3, 0, "1"), control("KP2", "key", 3, 1, "2"), control("KP3", "key", 3, 2, "3"),
        control("KPENTER", "key", 3, 3, "Enter", h=2),
        control("KP0", "key", 4, 0, "0", w=2), control("KPDOT", "key", 4, 2, "."),
    ]
    # Transmit on Enter, the largest key and furthest from the others.
    tx_home = "KPENTER"

    @classmethod
    def has_control(cls, cid: str) -> bool:
        return cid in KEY_NAMES

    @classmethod
    def control_kind(cls, cid: str) -> str | None:
        return "key" if cid in KEY_NAMES else None

    @classmethod
    def find(cls, cfg: dict | None = None, sysfs: Path = Path("/sys"), dev: Path = Path("/dev/input")) -> list[str]:
        """The chosen devices (cfg["keypads"]: ids from candidates()) that are
        plugged in and that udev has let us read. One chosen but unreadable
        needs a replug: only root can ask udev to look again."""
        chosen = set((cfg or {}).get("keypads") or [])
        if not chosen:
            return []
        out = []
        for c in candidates(sysfs):
            path = str(dev / Path(c["node"]).name)
            if c["id"] in chosen and os.access(path, os.R_OK):
                out.append(path)
        return out

    @classmethod
    def open(cls, path: str) -> "Keypad":
        return cls(EvdevTransport(path))

    def decode(self, raw: bytes) -> list[Event]:
        return decode(raw)


class Keyboard(Keypad):
    """Keys from a desktop app's own window (Windows, macOS, or Linux without
    the udev rule): no device to open, so the app calls
    InputHost.handle("keyboard", ("press", "KPENTER", 0)) from its key
    handler, with the same key names. The host drops a second press of a
    key still held, which is how a window's auto-repeat is kept from ever
    pressing (CONTROLLER-PAD.md §7.3)."""

    model = "keyboard"
    name = "Keyboard"
    layout_from = "keypad"        # an app's keypad layout serves unless it ships a keyboard one

    @classmethod
    def find(cls, cfg: dict | None = None, **_kw) -> list[str]:
        return []
