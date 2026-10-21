"""Ajazz AKP03E: six keys with screens, three plain buttons, three knobs.

Moved from the box (~/openhamshack/appliance/controllers/ajazz.py) with the
protocol untouched. The protocol is undocumented; what is here was measured
on workshop's pad (USB 0300:3002, "HOTSPOTEKUSB HID DEMO") and agrees with
FlexDeck's map of the same model:
- The pad is silent until woken: DIS, LIG 0, CLE all, LIG <level>, STP.
- Commands are `CRT\\0\\0` + a three-letter verb, padded to the output report
  (1024 bytes on this unit), written after a zero report id.
- Input reports (512 bytes, no report id on hidraw) start `ACK\\0\\0OK`; byte
  9 names the control, byte 10 is 1 on press and 0 on release. A knob sends
  one report per detent, a code per direction, with no press byte.
- A key screen takes a 64x64 JPEG: header `BAT\\0\\0` + length (2 bytes, big
  endian) + key, the data in report-sized chunks, then STP. The screens are
  mounted turned: drawn upright, the picture must be turned a quarter
  clockwise - measured, the opposite quarter showed K1..K6 upside down.
- The pad wants to hear from its host: left alone it drops the connection
  and comes back reset, about 39 s after the last wake. Ray's AKP03E on
  Windows did exactly that from NB 0.1.20 (15/10/2026: "Controller
  disconnected (read error)" every 39.2 s while idle, a little longer
  while key faces were changing); each drop blanked and re-woke the pad,
  which the operator saw as every key flickering. So a `CRT\\0\\0CONNECT`
  goes out every KEEP_ALIVE seconds, as mirajazz's keep_alive() does
  (OpenDeck's AKP03 plugin sends it every 15 s). Ten seconds leaves three
  tries inside the timeout; it carries no picture and changes nothing on
  the keys.

What changed: the device is reached through a transport (hidraw on Linux,
hidapi elsewhere, FakeHid in tests) and faces are drawn with Pillow instead
of ffmpeg, which a Windows or macOS desktop does not have. The model no
longer carries a default map: each app ships its own (pad-layout.json); the
box's map lives on as the QO-100 WB app's (examples/qo100wb-pad-layout.json).

Other Mirabox/Ajazz models (AKP153, AKP815, the 293 family) speak the same
CRT/ACK framing with different report sizes, key codes and screen sizes;
each would be a subclass here with its own tables, once measured.
"""

from __future__ import annotations

import sys
import threading

from .. import faces
from ..transport import hidapi, hidraw
from .base import Driver, Event, control

VENDOR, PRODUCT = 0x0300, 0x3002
HID_ID = "0003:00000300:00003002"
REPORT = 1024
CRT = b"CRT\0\0"
KEEP_ALIVE = 10.0
ACK = b"ACK\0\0OK"

# Control codes (byte 9).
KEYS = {1: "K1", 2: "K2", 3: "K3", 4: "K4", 5: "K5", 6: "K6"}
BUTTONS = {0x25: "B1", 0x30: "B2", 0x31: "B3"}
KNOB_PRESS = {0x35: "D1", 0x33: "D2", 0x34: "D3"}
KNOB_TURN = {0x51: ("D1", +1), 0x50: ("D1", -1), 0x91: ("D2", +1), 0x90: ("D2", -1),
             0x61: ("D3", +1), 0x60: ("D3", -1)}


def decode(report: bytes) -> Event | None:
    """('press'|'release'|'turn', control, step) for an input report, or None."""
    if len(report) < 11 or report[:7] != ACK:
        return None
    code, state = report[9], report[10]
    if code in KNOB_TURN:
        name, step = KNOB_TURN[code]
        return "turn", name, step
    name = KEYS.get(code) or BUTTONS.get(code) or KNOB_PRESS.get(code)
    if not name:
        return None
    return ("press" if state else "release"), name, 0


def command(verb: bytes, *args: int) -> bytes:
    return (CRT + verb + bytes(args)).ljust(REPORT, b"\0")


def wake_sequence(brightness: int) -> list[bytes]:
    level = max(0, min(100, int(brightness)))
    return [command(b"DIS"), command(b"LIG", 0, 0, 0), command(b"CLE", 0, 0, 0, 0xFF),
            command(b"LIG", 0, 0, level), command(b"STP")]


def image_reports(key: int, jpeg: bytes) -> list[bytes]:
    out = [command(b"BAT", 0, 0, (len(jpeg) >> 8) & 0xFF, len(jpeg) & 0xFF, key)]
    out += [jpeg[at:at + REPORT].ljust(REPORT, b"\0") for at in range(0, len(jpeg), REPORT)]
    out.append(command(b"STP"))
    return out


class AKP03E(Driver):
    """The pad and its screens. Writes go through one lock: an image is
    several reports and must not interleave with another."""

    model = "ajazz-akp03e"
    name = "Ajazz AKP03E"
    usb = [(VENDOR, PRODUCT)]
    screen = (64, 64)
    # Two rows of screen keys, the three small buttons under them, the three
    # knobs along the bottom - K1 top left, numbered across, as the pad's own
    # codes run.
    controls = [
        control("K1", "screen-key", 0, 0), control("K2", "screen-key", 0, 1), control("K3", "screen-key", 0, 2),
        control("K4", "screen-key", 1, 0), control("K5", "screen-key", 1, 1), control("K6", "screen-key", 1, 2),
        control("B1", "button", 2, 0), control("B2", "button", 2, 1), control("B3", "button", 2, 2),
        control("D1", "knob", 3, 0), control("D2", "knob", 3, 1), control("D3", "knob", 3, 2),
    ]
    # Where transmit goes when an app's layout does not say: K1, the box's
    # choice, so every app agrees (CONTROLLER-PAD.md §4).
    tx_home = "K1"
    # Screens are mounted a quarter turn anticlockwise; draw upright, turn
    # clockwise (ffmpeg transpose=1 on the box).
    turn = "cw"
    keep_alive_every = KEEP_ALIVE

    @classmethod
    def find(cls, cfg: dict | None = None) -> list[str]:
        if sys.platform.startswith("linux"):
            return hidraw.find(HID_ID, "input0")
        return hidapi.find(VENDOR, PRODUCT, 0)

    @classmethod
    def open(cls, path: str) -> "AKP03E":
        if sys.platform.startswith("linux") and path.startswith("/dev/hidraw"):
            return cls(hidraw.HidrawTransport(path))
        return cls(hidapi.HidapiTransport(path))

    def __init__(self, transport):
        super().__init__(transport)
        self.lock = threading.Lock()
        self.shown: dict[int, bytes] = {}

    def decode(self, raw: bytes) -> list[Event]:
        event = decode(raw)
        return [event] if event else []

    def write(self, reports: list[bytes]) -> None:
        with self.lock:
            for r in reports:
                self.transport.write(b"\0" + r)

    def start(self, brightness: int) -> None:
        self.write(wake_sequence(brightness))
        self.shown.clear()

    def set_brightness(self, level: int) -> None:
        self.write([command(b"LIG", 0, 0, max(0, min(100, int(level)))), command(b"STP")])

    def keep_alive(self) -> None:
        self.write([command(b"CONNECT")])

    def show(self, cid: str, face) -> None:
        key = int(cid[1:]) if cid[:1] == "K" and cid[1:].isdigit() else 0
        if not 1 <= key <= 6:
            return
        jpeg = faces.render_jpeg(face, self.screen[0], self.turn)
        if jpeg and self.shown.get(key) != jpeg:   # only what changed: a key is ~2-3 kB of USB
            self.write(image_reports(key, jpeg))
            self.shown[key] = jpeg

    def blank(self) -> None:
        self.write([command(b"CLE", 0, 0, 0, 0xFF), command(b"STP")])
        self.shown.clear()
