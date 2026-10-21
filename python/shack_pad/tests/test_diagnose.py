"""Finding the pad on Windows and macOS, telling a remote operator what is
wrong, and the test mode a settings page runs.

Nobody has an AKP03E on a Windows PC here, so the hidapi enumerations
below are shaped as hidapi's windows/hid.c builds them: one entry per
top-level collection, the path carrying MI_xx (the USB interface) and
&colNN when an interface has several collections; interface_number parsed
from MI_xx; usage page and usage from the collection. The vendor
collection's usage (0xFFA0, 1) is the one 4ndv/mirajazz + opendeck-akp03
query for every AKP03 variant.
"""

import errno
import sys
import types

import pytest
from helpers import Clock, make_app

from shack_pad import InputHost, diagnose
from shack_pad.drivers import AKP03E
from shack_pad.drivers.ajazz import PRODUCT, VENDOR
from shack_pad.fake import FakeHid, akp03e_report
from shack_pad.focus import DesktopPad
from shack_pad.transport import hidapi

WIN = "\\\\?\\HID#VID_0300&PID_3002&{mi}#7&2a3b4c5d&0&0000#{{4d1e55b2-f16f-11cf-88cb-001111000030}}"


def win_entry(mi, usage_page, usage, interface, vid=VENDOR, pid=PRODUCT):
    return {"path": WIN.format(mi=mi).encode(), "vendor_id": vid, "product_id": pid,
            "serial_number": "SN-DO-NOT-LEAK", "release_number": 0x0100,
            "manufacturer_string": "HOTSPOTEKUSB", "product_string": "HOTSPOTEKUSB HID DEMO",
            "usage_page": usage_page, "usage": usage, "interface_number": interface}


# The AKP03E on Windows as hidapi would list it: its vendor interface, and
# the keyboard face's interface split into keyboard and media-key collections.
AKP03E_WINDOWS = [
    win_entry("MI_01&Col01", 0x01, 0x06, 1),
    win_entry("MI_01&Col02", 0x0C, 0x01, 1),
    win_entry("MI_00", 0xFFA0, 0x01, 0),
]


def fake_hid(monkeypatch, entries, device=None):
    def enumerate_(vid=0, pid=0):
        return [e for e in entries if (not vid or e["vendor_id"] == vid) and (not pid or e["product_id"] == pid)]
    mod = types.SimpleNamespace(enumerate=enumerate_, version_str=lambda: "0.15.0",
                                device=device or (lambda: None))
    monkeypatch.setitem(sys.modules, "hid", mod)
    return mod


# --- Choosing the interface ----------------------------------------------------------
def test_windows_picks_the_vendor_collection_not_the_keyboard_or_media_keys(monkeypatch):
    fake_hid(monkeypatch, AKP03E_WINDOWS)
    assert hidapi.find(VENDOR, PRODUCT) == [WIN.format(mi="MI_00")]


def test_windows_interface_zero_with_two_collections_takes_only_the_vendor_one(monkeypatch):
    # Before 12/10/2026 any entry on interface 0 counted, and the first was
    # opened: here that would have been the mouse-shaped collection.
    entries = [win_entry("MI_00&Col02", 0x01, 0x02, 0), win_entry("MI_00&Col01", 0xFFA0, 0x01, 0),
               win_entry("MI_01", 0x01, 0x06, 1)]
    fake_hid(monkeypatch, entries)
    assert hidapi.find(VENDOR, PRODUCT) == [WIN.format(mi="MI_00&Col01")]


def test_akp03e_find_uses_hidapi_off_linux(monkeypatch):
    fake_hid(monkeypatch, AKP03E_WINDOWS)
    monkeypatch.setattr(sys, "platform", "win32")
    assert AKP03E.find() == [WIN.format(mi="MI_00")]


def test_macos_with_no_interface_number_still_finds_the_vendor_usage():
    entries = [{"path": b"DevSrvsID:4294970000", "interface_number": -1, "usage_page": 0x01, "usage": 0x06},
               {"path": b"DevSrvsID:4294970001", "interface_number": -1, "usage_page": 0xFFA0, "usage": 0x01}]
    assert [e["path"] for e in hidapi.choose(entries)] == [b"DevSrvsID:4294970001"]


def test_a_backend_without_usages_falls_back_to_interface_zero():
    entries = [{"path": b"1-2:1.1", "interface_number": 1, "usage_page": 0, "usage": 0},
               {"path": b"1-2:1.0", "interface_number": 0, "usage_page": 0, "usage": 0}]
    assert [e["path"] for e in hidapi.choose(entries)] == [b"1-2:1.0"]


def test_without_the_hidapi_package_nothing_is_found(monkeypatch):
    monkeypatch.setitem(sys.modules, "hid", None)       # import hid raises ImportError
    assert hidapi.find(VENDOR, PRODUCT) == [] and hidapi.library_version() is None


# --- Opening, and errors in words ----------------------------------------------------
class RefusingDevice:
    def open_path(self, path):
        raise OSError("open failed")

    def error(self):
        return "Access is denied."

    def close(self):
        pass


def test_a_refused_open_says_why_and_what_to_do(monkeypatch):
    fake_hid(monkeypatch, AKP03E_WINDOWS, device=RefusingDevice)
    with pytest.raises(OSError) as e:
        AKP03E.open(WIN.format(mi="MI_00"))
    assert "Access is denied" in str(e.value)
    words = diagnose.explain(e.value, "win32")
    assert "Ajazz or Mirabox" in words and "plug it in again" in words


def test_explain_covers_permission_unplug_and_write():
    assert "udev rule" in diagnose.explain(PermissionError(errno.EACCES, "Permission denied"), "linux")
    assert "unplugged" in diagnose.explain(OSError(errno.ENODEV, "No such device"), "linux", doing="read")
    assert "picture" in diagnose.explain(OSError("write error: The parameter is incorrect."), "win32", "write")
    assert "Windows would not open" in diagnose.explain(OSError("open failed"), "win32")


# --- The survey and the report -------------------------------------------------------
def test_windows_survey_names_the_pad_its_interface_and_the_makers_software(monkeypatch):
    fake_hid(monkeypatch, AKP03E_WINDOWS)
    s = diagnose.survey("win32", entries=AKP03E_WINDOWS, descriptors=False, software=["StreamDock.exe"])
    assert s["chosen"] == WIN.format(mi="MI_00")
    [dev] = s["devices"]
    assert dev["supported"] and dev["name"] == "Ajazz AKP03E (rev. 2)"
    assert [i["chosen"] for i in dev["interfaces"]] == [False, False, True]
    assert any("StreamDock.exe" in p and "Close it" in p for p in s["problems"])
    text = diagnose.report(s, ["app: QO-100 NB 0.1.16"])
    assert "USB 0300:3002" in text and "usage ffa0:0001 <- the pad" in text and "app: QO-100 NB 0.1.16" in text
    assert "SN-DO-NOT-LEAK" not in text


def test_a_first_revision_akp03e_is_named_and_refused_never_opened(monkeypatch):
    rev1 = [win_entry("MI_00", 0xFFA0, 0x01, 0, pid=0x1002)]
    fake_hid(monkeypatch, rev1)
    monkeypatch.setattr(sys, "platform", "win32")
    assert AKP03E.find() == []
    s = diagnose.survey("win32", entries=rev1, descriptors=False, software=[])
    assert s["chosen"] is None
    assert any("0300:1002" in p and "first revision" in p for p in s["problems"])
    assert "NOT supported" in diagnose.report(s)


def test_a_build_without_hidapi_says_so(monkeypatch):
    monkeypatch.setattr(hidapi, "library_version", lambda: None)
    s = diagnose.survey("win32", entries=[], descriptors=False, software=[])
    assert any("missing the hidapi library" in p for p in s["problems"])


def test_nothing_plugged_in_says_check_the_cable(monkeypatch):
    fake_hid(monkeypatch, [])
    s = diagnose.survey("win32", entries=[], descriptors=False, software=[])
    assert any("No Ajazz or Mirabox controller" in p for p in s["problems"])


def descriptor(input_count=512, output_count=1024):
    """A vendor-page descriptor shaped like the AKP03E's: one input and one
    output report of 8-bit fields, no report ids."""
    return bytes([0x06, 0xA0, 0xFF, 0x09, 0x01, 0xA1, 0x01,          # usage page FFA0, usage 1, collection
                  0x75, 0x08, 0x96, input_count & 0xFF, input_count >> 8, 0x09, 0x02, 0x81, 0x02,
                  0x96, output_count & 0xFF, output_count >> 8, 0x09, 0x03, 0x91, 0x02, 0xC0])


def test_report_descriptor_lengths():
    d = diagnose.parse_descriptor(descriptor())
    assert d["usage_pages"] == [0xFFA0] and d["input"] == {0: 512} and d["output"] == {0: 1024}


def linux_sysfs(tmp_path, out_count=1024, readable=True):
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    for name, phys in (("hidraw3", "input0"), ("hidraw4", "input1")):
        d = sysfs / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(f"HID_ID=0003:00000300:00003002\nHID_NAME=HOTSPOTEKUSB HID DEMO\n"
                                  f"HID_PHYS=usb-0000:00:14.0-5/{phys}\n")
        (d / "report_descriptor").write_bytes(descriptor(output_count=out_count))
        node = dev / name
        node.write_bytes(b"")
        node.chmod(0o600 if readable else 0)
    return sysfs, dev


def test_linux_survey_finds_input0_and_reports_a_missing_udev_rule(tmp_path):
    sysfs, dev = linux_sysfs(tmp_path, readable=False)
    s = diagnose.survey("linux", sysfs=sysfs, dev=dev, software=[])
    assert s["chosen"] == str(dev / "hidraw3")
    assert any("udev rule" in p for p in s["problems"])
    assert "NO ACCESS" in diagnose.report(s)


def test_linux_survey_of_a_healthy_pad_finds_no_problem(tmp_path):
    sysfs, dev = linux_sysfs(tmp_path)
    s = diagnose.survey("linux", sysfs=sysfs, dev=dev, software=[])
    if not diagnose.libraries()["pillow"]:
        s["problems"] = [p for p in s["problems"] if "Pillow" not in p]
    assert s["problems"] == []
    assert "output 0:1024" in diagnose.report(s)


def test_a_pad_wanting_other_report_sizes_is_called_a_different_revision(tmp_path):
    sysfs, dev = linux_sysfs(tmp_path, out_count=512)
    s = diagnose.survey("linux", sysfs=sysfs, dev=dev, software=[])
    assert any("output report is 512 bytes" in p for p in s["problems"])


def test_vendor_software_on_windows_and_linux(tmp_path):
    csv = '"System Idle Process","0","Services","0","8 K"\n"StreamDock.exe","4242","Console","1","90,000 K"\n'

    def runner(cmd, **kw):
        assert cmd[0] == "tasklist" and "creationflags" in kw
        return types.SimpleNamespace(stdout=csv)
    assert diagnose.vendor_software("win32", runner=runner) == ["StreamDock.exe"]
    for pid, comm in (("1", "systemd"), ("77", "opendeck")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "comm").write_text(comm + "\n")
    assert diagnose.vendor_software("linux", proc=tmp_path) == ["opendeck"]


def test_the_start_up_line_names_the_libraries():
    line = diagnose.describe_libraries("win32")
    assert line.startswith("shack_pad ") and ("hidapi " in line) and ("Pillow" in line)


# --- Test mode -------------------------------------------------------------------------
@pytest.fixture
def host(nb):
    clock = Clock()
    app = make_app("qo100nb", *nb, name="NB")
    return InputHost([app], clock=clock, host="test"), app, clock


def test_test_mode_shows_presses_and_never_keys_or_acts(host):
    h, app, clock = host
    seen = []
    h.listeners.append(lambda model, ev: seen.append(ev))
    fake = FakeHid([akp03e_report(0x01, 1), akp03e_report(0x01, 0), akp03e_report(0x02, 1),
                    akp03e_report(0x91)])
    dev = AKP03E(fake)
    h.attach(dev)
    h.start_test()
    while not fake.drained():
        h.step(dev, 0)
    assert seen == [("press", "K1", 0), ("release", "K1", 0), ("press", "K2", 0), ("turn", "D2", 1)]
    assert app.tx.calls("key") == [] and app.performed == []
    faces = h.faces("ajazz-akp03e")
    assert faces["K1"].l1 == "K1" and faces["K2"].tone == "active"
    h.stop_test()
    assert not h.testing()


def test_test_mode_lights_each_key_in_turn(host):
    h, _app, clock = host
    h.start_test()
    lit = []
    for _ in range(6):
        lit.append([k for k, f in h.faces("ajazz-akp03e").items() if f.tone == "info"])
        clock.advance(0.5)
    assert lit == [["K1"], ["K2"], ["K3"], ["K4"], ["K5"], ["K6"]]


def test_test_mode_is_refused_on_air_and_ends_when_an_app_keys(host):
    h, app, clock = host
    app.tx.on_air = True
    with pytest.raises(RuntimeError):
        h.start_test()
    app.tx.on_air = False
    h.start_test()
    assert h.testing()
    app.tx.on_air = True                  # keyed from the app's own window
    assert not h.testing()
    assert h.faces("ajazz-akp03e")["K1"].tone == "onair"


def test_test_mode_ends_by_itself(host):
    h, _app, clock = host
    h.start_test(seconds=10)
    clock.advance(10.1)
    assert not h.testing()


def test_a_press_held_into_the_test_does_not_eat_the_next_press(host):
    h, app, clock = host
    fake = FakeHid([akp03e_report(0x02, 1)])
    dev = AKP03E(fake)
    h.attach(dev)
    h.step(dev, 0)                         # K2 (mute) down before the test
    h.start_test()
    fake.push(akp03e_report(0x02, 0))
    h.step(dev, 0)
    h.stop_test()
    fake.push(akp03e_report(0x02, 1))
    h.step(dev, 0)
    assert [a.action for a in app.performed] == ["mute", "mute"]


# --- DesktopPad status --------------------------------------------------------------------
def test_desktop_status_says_missing_refused_serving_and_unplugged(nb, tmp_path, monkeypatch):
    app = make_app("qo100nb", *nb)
    h = InputHost([app], host="desk")
    plan = {"next": None}
    fakes = []

    def open_device():
        what = plan["next"]
        if isinstance(what, BaseException):
            raise what
        if what == "pad":
            fakes.append(FakeHid())
            return AKP03E(fakes[-1])
        return None
    pad = DesktopPad(h, app="shack-qo100nb", store_dir=tmp_path, open_device=open_device, token="t")
    try:
        pad.focus_gained()
        assert pad.status()["state"] == "missing"
        plan["next"] = PermissionError(errno.EACCES, "Permission denied")
        pad._retry_at = 0
        pad.poll()
        st = pad.status()
        assert st["state"] == "error" and st["error"]
        plan["next"] = "pad"
        pad._retry_at = 0
        pad.poll()
        assert pad.serving() and pad.status()["state"] == "serving" and pad.status()["error"] == ""
        fakes[-1].unplug()
        pad.thread.join(timeout=2)
        plan["next"] = None
        pad.poll()
        st = pad.status()
        assert st["state"] == "error" and "unplugged" in st["error"]
    finally:
        pad.close()
