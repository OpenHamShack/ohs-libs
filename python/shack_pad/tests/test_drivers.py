"""The drivers against FakeHid: decoding as captured, the wake sequence,
key-image framing, only changed keys written, finding the vendor interface,
keypads and the udev helper. Ported from ~/openhamshack/appliance/test_pad.py and
test_controllers.py, now through the transport layer."""

import json
import subprocess
from pathlib import Path

import pytest

from shack_pad import faces
from shack_pad.drivers import ajazz, keypad
from shack_pad.drivers.ajazz import AKP03E
from shack_pad.drivers.keypad import Keypad
from shack_pad.faces import Face
from shack_pad.fake import FakeHid, akp03e_report, evdev_events
from shack_pad.transport import hidraw

# The captured reports are spec, not this package's: every language's
# driver runs the same file (REPO-LAYOUT.md §2 spec/vectors/pad/).
VECTORS = json.loads((Path(__file__).resolve().parents[3] / "spec" / "vectors" / "pad" / "akp03e-reports.json")
                     .read_text())
UDEV = Path(__file__).resolve().parents[1] / "src" / "shack_pad" / "udev"


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: f"code{c['code']}-{c['state']}")
def test_captured_reports_read_through_the_driver(case):
    dev = AKP03E(FakeHid([akp03e_report(case["code"], case["state"], VECTORS["report_size"])]))
    want = [tuple(case["event"])] if case["event"] else []
    assert dev.read(0) == want


def test_noise_and_a_quiet_pad_give_nothing():
    dev = AKP03E(FakeHid([b"\0" * 512, b"ACK"]))
    assert dev.read(0) == [] and dev.read(0) == [] and dev.read(0) == []


def test_unplugged_reads_raise_oserror():
    fake = FakeHid()
    dev = AKP03E(fake)
    fake.unplug()
    with pytest.raises(OSError):
        dev.read(0.01)


def test_wake_sequence_is_written_after_a_zero_report_id():
    fake = FakeHid()
    AKP03E(fake).start(60)
    assert [w[0] for w in fake.writes] == [0] * 5
    reports = [w[1:] for w in fake.writes]
    assert all(len(r) == 1024 for r in reports)
    assert [r[:5] for r in reports] == [b"CRT\0\0"] * 5
    assert [r[5:8] for r in reports] == [b"DIS", b"LIG", b"CLE", b"LIG", b"STP"]
    assert reports[2][8:12] == b"\0\0\0\xff"        # clear all keys
    assert reports[3][10] == 60                      # brightness


def test_image_framing_header_chunks_commit():
    jpeg = bytes(range(256)) * 9                     # 2304 bytes: three chunks
    reps = ajazz.image_reports(4, jpeg)
    assert reps[0][:8] == b"CRT\0\0BAT"
    assert reps[0][8:13] == bytes([0, 0, 0x09, 0x00, 4])   # length big-endian, then key
    assert b"".join(reps[1:-1])[:len(jpeg)] == jpeg
    assert len(reps) == 1 + 3 + 1
    assert reps[-1][:8] == b"CRT\0\0STP"


def test_a_face_reaches_the_key_as_a_turned_jpeg_and_only_once():
    pytest.importorskip("PIL")
    fake = FakeHid()
    dev = AKP03E(fake)
    face = Face("ON AIR", "", "onair")
    dev.show("K3", face)
    first = list(fake.writes)
    header = first[0][1:]
    assert header[:8] == b"CRT\0\0BAT" and header[12] == 3
    size = (header[10] << 8) | header[11]
    jpeg = b"".join(w[1:] for w in first[1:-1])[:size]
    assert jpeg[:2] == b"\xff\xd8" and first[-1][1:9] == b"CRT\0\0STP"
    assert jpeg == faces.render_jpeg(face, 64, "cw")
    # Measured on workshop's pad: drawn upright, the picture must be turned
    # a quarter clockwise. Compare the key's JPEG with the upright drawing
    # turned each way; JPEG is lossy, so "close" rather than equal.
    from io import BytesIO

    from PIL import Image, ImageChops, ImageStat
    two_lines = Face("TUNE", "Hz")
    dev.show("K2", two_lines)
    hdr = fake.writes[len(first)][1:]
    sent = b"".join(w[1:] for w in fake.writes[len(first) + 1:-1])[:(hdr[10] << 8) | hdr[11]]
    got = Image.open(BytesIO(sent)).convert("RGB")
    upright = faces.render(two_lines)

    def diff(img):
        return sum(ImageStat.Stat(ImageChops.difference(got, img)).mean)
    assert diff(upright.transpose(Image.Transpose.ROTATE_270)) < 10
    assert diff(upright.transpose(Image.Transpose.ROTATE_90)) > 3 * diff(upright.transpose(Image.Transpose.ROTATE_270))
    first = list(fake.writes)
    # Unchanged: nothing more is written. Changed: written again.
    dev.show("K3", face)
    assert len(fake.writes) == len(first)
    dev.show("K3", Face("TX", "", "normal"))
    assert len(fake.writes) > len(first)


def test_buttons_and_knobs_have_no_screen_to_draw():
    fake = FakeHid()
    dev = AKP03E(fake)
    dev.show("B1", Face("X"))
    dev.show("D2", Face("X"))
    assert fake.writes == []


def test_blank_clears_every_key_and_forgets_what_was_shown():
    fake = FakeHid()
    dev = AKP03E(fake)
    dev.shown[1] = b"x"
    dev.blank()
    assert [w[6:9] for w in fake.writes] == [b"CLE", b"STP"] and dev.shown == {}


def test_finds_the_vendor_interface_not_the_keyboard_face(tmp_path):
    for name, phys in (("hidraw3", "input0"), ("hidraw4", "input1")):
        d = tmp_path / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(f"HID_ID=0003:00000300:00003002\nHID_PHYS=usb-0000:00:14.0-5/{phys}\n")
    assert hidraw.find(ajazz.HID_ID, "input0", tmp_path) == ["/dev/hidraw3"]


def test_hidapi_finds_interface_zero(monkeypatch):
    import sys
    import types
    from shack_pad.transport import hidapi
    fake_hid = types.SimpleNamespace(enumerate=lambda v, p: [
        {"path": b"/dev/hidraw9", "interface_number": 1, "usage_page": 1, "usage": 6},
        {"path": b"/dev/hidraw8", "interface_number": 0, "usage_page": 0xFFA0, "usage": 1}])
    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    assert hidapi.find(ajazz.VENDOR, ajazz.PRODUCT) == ["/dev/hidraw8"]


# --- Keypads ---------------------------------------------------------------------------
def test_keypad_decodes_presses_and_never_auto_repeat():
    raw = evdev_events((1, 96, 1), (1, 96, 2), (1, 96, 2), (1, 96, 0), (4, 4, 0x70058), (1, 78, 1))
    assert Keypad(FakeHid([raw])).read(0) == [("press", "KPENTER", 0), ("release", "KPENTER", 0),
                                            ("press", "KPPLUS", 0)]


def _fake_input(root: Path, n: int, vendor: str, product: str, name: bytes, key_bits: str = "1 fffffffe",
                abs_bits: str = "0") -> Path:
    node = root / "class" / "input" / f"event{n}"
    dev = node / "device"
    (dev / "id").mkdir(parents=True)
    (dev / "capabilities").mkdir()
    (dev / "id" / "bustype").write_text("0003\n")
    (dev / "id" / "vendor").write_text(vendor + "\n")
    (dev / "id" / "product").write_text(product + "\n")
    (dev / "name").write_bytes(name + b"\n")
    (dev / "capabilities" / "key").write_text(key_bits + "\n")
    (dev / "capabilities" / "abs").write_text(abs_bits + "\n")
    return node


def test_only_usb_keyboards_are_offered_and_never_the_pads_keyboard_face(tmp_path):
    _fake_input(tmp_path, 3, "1a2c", "0e24", b"SEM USB Keyboard")
    _fake_input(tmp_path, 4, "0eef", "c000", b"Touch", abs_bits="3")
    _fake_input(tmp_path, 5, "0300", "3002", b"HOTSPOTEKUSB HID DEMO")
    assert [c["id"] for c in keypad.candidates(tmp_path)] == ["1a2c:0e24:SEM USB Keyboard"]


def test_the_udev_helper_agrees_with_the_driver_byte_for_byte(tmp_path):
    node = _fake_input(tmp_path, 7, "05ac", "0256", b"Odd: n\xc3\xa4me/with*stuff")
    did = keypad.device_id(node)
    chosen = tmp_path / "keypads"
    chosen.write_text("someone:else:kbd\n")
    helper = [str(UDEV / "shack-keypad"), "event7", str(tmp_path), str(chosen)]
    assert subprocess.run(helper).returncode == 1
    chosen.write_text(did + "\n")
    assert subprocess.run(helper).returncode == 0
    assert subprocess.run([str(UDEV / "shack-keypad"), "../event7", str(tmp_path), str(chosen)]).returncode == 1


def test_find_takes_only_chosen_readable_keypads(tmp_path):
    _fake_input(tmp_path, 3, "1a2c", "0e24", b"SEM USB Keyboard")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "event3").write_bytes(b"")
    assert Keypad.find({}, tmp_path, dev) == []
    assert Keypad.find({"keypads": ["1a2c:0e24:SEM USB Keyboard"]}, tmp_path, dev) == [str(dev / "event3")]
