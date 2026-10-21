"""Which Pluto this is: firmware family, chip setting, tuning range, and the
key it is remembered by on this computer.

**Firmware family** decides what the app may do to a Pluto beyond tuning it
(PLAN-0.2 §3.6.4):

| family | how it is told | AD9364 enable | notes |
|---|---|---|---|
| `adi` | `fw_version` like `v0.39` or `v0.39-dirty`: ADI's own and the clones built from it | yes (D5) | shack's Rev.B clone: v0.39-dirty, BusyBox 1.36.1 (measured 18/10/2026) |
| `tezuka` | `fw_version` starting `tezuka` | refused until verified (D5) | the nano: tezuka-v0.3.21; its FIR writes are EINVAL (openhamshack-nb ohscore/devices/pluto.py) |
| `plutodvb2` | not Tezuka, and any of: its MQTT broker answering on the Pluto (port 1883, which every PlutoDVB2 control path uses: openhamshack `plutodvb2_push`, `mqtt_push`); F5OEO's scripts found over SSH (`probe_over_ssh`: /root/watchconsoletx.sh, started by /etc/init.d/S95bgcript); or its version on its own broker, read over SSH (`dt/pluto/+/system/version`, "PlutoDVB2-0.5.16.7(F5OEO)" on workshop's, 3/10/2026) | refused until verified (D5) | Its fw_version has not been read from one [unverified]; "dvb" in it counts. Tezuka answers on 1883 as well (the nano, 18/10/2026), so Tezuka is told first; the ADI clone does not. A Pluto shared through the controller box (endpoint.py) forwards only iiod and SSH, so port 1883 is unreachable there and the SSH probe is what tells it. It reports itself "PlutoSDR Rev.C" (its device tree). Its own DATV processes can transmit, and its watchconsoletx.sh drives GPO0 with the TX LO (radios/pluto.py `ptt_output`): the app must not share it with them |
| `unknown` | anything else | refused | |

**Chip setting**: the context's `ad9361-phy,model` - `ad9363` on a stock
Pluto (325 MHz-3.8 GHz), `ad9364` once enabled (70 MHz-6 GHz), `ad9361` on
boards built with one (the nano: 46.9 MHz-6 GHz). The range itself is read,
not assumed, from each LO's `frequency_available`, `[low step high]`: the
two can differ (shack's clone in AD9364 mode: RX 70 MHz, TX 46.875 MHz, up
to 6 GHz, 18/10/2026).

**The key** a Pluto's settings are kept under (D5: "per serial or MAC"):
`hw_serial` when it is a real one; the Tezuka nano was seen reporting
`ffffffffffffffff` on 08/10/2026 (and its real serial on 18/10), so all-f
and all-0 serials are not used, and the USB network's MAC is the key
instead. Two Plutos were seen with the same `ethaddr` in their environment
(00:0a:35:00:01:22, both), so the MAC is the USB gadget's, which differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FAMILIES = ("adi", "tezuka", "plutodvb2", "unknown")
#: What each family may have done to it here (module docstring).
CAN_ENABLE_AD9364 = {"adi"}


def parse_range(text: str | None) -> tuple[float, float] | None:
    """`[70000000 1 6000000000]` (an iio *_available attribute) -> (low, high)."""
    if not text:
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(text))
    if len(nums) < 2:
        return None
    return float(nums[0]), float(nums[-1])


def family_of(fw_version: str, *, mqtt_answers: bool = False, probe: dict | None = None) -> str:
    """`probe`: probe_over_ssh's answer, when SSH could be reached."""
    fw = (fw_version or "").strip().lower()
    probe = probe or {}
    # Tezuka first: the nano on tezuka-v0.3.21 answers on 1883 too
    # (measured 18/10/2026), so the broker alone is not PlutoDVB2.
    if fw.startswith("tezuka") or probe.get("tezuka"):
        return "tezuka"
    if mqtt_answers or "dvb" in fw or probe.get("f5oeo") or probe.get("dvb2_version"):
        return "plutodvb2"
    if re.match(r"^v0\.\d+", fw):
        return "adi"
    return "unknown"


def usable_serial(serial: str | None) -> str | None:
    s = (serial or "").strip().lower()
    if not s or set(s) <= {"f"} or set(s) <= {"0"}:
        return None
    return s


@dataclass
class PlutoIdentity:
    uri: str
    hw_model: str = ""
    fw_version: str = ""
    serial: str | None = None
    chip: str = ""
    family: str = "unknown"
    tx_range: tuple[float, float] | None = None
    rx_range: tuple[float, float] | None = None
    mac: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str | None:
        """What this Pluto's settings are filed under, or None when it has
        neither a usable serial nor a known MAC."""
        if self.serial:
            return f"serial:{self.serial}"
        if self.mac:
            return f"mac:{self.mac.lower()}"
        return None

    @property
    def can_enable_ad9364(self) -> bool:
        return self.family in CAN_ENABLE_AD9364

    def covers(self, hz: float, *, transmit: bool = True) -> bool | None:
        """Whether `hz` is inside what the chip is set up to tune (both
        sides when transmitting); None when the range could not be read."""
        ranges = [self.rx_range] + ([self.tx_range] if transmit else [])
        if any(r is None for r in ranges):
            return None
        return all(r[0] <= hz <= r[1] for r in ranges)

    def as_dict(self) -> dict:
        return {"uri": self.uri, "hwModel": self.hw_model, "fwVersion": self.fw_version, "serial": self.serial,
                "chip": self.chip, "family": self.family, "txRange": self.tx_range, "rxRange": self.rx_range,
                "mac": self.mac, "key": self.key}


def _attr(chan, name):
    try:
        return chan.attrs[name].value
    except (KeyError, OSError, AttributeError):
        return None


def identify(ctx, uri: str, *, mac: str | None = None, mqtt_answers: bool = False,
             probe: dict | None = None) -> PlutoIdentity:
    """Read a Pluto's identity from an open iio Context (read-only: no
    attribute is written). `mac` is the USB network's MAC when the caller
    knows it; `mqtt_answers` whether port 1883 on the Pluto answered;
    `probe` what probe_over_ssh found."""
    attrs = dict(getattr(ctx, "attrs", {}) or {})
    ident = PlutoIdentity(uri=uri, hw_model=str(attrs.get("hw_model", "") or ""),
                          fw_version=str(attrs.get("fw_version", "") or ""),
                          serial=usable_serial(attrs.get("hw_serial")),
                          chip=str(attrs.get("ad9361-phy,model", "") or "").lower(), mac=mac)
    ident.family = family_of(ident.fw_version, mqtt_answers=mqtt_answers, probe=probe)
    if probe and probe.get("dvb2_version"):
        ident.extra["plutodvb2"] = probe["dvb2_version"]
    phy = ctx.find_device("ad9361-phy")
    if phy is not None:
        tx_lo = phy.find_channel("altvoltage1", True)
        rx_lo = phy.find_channel("altvoltage0", True)
        ident.tx_range = parse_range(_attr(tx_lo, "frequency_available")) if tx_lo is not None else None
        ident.rx_range = parse_range(_attr(rx_lo, "frequency_available")) if rx_lo is not None else None
    return ident


def mqtt_answers(host: str, timeout: float = 0.5) -> bool:
    """Whether something accepts a TCP connection on the Pluto's port 1883:
    PlutoDVB2's broker. A plain connect, nothing sent."""
    import socket
    try:
        with socket.create_connection((host, 1883), timeout=timeout):
            return True
    except OSError:
        return False


#: Where a Pluto's USB network keeps the MAC the computer sees for it
#: (the host end, enx<mac> on Linux): read over SSH, both Plutos measured
#: 18/10/2026 (rndis.0 on the clone, ncm.usb0 on the nano).
HOST_ADDR_GLOB = "/sys/kernel/config/usb_gadget/*/functions/*/host_addr"


def mac_over_ssh(runner) -> str | None:
    """The USB network's host-side MAC, for a Pluto with no usable serial;
    None when it cannot be read. `runner` is a guard.SshRunner."""
    try:
        status, out = runner.run(f"cat {HOST_ADDR_GLOB} 2>/dev/null | head -n 1", timeout=5.0)
    except Exception:                               # noqa: BLE001 - no MAC, said by the caller
        return None
    mac = out.strip().replace("\x00", "").lower()
    return mac if status == 0 and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac) else None


#: One SSH command, read-only: F5OEO's PTT script (PlutoDVB2 and 2201RC),
#: PlutoDVB2's version from its own broker (3 s at most; nothing is
#: published to it), and Tezuka's name in /etc/os-release or the banner
#: file, if it says so.
PROBE_COMMAND = ("ls /root/watchconsoletx.sh /etc/init.d/S95bgcript 2>/dev/null; "
                 "command -v mosquitto_sub >/dev/null 2>&1 && "
                 "mosquitto_sub -h 127.0.0.1 -t 'dt/pluto/+/system/version' -C 1 -W 3 -v 2>/dev/null; "
                 "cat /etc/os-release /etc/issue 2>/dev/null | head -n 6")


def parse_probe(text: str) -> dict:
    """PROBE_COMMAND's output -> {"f5oeo", "dvb2_version", "tezuka"}."""
    out = {"f5oeo": False, "dvb2_version": None, "tezuka": False}
    for line in (text or "").splitlines():
        line = line.strip()
        if line.endswith(("watchconsoletx.sh", "S95bgcript")):
            out["f5oeo"] = True
        m = re.search(r"system/version\s+(PlutoDVB2-[\w.]+(?:\([^)]*\))?)", line)
        if m:
            out["dvb2_version"] = m.group(1)
        if "tezuka" in line.lower():
            out["tezuka"] = True
    return out


def probe_over_ssh(runner) -> dict | None:
    """What SSH shows of the firmware (PROBE_COMMAND), or None when SSH
    cannot be reached. `runner` is a guard.SshRunner (its timeouts allow
    PlutoDVB2's slow banner)."""
    try:
        _status, out = runner.run(PROBE_COMMAND, timeout=10.0)
    except Exception:                               # noqa: BLE001 - no probe, said by the caller
        return None
    return parse_probe(out)
