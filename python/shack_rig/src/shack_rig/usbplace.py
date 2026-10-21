"""Where a radio's devices sit on USB: which sound device is the radio's,
and whether an SDR shares a USB hub with it.

**Moved from openhamshack-nb** (`ohscore/rig/usbpair.py` at e73f68f, written for
NB's setup assistant), because the packet app is its second user and the
family's rule is one shared component, not two copies (GUIDE §1). NB keeps
its copy until its owner switches it to this one. Its own docstring follows,
then the part added here.

Which sound device belongs to the radio on a serial port: by where both
sit on USB, not by name.

Every Icom with a USB port (the IC-9700, 7300, 7610, 705…) has the same
TI/Burr-Brown "USB Audio CODEC" behind the same internal hub as its CI-V
bridge, so a station with two Icoms has two devices of that name. Ray's
does (12/10/2026: an IC-9700 on a CP2102N, a second radio on a CP2105),
and NB's setup assistant took "Speakers (2- USB AUDIO  CODEC)" by name -
the other radio's - so the level set keyed the 9700 and played its tone
into a radio that was not transmitting: ALC 5 and Po 0 at every level.
Ray had renamed the right one "IC9700 Speakers", so no name rule could
have found it either.

What does tell them apart is the USB tree: the radio's CI-V bridge and
its CODEC hang off the radio's own hub. So:

- **Linux**: sysfs. /sys/class/tty/<port>/device and
  /sys/class/sound/card<N>/device resolve to paths ending in the USB
  device (`3-1.2.2`, `3-1.2.4`); the hub is the component above it
  (`3-1.2`). PortAudio's ALSA names carry the card (`… (hw:2,0)`).
  Measured on Brian's 9700 on shack: CI-V on 3-1.2.2 and 3-1.2.3, the
  CODEC on 3-1.2.4.
- **Windows**: the PnP tree, read through Windows PowerShell 5.1
  (present on every Windows NB supports, unlike any Python binding) and
  matched in Python (windows_match, below): the COM port's USB device and
  each audio endpoint's, and whether they share a parent hub, or a
  container that is not the computer's own. A root hub never counts:
  every device plugged straight into the computer shares one (seen on
  Brian's desktop, where a webcam "shared a hub" with a Bluetooth COM
  port). MME names are cut at 31 characters, so an endpoint matches when
  its name starts with the MME name. A query like it took about 4.5 s on
  Brian's desktop.
- **macOS, or anything that fails**: no answer. The caller then does not
  guess: with more than one CODEC it asks the operator, and offers the
  tone test (openhamshack-nb ohsnb/rigsetup.py which_codec).

`pair()` is the pure decision and is what the tests drive; the two
look-ups only gather locations.

**Added here: an SDR on the radio's hub** (`sdrs_sharing`). On 18/10/2026
the IC-9700's USB transmit audio failed to decode while a Pluto sat on the
same hub chain; moving the radio to a port of its own did not cure it
(the radio's own codec slips: icom/datasetup.py's warning), but a hub
shared with a device streaming megabytes a second is still the first thing
to rule out, and it costs the operator nothing to be told. So: the hubs
above the radio's sound device, and whether any RTL-SDR, Pluto, SDRplay,
HackRF or Airspy hangs below one of them. A root hub is the computer's own
and does not count, as in `windows_match`. Linux reads sysfs; Windows the
same PnP tree as above; macOS says nothing (no answer, not "no").
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("shack_rig.usbplace")

USB_DEV = re.compile(r"^\d+-[\d.]+$")              # a USB device in sysfs: bus-port.port…
HW = re.compile(r"\(hw:(\d+),\d+\)")
COM = re.compile(r"^COM\d{1,3}$", re.I)
PS_TIMEOUT_S = 20.0


def _norm(name: str) -> str:
    return " ".join(str(name).lower().split())


def _hub_of(path: str) -> str | None:
    """The hub above the last USB device in a resolved sysfs path, or None
    when that device is on a root hub (or there is none)."""
    parts = Path(path).parts
    devs = [i for i, p in enumerate(parts) if USB_DEV.match(p)]
    if not devs:
        return None
    i = devs[-1]
    return parts[i - 1] if i > 0 and USB_DEV.match(parts[i - 1]) else None


def linux_hubs(port: str, names: list[str], sysfs: str = "/sys") -> dict | None:
    """{"port": hub, "audio": {name: hub}} from sysfs, or None if the
    port's hub cannot be found. `sysfs` is for tests. A /dev/serial/by-id
    name resolves to its ttyUSB first."""
    tty = os.path.basename(os.path.realpath(port))
    try:
        port_hub = _hub_of(os.path.realpath(os.path.join(sysfs, "class", "tty", tty, "device")))
    except OSError:
        port_hub = None
    if port_hub is None:
        return None
    audio = {}
    for n in names:
        m = HW.search(n)
        if not m:
            continue
        try:
            audio[n] = _hub_of(os.path.realpath(os.path.join(sysfs, "class", "sound", f"card{m.group(1)}", "device")))
        except OSError:
            audio[n] = None
    return {"port": port_hub, "audio": audio}


# Windows: the PnP tree, gathered by PowerShell and matched here, as the
# standalone IC-9700 Diagnostic 1.1 does it (openhamshack-nb branch
# tools/ic9700-diag, 81b229c, tools/ic9700-diag/ic9700_diag.py), which
# named the right CODEC on Ray's PC: the 9700's hub (VID_0451&PID_2046)
# holds its CP2102N and its PCM2901, the FTDX10's (VID_0424&PID_2512)
# its CP2105 and another. Two things that tool learnt are kept: the
# script goes as -EncodedCommand, because "-Command -" runs stdin line by
# line and a block over several lines silently does nothing; and the
# matching is done in Python, on the raw nodes, so it is tested.
TOPOLOGY_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$devs = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\*' -or $_.Class -eq 'AudioEndpoint' -or $_.Class -eq 'MEDIA' -or $_.Class -eq 'Ports' })
$props = @{}
$devs | Get-PnpDeviceProperty -KeyName 'DEVPKEY_Device_Parent','DEVPKEY_Device_ContainerId' | ForEach-Object {
    if (-not $props.ContainsKey($_.InstanceId)) { $props[$_.InstanceId] = @{} }
    $props[$_.InstanceId][$_.KeyName] = [string]$_.Data
}
$out = foreach ($d in $devs) {
    $p = $props[$d.InstanceId]
    [pscustomobject]@{ id = $d.InstanceId; cls = [string]$d.Class; name = [string]$d.FriendlyName
                       parent = $(if ($p) { $p['DEVPKEY_Device_Parent'] } else { '' })
                       container = $(if ($p) { $p['DEVPKEY_Device_ContainerId'] } else { '' }) }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
"""
PC_CONTAINER = "{00000000-0000-0000-FFFF-FFFFFFFFFFFF}"


def windows_nodes(run=None) -> list[dict]:
    """The present USB, port, media and audio-endpoint devices: [{id, cls,
    name, parent, container}]; empty when PowerShell fails."""
    import base64
    if run is None:
        def run(script):
            enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-EncodedCommand", enc], capture_output=True, text=True, timeout=PS_TIMEOUT_S,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.stdout
    try:
        nodes = json.loads(run(TOPOLOGY_PS) or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        log.warning("reading the USB device tree failed", exc_info=True)
        return []
    return [n for n in (nodes if isinstance(nodes, list) else [nodes]) if isinstance(n, dict)]


def mme_matches(mme: str, friendly: str) -> bool:
    """PortAudio's MME names are the endpoint's friendly name cut to 31
    characters ('IC9700 Speakers (USB Audio CODE')."""
    return friendly == mme or (len(mme) >= 31 and friendly.startswith(mme))


def windows_match(nodes: list[dict], com: str, outputs: list[str]) -> dict:
    """{output name: "hub" | "container"} for the outputs on the same radio
    as COM port `com`. A root hub is no radio's: two devices plugged
    straight into the computer share one."""
    by = {str(n.get("id", "")).upper(): n for n in nodes}

    def up(n):
        return by.get(str(n.get("parent") or "").upper())

    def usb_device(n):
        seen = 0
        while n is not None and seen < 12:
            i = str(n.get("id", "")).upper()
            if i.startswith("USB\\") and "&MI_" not in i:
                return n
            n, seen = up(n), seen + 1
        return None

    def hub_of(n):
        d = usb_device(n)
        h = str(d.get("parent") or "").upper() if d else ""
        return "" if h.startswith("USB\\ROOT_HUB") else h

    def container(n):
        c = str((usb_device(n) or n).get("container") or "").upper()
        return "" if c in ("", PC_CONTAINER) else c

    port = next((n for n in nodes if n.get("cls") == "Ports"
                 and str(n.get("name", "")).upper().endswith(f"({com.upper()})")), None)
    if port is None:
        return {}
    p_hub, p_cont = hub_of(port), container(port)
    found = {}
    endpoints = [n for n in nodes if n.get("cls") == "AudioEndpoint"
                 and str(n.get("id", "")).upper().startswith("SWD\\MMDEVAPI\\{0.0.0.")]
    for name in outputs:
        eps = [e for e in endpoints if mme_matches(name, str(e.get("name", "")))]
        if len(eps) != 1:
            continue
        ep = eps[0]
        parent = up(ep)
        media = [parent] if parent is not None and str(parent.get("id", "")).upper().startswith("USB\\") else \
            [n for n in nodes if n.get("cls") == "MEDIA" and container(n) and container(n) == container(ep)]
        hubs = {hub_of(m) for m in media} - {""}
        how = "hub" if p_hub and p_hub in hubs else ("container" if p_cont and container(ep) == p_cont else "")
        if how:
            found[name] = how
    return found


def pair(codecs: list[str], port_hub: str | None = None, audio_hubs: dict | None = None,
         matched: dict | None = None) -> str | None:
    """The one CODEC on the radio's USB hub, or None if that is not one
    device. Linux gives hubs (`port_hub`, `audio_hubs`), Windows the
    outputs it matched (`matched`, windows_match)."""
    if matched is not None:
        hits = [c for c in codecs if c in matched]
    elif port_hub is not None and audio_hubs is not None:
        hits = [c for c in codecs if audio_hubs.get(c) == port_hub]
    else:
        return None
    return hits[0] if len(hits) == 1 else None


def radio_codec(port: str, codecs: list[str]) -> str | None:
    """The CODEC that is on the same radio as `port`, when the system can
    say (module docstring); None otherwise."""
    if not codecs or not port:
        return None
    try:
        if sys.platform.startswith("linux"):
            found = linux_hubs(port, codecs)
            return pair(codecs, found["port"], found["audio"]) if found else None
        if sys.platform == "win32" and COM.match(port):
            return pair(codecs, matched=windows_match(windows_nodes(), port, codecs))
    except Exception:  # noqa: BLE001 - a look-up that fails is "cannot say", never an error
        log.warning("finding the radio's sound device failed", exc_info=True)
    return None


# -- an SDR on the radio's hub ---------------------------------------------------

#: USB vendor and product ids of the SDRs worth warning about; None as the
#: product matches every product of that vendor (SDRplay makes nothing else).
SDR_IDS = {
    (0x0BDA, 0x2838): "RTL-SDR", (0x0BDA, 0x2832): "RTL-SDR",
    (0x0456, 0xB673): "PlutoSDR", (0x0456, 0xB674): "PlutoSDR",
    (0x1DF7, None): "SDRplay",
    (0x1D50, 0x6089): "HackRF", (0x1D50, 0x60A1): "Airspy",
}


def sdr_name(vid: int, pid: int) -> str | None:
    return SDR_IDS.get((vid, pid)) or SDR_IDS.get((vid, None))


def _hubs_above(path: str) -> list[str]:
    """The USB devices above the last one in a resolved sysfs path - the hubs
    it hangs from, nearest first, the computer's root hub left out."""
    parts = Path(path).parts
    devs = [p for p in parts if USB_DEV.match(p)]
    return list(reversed(devs[:-1]))


def linux_sdrs_sharing(card: int, sysfs: str = "/sys") -> list[str] | None:
    """The SDRs below any hub above sound card `card`; None when the card's
    place on USB cannot be found."""
    try:
        where = os.path.realpath(os.path.join(sysfs, "class", "sound", f"card{card}", "device"))
    except OSError:
        return None
    hubs = set(_hubs_above(where))
    if not any(USB_DEV.match(p) for p in Path(where).parts):
        return None
    found = []
    root = Path(sysfs, "bus", "usb", "devices")
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for dev in entries:
        if not USB_DEV.match(dev.name):
            continue
        try:
            vid = int((dev / "idVendor").read_text().strip(), 16)
            pid = int((dev / "idProduct").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        name = sdr_name(vid, pid)
        if name and hubs & set(_hubs_above(os.path.realpath(dev))):
            found.append(name)
    return found


_VID_PID = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)


def windows_sdrs_sharing(nodes: list[dict], output: str) -> list[str] | None:
    """The SDRs below any hub above the sound device named `output` (an MME
    name, as `windows_match`), from `windows_nodes()`; None when the device
    cannot be placed."""
    by = {str(n.get("id", "")).upper(): n for n in nodes}

    def chain(n) -> list[str]:
        hubs, seen = [], 0
        n = by.get(str(n.get("parent") or "").upper())
        while n is not None and seen < 16:
            i = str(n.get("id", "")).upper()
            if i.startswith("USB\\ROOT_HUB"):
                break
            if i.startswith("USB\\") and "&MI_" not in i:
                hubs.append(i)
            n, seen = by.get(str(n.get("parent") or "").upper()), seen + 1
        return hubs

    eps = [n for n in nodes if n.get("cls") == "AudioEndpoint" and mme_matches(output, str(n.get("name", "")))]
    if len(eps) != 1:
        return None
    ep = eps[0]
    # The endpoint's parent is the codec's USB interface or, on some
    # drivers, a software device; the media node in the same container then
    # stands in, as in windows_match.
    start = by.get(str(ep.get("parent") or "").upper())
    if start is None or not str(start.get("id", "")).upper().startswith("USB\\"):
        cont = str(ep.get("container") or "").upper()
        media = [n for n in nodes if n.get("cls") == "MEDIA" and str(n.get("container") or "").upper() == cont
                 and cont not in ("", PC_CONTAINER)]
        start = media[0] if media else None
    if start is None:
        return None
    own = chain(start)
    # The codec's own USB device is the first of its chain when the start
    # was an interface; a hub is anything above that.
    radio_hubs = set(own[1:]) if own and "&MI_" in str(start.get("id", "")).upper() else set(own)
    found = []
    for n in nodes:
        i = str(n.get("id", "")).upper()
        m = _VID_PID.search(i)
        if not i.startswith("USB\\") or "&MI_" in i or not m:
            continue
        name = sdr_name(int(m.group(1), 16), int(m.group(2), 16))
        if name and radio_hubs & set(chain(n)):
            found.append(name)
    return found


def sdrs_sharing(audio_device: str) -> list[str] | None:
    """The SDRs on a USB hub above the radio's sound device `audio_device`
    (a PortAudio name: ALSA's carries "(hw:N,0)", Windows' is the MME name),
    or None when this system cannot say. Never raises."""
    if not audio_device:
        return None
    try:
        if sys.platform.startswith("linux"):
            m = HW.search(audio_device)
            return linux_sdrs_sharing(int(m.group(1))) if m else None
        if sys.platform == "win32":
            return windows_sdrs_sharing(windows_nodes(), audio_device)
    except Exception:  # noqa: BLE001 - a look-up that fails is "cannot say", never an error
        log.warning("placing the radio's sound device on USB failed", exc_info=True)
    return None
