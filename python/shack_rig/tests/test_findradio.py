"""Find my radio (findradio.py) against fake serial ports: an IC-9700 at
115200, an IC-705 left at 9600, an FTX-1, a port another program holds,
and a GPS that must only ever be read from; then the sound-device
matching with Ray's names (GM0OTB, 12/10 and 20/10/2026)."""

from __future__ import annotations

import time

from shack_rig import findradio
from shack_rig.icom import civ
from shack_rig.serialio import PortBusy


class Device:
    """What is on the far end of a port: answers bytes at one speed."""

    def __init__(self, kind="", baud=0, address=0, hz=145_825_000, yaesu_id=""):
        self.kind, self.baud, self.address, self.hz, self.yaesu_id = kind, baud, address, hz, yaesu_id
        self.heard: list[bytes] = []

    def answer(self, baud: int, data: bytes) -> bytes:
        self.heard.append(data)
        if baud != self.baud:
            return b"\x00\xff" if self.kind else b""            # line noise at the wrong speed
        if self.kind == "icom" and data.startswith(b"\xfe\xfe"):
            to, frm, payload = civ.parse(data)
            if to != self.address:
                return b""
            if payload == b"\x19\x00":
                return civ.frame(bytes([0x19, 0x00, self.address]), to=frm, frm=self.address)
            if payload[:1] == b"\x03":
                return civ.frame(b"\x03" + civ.encode_freq(self.hz), to=frm, frm=self.address)
        if self.kind == "yaesu" and data == b"ID;":
            return f"ID{self.yaesu_id};".encode()
        if self.kind == "kenwood":
            # A TM-V71 at its PC port speed (LA3QMA's list, CHIRP's ID).
            return {b"TY\r": b"TY K,0,0,1,0\r", b"ID\r": b"ID TM-V71\r",
                    b"FO 0\r": b"FO 0,0144800000,0,0,0,0,0,0,08,08,000,00600000,0\r"}.get(data, b"?\r")
        return b""


def opener_for(devices: dict, busy=()):
    class FakePort:
        def __init__(self, device, baud, stop_bits=1):
            self.device, self.baud = device, baud

        def open(self):
            if self.device in busy:
                raise PortBusy("held")

        def close(self):
            pass

        def exchange(self, data, wait):
            return devices[self.device].answer(self.baud, data)
    return FakePort


PORTS = [{"device": "COM3", "description": "Silicon Labs CP210x USB to UART Bridge (COM3)", "vid": 0x10C4},
         {"device": "COM4", "description": "Silicon Labs CP210x USB to UART Bridge (COM4)", "vid": 0x10C4},
         {"device": "COM5", "description": "Silicon Labs Dual CP2105 USB to UART Bridge: Enhanced COM Port (COM5)",
          "vid": 0x10C4},
         {"device": "COM6", "description": "u-blox GNSS receiver (COM6)", "vid": 0x1546},
         {"device": "COM7", "description": "USB Serial Device (COM7)", "vid": 0x10C4}]


def test_each_radio_is_found_with_its_port_speed_and_address_and_nothing_else_is_disturbed():
    gps = Device()
    devices = {"COM3": Device("icom", 9600, 0xA4), "COM4": Device("icom", 115200, 0xA2, hz=144_800_000),
               "COM5": Device("yaesu", 38400, yaesu_id="0840"), "COM6": gps, "COM7": Device()}
    s = findradio.scan(PORTS, opener=opener_for(devices, busy={"COM7"}), ask_s=0.0)
    got = {f.port: (f.kind, f.baud, f.civ_address) for f in s.found}
    assert got == {"COM3": ("ic-705", 9600, 0xA4), "COM4": ("ic-9700", 115200, 0xA2), "COM5": ("ftx-1", 38400, None)}
    nine = next(f for f in s.found if f.port == "COM4")
    assert nine.frequency_hz == 144_800_000 and "0xA2" in nine.evidence and "144.8000 MHz" in nine.evidence
    notes = {n.port: n.outcome for n in s.ports}
    assert notes == {"COM3": "radio", "COM4": "radio", "COM5": "radio", "COM6": "silent", "COM7": "in use"}
    # The GPS heard only reads: CI-V "who are you", Yaesu's "ID;", and the
    # Kenwood probes that no TNC acts on - never Kenwood's "ID\r".
    for data in gps.heard:
        assert data in (b"ID;", b"\x18\r", b"TY\r", b"FV 0\r") or civ.parse(data)[2] == b"\x19\x00"


def test_a_tmv71_is_found_on_its_pc_port_and_a_tnc_is_never_sent_id():
    tnc = Device()
    # A TNC-2 in command mode: answers every line with "?EH", as a Kenwood
    # answers nothing it knows - so it is never taken for one.
    tnc.answer = lambda baud, data, heard=tnc.heard: heard.append(data) or (b"?EH\r" if data.endswith(b"\r")
                                                                             else b"")
    devices = {"COM3": Device("kenwood", 57600), "COM6": tnc}
    s = findradio.scan([PORTS[0], PORTS[3]], opener=opener_for(devices), ask_s=0.0)
    (v71,) = s.found
    assert (v71.port, v71.kind, v71.name, v71.baud) == ("COM3", "tm-v71", "TM-V71", 57600)
    assert v71.frequency_hz == 144_800_000 and "TM-V71" in v71.evidence
    assert b"ID\r" not in tnc.heard, "ID sends a beacon on a TNC-2: only a port that answered TY like a Kenwood"
    assert tnc.heard.count(b"\x18\r") >= 1 and b"TY\r" in tnc.heard


def test_the_scan_is_bounded():
    class Slow:
        def __init__(self, device, baud, stop_bits=1):
            pass

        def open(self):
            pass

        def close(self):
            pass

        def exchange(self, data, wait):
            time.sleep(0.2)
            return b""
    t = time.monotonic()
    s = findradio.scan([{"device": f"/dev/ttyUSB{i}"} for i in range(6)], opener=Slow, deadline_s=1.0)
    assert time.monotonic() - t < 3.0 and not s.found
    assert all(n.outcome in ("silent", "skipped") for n in s.ports) and len(s.ports) == 6


RAY_INPUTS = ["Microphone (2- USB AUDIO  CODEC)", "IC9700 Microphone (USB Audio CODEC)", "Microphone (Realtek)"]
RAY_OUTPUTS = ["Speakers (2- USB AUDIO  CODEC)", "IC9700 Speakers (USB Audio CODEC)", "Speakers (Realtek)"]


def test_a_sound_device_named_after_the_radio_wins():
    f = findradio.Found(kind="ic-9700", name="IC-9700", port="COM4")
    findradio.match_audio(f, RAY_INPUTS, RAY_OUTPUTS, codec_of=lambda port, names: None)
    assert (f.audio_in, f.audio_out, f.audio_how) == ("IC9700 Microphone (USB Audio CODEC)",
                                                      "IC9700 Speakers (USB Audio CODEC)", "name")


def test_otherwise_the_codec_on_the_radios_usb_hub():
    f = findradio.Found(kind="ic-705", name="IC-705", port="COM3")
    asked = []

    def codec_of(port, names):
        asked.append((port, tuple(names)))
        return next(n for n in names if "2-" in n)
    findradio.match_audio(f, RAY_INPUTS, RAY_OUTPUTS, codec_of=codec_of)
    assert (f.audio_in, f.audio_out, f.audio_how) == ("Microphone (2- USB AUDIO  CODEC)",
                                                      "Speakers (2- USB AUDIO  CODEC)", "usb")
    assert all(port == "COM3" and all("CODEC" in n for n in names) for port, names in asked)


def test_and_when_nothing_can_say_it_asks_rather_than_guess():
    f = findradio.Found(kind="ic-705", name="IC-705", port="COM3")
    findradio.match_audio(f, RAY_INPUTS, RAY_OUTPUTS, codec_of=lambda port, names: None)
    assert (f.audio_in, f.audio_out, f.audio_how) == ("", "", "")


def test_a_tmv71_is_paired_with_the_interface_on_its_hub():
    """A TM-V71 has no codec of its own: the Digirig (a C-Media "USB Audio
    Device") on the same hub as its PC port is its sound, and the webcam
    microphone elsewhere is not."""
    f = findradio.Found(kind="tm-v71", name="TM-V71", port="/dev/ttyUSB0")
    names = ["Webcam Microphone", "USB Audio Device"]

    def hub(port, candidates):
        assert "Webcam Microphone" not in candidates, "only interface-like names are offered to the hub match"
        return "USB Audio Device" if port == "/dev/ttyUSB0" else None
    findradio.match_audio(f, names, ["USB Audio Device", "Speakers"], codec_of=hub)
    assert (f.audio_in, f.audio_out, f.audio_how) == ("USB Audio Device", "USB Audio Device", "usb")
