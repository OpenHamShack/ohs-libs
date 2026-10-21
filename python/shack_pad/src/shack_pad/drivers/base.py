"""What every controller driver is.

Kept from the box (~/openhamshack/appliance/controllers/base.py) with one change:
a driver no longer opens its own file descriptor but is handed a transport
(transport/), so the same protocol code runs on hidraw, on hidapi and on
the test fake.

An event is (kind, control id, step): kind is 'press', 'release' or 'turn';
step is the detents turned (+ clockwise) and 0 otherwise. The tuple shape is
the one the AKP03E's decoder has always returned.
"""

from __future__ import annotations

# Kinds of control. A screen key is a key with a picture on it; a knob turns
# and is pressed; a button is a key with no picture (on the AKP03E, the three
# small round ones).
KINDS = ("screen-key", "key", "button", "knob")

Event = tuple[str, str, int]


def control(cid: str, kind: str, row: int, col: int, label: str = "", w: int = 1, h: int = 1) -> dict:
    """One control in a driver's layout. row/col are grid cells for a
    settings screen's drawing, w/h how many it spans (a keypad's Enter is
    two tall)."""
    assert kind in KINDS, kind
    return {"id": cid, "kind": kind, "row": row, "col": col, "w": w, "h": h, "label": label or cid}


def slots(kind: str) -> tuple[str, ...]:
    """What can be mapped on a control of this kind: everything presses,
    only a knob also turns."""
    return ("press", "turn") if kind == "knob" else ("press",)


class Driver:
    """A model of controller. Subclass, fill in the class attributes and
    implement decode (and show/set_brightness when it has screens)."""

    model = ""                     # stable id: maps are stored under it
    name = ""                      # what a settings screen calls it (a product name, not translated)
    usb: list[tuple[int, int]] = []   # (vendor, product)
    controls: list[dict] = []
    screen: tuple[int, int] | None = None   # a screen key's pixels, if it has any
    # Any key code the hardware may send counts as a control even when it is
    # not drawn (a generic keypad sends whatever its maker chose).
    open_layout = False
    # Seconds between keep-alives (keep_alive()), or None for a device that
    # needs none. InputHost.step sends them; see the AKP03E for why.
    keep_alive_every: float | None = None

    @classmethod
    def find(cls, cfg: dict | None = None) -> list[str]:
        """Device paths of this model plugged in and usable now."""
        return []

    @classmethod
    def open(cls, path: str) -> "Driver":
        """The driver on a real device: the transport this OS uses."""
        raise NotImplementedError

    @classmethod
    def has_control(cls, cid: str) -> bool:
        return any(c["id"] == cid for c in cls.controls)

    @classmethod
    def control_kind(cls, cid: str) -> str | None:
        for c in cls.controls:
            if c["id"] == cid:
                return c["kind"]
        return None

    @classmethod
    def screen_keys(cls) -> list[str]:
        return [c["id"] for c in cls.controls if c["kind"] == "screen-key"]

    @classmethod
    def describe(cls) -> dict:
        return {"model": cls.model, "name": cls.name, "controls": cls.controls,
                "screen": list(cls.screen) if cls.screen else None, "openLayout": cls.open_layout}

    # A device of this model ---------------------------------------------------------
    def __init__(self, transport):
        self.transport = transport
        self.path = getattr(transport, "path", "")

    def decode(self, raw: bytes) -> list[Event]:
        raise NotImplementedError

    def start(self, brightness: int) -> None:
        """Whatever the device needs before it sends events."""

    def read(self, timeout: float) -> list[Event]:
        """Events from the device, waiting at most timeout seconds. OSError
        when it has gone: the caller unkeys anything the pad keyed, then
        closes (CONTROLLER-PAD.md §7.2)."""
        raw = self.transport.read(timeout)
        return self.decode(raw) if raw else []

    def show(self, cid: str, face) -> None:
        """Draw a face (faces.Face) on a screen key."""

    def blank(self) -> None:
        """Every screen dark: on hand-over and on close, so a pad nobody owns
        never shows a stale ON AIR or a key that no longer does anything."""

    def set_brightness(self, level: int) -> None:
        pass

    def keep_alive(self) -> None:
        """Tell the device its host is still there (every keep_alive_every
        seconds). OSError when it has gone, as for any write."""

    def close(self) -> None:
        self.transport.close()
