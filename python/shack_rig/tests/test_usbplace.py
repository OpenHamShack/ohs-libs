"""Which USB Audio CODEC is the radio's (shack_rig.usbplace, moved from
openhamshack-nb's ohscore/rig/usbpair.py with these tests): by where
it sits on USB beside the CI-V port, never by name. A sysfs tree laid out
as Brian's 9700 is on shack (CI-V on 3-1.2.2/3-1.2.3, CODEC on 3-1.2.4)
with a second radio on another hub, and Windows' answer for Ray's PC, two
CODECs of which he had renamed the 9700's."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from shack_rig import usbplace as usbpair

ROOT = "devices/pci0000:00/0000:00:08.1/0000:e4:00.4/usb3"
NINE_7 = f"{ROOT}/3-1/3-1.2"            # the IC-9700's internal hub
OTHER = f"{ROOT}/3-2/3-2.1"             # a second radio's
LINUX = ["HD-Audio Generic: ALC269VC Analog (hw:1,0)", "USB Audio CODEC: - (hw:2,0)", "USB Audio CODEC: - (hw:3,0)"]
RAY_MME = ["Speakers / Headphones (Realtek ", "Speakers (2- USB AUDIO  CODEC)", "IC9700 Speakers (USB Audio CODE"]


def sysfs(root: Path, cards: dict, ttys: dict) -> None:
    """cards {n: device path}, ttys {name: device path} under root/devices."""
    for n, path in cards.items():
        (root / path).mkdir(parents=True, exist_ok=True)
        (root / "class" / "sound" / f"card{n}").mkdir(parents=True, exist_ok=True)
        os.symlink(root / path, root / "class" / "sound" / f"card{n}" / "device")
    for name, path in ttys.items():
        (root / path).mkdir(parents=True, exist_ok=True)
        (root / "class" / "tty" / name).mkdir(parents=True, exist_ok=True)
        os.symlink(root / path, root / "class" / "tty" / name / "device")


@unittest.skipIf(os.name == "nt", "sysfs symlinks")
class Linux(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        sysfs(self.root, {1: "devices/pci0000:00/0000:00:08.1/0000:e4:00.6",
                          2: f"{OTHER}/3-2.1.4/3-2.1.4:1.0", 3: f"{NINE_7}/3-1.2.4/3-1.2.4:1.0"},
              {"ttyUSB0": f"{OTHER}/3-2.1.1/3-2.1.1:1.0/ttyUSB0",
               "ttyUSB1": f"{NINE_7}/3-1.2.2/3-1.2.2:1.0/ttyUSB1"})

    def test_the_codec_beside_the_ci_v_port_is_the_radios(self):
        found = usbpair.linux_hubs("/dev/ttyUSB1", LINUX, sysfs=str(self.root))
        self.assertEqual(found["port"], "3-1.2")
        self.assertEqual(found["audio"][LINUX[2]], "3-1.2")
        self.assertEqual(found["audio"][LINUX[1]], "3-2.1")
        codecs = LINUX[1:]
        self.assertEqual(usbpair.pair(codecs, found["port"], found["audio"]), LINUX[2])
        # The other radio's port finds the other radio's CODEC.
        other = usbpair.linux_hubs("/dev/ttyUSB0", LINUX, sysfs=str(self.root))
        self.assertEqual(usbpair.pair(codecs, other["port"], other["audio"]), LINUX[1])

    def test_a_port_it_cannot_place_says_nothing(self):
        self.assertIsNone(usbpair.linux_hubs("/dev/ttyUSB9", LINUX, sysfs=str(self.root)))
        self.assertIsNone(usbpair.linux_hubs("socket://127.0.0.1:5000", LINUX, sysfs=str(self.root)))

    def test_two_codecs_on_one_hub_is_no_answer(self):
        self.assertIsNone(usbpair.pair(LINUX[1:], "3-1.2", {LINUX[1]: "3-1.2", LINUX[2]: "3-1.2"}))


def ray_nodes(parents=True):
    """Ray's PC as Windows' device tree shows it (the standalone diagnostic's
    fixture, tools/ic9700-diag): the 9700 behind its own hub (CP2102N COM4
    and COM6, a PCM2901 CODEC he renamed "IC9700 Speakers") and the FTDX10
    behind another (a CP2105, COM3/COM5, and another PCM2901), both on one
    external hub; and a Bluetooth COM port on the root hub beside a webcam."""
    def n(i, cls, name, parent, cont):
        return {"id": i, "cls": cls, "name": name, "parent": parent if parents else "", "container": cont}
    pc = usbpair.PC_CONTAINER
    return [
        n("USB\\VID_0451&PID_2046\\H97", "USB", "Generic USB Hub", "USB\\VID_05E3&PID_0610\\EXT", "{C97}"),
        n("USB\\VID_0424&PID_2512\\HFT", "USB", "Generic USB Hub", "USB\\VID_05E3&PID_0610\\EXT", "{CFT}"),
        n("USB\\VID_10C4&PID_EA60\\A", "Ports", "Silicon Labs CP210x USB to UART Bridge (COM4)",
          "USB\\VID_0451&PID_2046\\H97", "{C97}"),
        n("USB\\VID_10C4&PID_EA60\\B", "Ports", "Silicon Labs CP210x USB to UART Bridge (COM6)",
          "USB\\VID_0451&PID_2046\\H97", "{C97}"),
        n("USB\\VID_10C4&PID_EA70\\01AED7FB", "USB", "USB Composite Device", "USB\\VID_0424&PID_2512\\HFT", "{CFT}"),
        n("USB\\VID_10C4&PID_EA70&MI_00\\P0", "Ports",
          "Silicon Labs Dual CP2105 USB to UART Bridge: Enhanced COM Port (COM5)",
          "USB\\VID_10C4&PID_EA70\\01AED7FB", "{CFT}"),
        n("USB\\VID_08BB&PID_2901\\C97", "USB", "USB Composite Device", "USB\\VID_0451&PID_2046\\H97", "{C97}"),
        n("USB\\VID_08BB&PID_2901&MI_00\\M97", "MEDIA", "USB Audio CODEC", "USB\\VID_08BB&PID_2901\\C97", "{C97}"),
        n("USB\\VID_08BB&PID_2901\\CFT", "USB", "USB Composite Device", "USB\\VID_0424&PID_2512\\HFT", "{CFT}"),
        n("USB\\VID_08BB&PID_2901&MI_00\\MFT", "MEDIA", "USB AUDIO  CODEC", "USB\\VID_08BB&PID_2901\\CFT", "{CFT}"),
        n("SWD\\MMDEVAPI\\{0.0.0.00000000}.{E97}", "AudioEndpoint", "IC9700 Speakers (USB Audio CODEC)",
          "SWD\\MMDEVAPI\\MMDEVAPI", "{C97}"),
        n("SWD\\MMDEVAPI\\{0.0.0.00000000}.{EFT}", "AudioEndpoint", "Speakers (2- USB AUDIO  CODEC)",
          "SWD\\MMDEVAPI\\MMDEVAPI", "{CFT}"),
        n("SWD\\MMDEVAPI\\{0.0.1.00000000}.{M97}", "AudioEndpoint", "Microphone (USB Audio CODEC)",
          "SWD\\MMDEVAPI\\MMDEVAPI", "{C97}"),
        n("SWD\\MMDEVAPI\\{0.0.0.00000000}.{RTK}", "AudioEndpoint", "Speakers / Headphones (Realtek Audio)",
          "SWD\\MMDEVAPI\\MMDEVAPI", pc),
        # Straight into the computer: a Bluetooth COM port and a webcam
        # share the root hub, and no radio.
        n("BTHENUM\\{1101}\\BT", "Ports", "Standard Serial over Bluetooth link (COM9)", "USB\\VID_8087&PID_0026\\BT",
          pc),
        n("USB\\VID_8087&PID_0026\\BT", "USB", "Bluetooth", "USB\\ROOT_HUB30\\4&1", pc),
        n("USB\\VID_046D&PID_082D\\CAM", "USB", "HD Pro Webcam C920", "USB\\ROOT_HUB30\\4&1", pc),
        n("SWD\\MMDEVAPI\\{0.0.0.00000000}.{CAM}", "AudioEndpoint", "Speakers (HD Pro Webcam C920)",
          "USB\\VID_046D&PID_082D\\CAM", pc),
    ]


class Windows(unittest.TestCase):
    def test_rays_pc_the_renamed_one_on_the_9700s_own_hub(self):
        codecs = RAY_MME[1:]
        m = usbpair.windows_match(ray_nodes(), "COM4", codecs)
        self.assertEqual(m, {"IC9700 Speakers (USB Audio CODE": "hub"})     # MME cut it at 31 characters
        self.assertEqual(usbpair.pair(codecs, matched=m), "IC9700 Speakers (USB Audio CODE")
        # The FTDX10's port finds the FTDX10's CODEC.
        self.assertEqual(usbpair.windows_match(ray_nodes(), "COM5", codecs), {"Speakers (2- USB AUDIO  CODEC)": "hub"})
        # With no parent chains, Windows' container still decides.
        self.assertEqual(usbpair.windows_match(ray_nodes(parents=False), "COM4", codecs),
                         {"IC9700 Speakers (USB Audio CODE": "container"})

    def test_a_root_hub_or_the_computers_container_is_no_radio(self):
        self.assertEqual(usbpair.windows_match(ray_nodes(), "COM9", ["Speakers (HD Pro Webcam C920)"]), {})

    def test_powershell_output_and_failure(self):
        one = ray_nodes()[0]
        self.assertEqual(usbpair.windows_nodes(run=lambda _s: json.dumps(one)), [one])   # a single item, unwrapped
        self.assertEqual(usbpair.windows_nodes(run=lambda _s: ""), [])
        self.assertEqual(usbpair.windows_nodes(run=lambda _s: "not json"), [])
        self.assertEqual(usbpair.windows_match([], "COM4", RAY_MME), {})


if __name__ == "__main__":
    unittest.main()


# -- an SDR on the radio's hub (added in shack_rig) ------------------------------


def usb_device(root: Path, path: str, vid: int, pid: int) -> None:
    """A USB device at `path` under root, listed in bus/usb/devices."""
    d = root / path
    d.mkdir(parents=True, exist_ok=True)
    (d / "idVendor").write_text(f"{vid:04x}\n")
    (d / "idProduct").write_text(f"{pid:04x}\n")
    (root / "bus" / "usb" / "devices").mkdir(parents=True, exist_ok=True)
    os.symlink(d, root / "bus" / "usb" / "devices" / Path(path).name)


@unittest.skipIf(os.name == "nt", "sysfs symlinks")
class SdrOnTheRadiosHub(unittest.TestCase):
    """The 9700 behind an external hub at 3-1 (its own hub 3-1.2), a Pluto
    on that external hub, an RTL straight into the computer, an SDRplay on
    another bus's hub."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        sysfs(self.root, {2: f"{NINE_7}/3-1.2.4/3-1.2.4:1.0", 3: f"{OTHER}/3-2.1.4/3-2.1.4:1.0"}, {})
        usb_device(self.root, f"{NINE_7}/3-1.2.4", 0x08BB, 0x2901)            # the codec itself
        usb_device(self.root, f"{ROOT}/3-1/3-1.3", 0x0456, 0xB673)            # Pluto, on the radio's hub
        usb_device(self.root, f"{ROOT}/3-4", 0x0BDA, 0x2838)                  # RTL, on a root port
        usb_device(self.root, "devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2.1", 0x1DF7, 0x3050)  # SDRplay

    def test_an_sdr_on_a_hub_above_the_codec_is_named(self):
        self.assertEqual(usbpair.linux_sdrs_sharing(2, sysfs=str(self.root)), ["PlutoSDR"])

    def test_root_ports_and_other_hubs_do_not_count(self):
        self.assertEqual(usbpair.linux_sdrs_sharing(3, sysfs=str(self.root)), [])

    def test_a_card_it_cannot_place_says_nothing(self):
        self.assertIsNone(usbpair.linux_sdrs_sharing(7, sysfs=str(self.root)))


class SdrOnTheRadiosHubWindows(unittest.TestCase):
    """The same arrangement as Windows' PnP tree gives it."""

    NODES = [
        {"id": "USB\\ROOT_HUB30\\4&1", "cls": "USB", "name": "root", "parent": "PCI\\X", "container": ""},
        {"id": "USB\\VID_05E3&PID_0610\\HUB", "cls": "USB", "name": "external hub",
         "parent": "USB\\ROOT_HUB30\\4&1", "container": ""},
        {"id": "USB\\VID_0451&PID_2046\\9700HUB", "cls": "USB", "name": "9700 hub",
         "parent": "USB\\VID_05E3&PID_0610\\HUB", "container": "{C9700}"},
        {"id": "USB\\VID_08BB&PID_2901\\CODEC", "cls": "USB", "name": "codec",
         "parent": "USB\\VID_0451&PID_2046\\9700HUB", "container": "{C9700}"},
        {"id": "USB\\VID_08BB&PID_2901&MI_00\\IF", "cls": "MEDIA", "name": "USB Audio CODEC",
         "parent": "USB\\VID_08BB&PID_2901\\CODEC", "container": "{C9700}"},
        {"id": "SWD\\MMDEVAPI\\{0.0.0.00000000}.{OUT}", "cls": "AudioEndpoint", "name": "Speakers (USB Audio CODEC )",
         "parent": "USB\\VID_08BB&PID_2901&MI_00\\IF", "container": "{C9700}"},
        {"id": "USB\\VID_0456&PID_B673\\PLUTO", "cls": "USB", "name": "PlutoSDR",
         "parent": "USB\\VID_05E3&PID_0610\\HUB", "container": "{CPLUTO}"},
        {"id": "USB\\VID_0BDA&PID_2838\\RTL", "cls": "USB", "name": "RTL",
         "parent": "USB\\ROOT_HUB30\\4&1", "container": "{CRTL}"},
    ]

    def test_the_pluto_on_the_external_hub_is_named_and_the_rtl_is_not(self):
        self.assertEqual(usbpair.windows_sdrs_sharing(self.NODES, "Speakers (USB Audio CODEC )"), ["PlutoSDR"])

    def test_an_unknown_output_says_nothing(self):
        self.assertIsNone(usbpair.windows_sdrs_sharing(self.NODES, "Headphones"))

    def test_the_dispatcher_never_raises(self):
        self.assertIsNone(usbpair.sdrs_sharing(""))
        self.assertIsNone(usbpair.sdrs_sharing("no card number here"))
