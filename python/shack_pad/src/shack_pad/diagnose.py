"""Why the pad does or does not work, in words the operator can act on.

The first people to plug an AKP03E into a desktop app are remote: nobody
who wrote this can see their computer, and a pad that silently does
nothing is the worst outcome. So everything the library can find out is
gathered here, as data an app's settings page shows and as one plain-text
report the operator can copy into an email:

- survey(): which Ajazz/Mirabox devices are plugged in, each HID interface
  with its usage page and the one the driver would open, whether this
  installation has what the pad needs (hidapi on Windows and macOS, Pillow
  for the key pictures, a system font), Linux permissions on the hidraw
  node, the report sizes the device declares, and whether the maker's own
  software is running - then the problems found, as sentences.
- explain(exc): an OSError from opening, reading or writing the pad, as a
  sentence that says what to do ("close the Ajazz or Mirabox software"),
  rather than hidapi's "open failed".
- report(): the survey and an app's own lines as text. No secrets: device
  paths, ids and versions only; serial numbers are left out.

Nothing here writes to the pad or changes anything. Reading the report
descriptor is the only open: sysfs on Linux, a shared hidapi handle on
Windows and macOS (Windows lets several programs open one HID device).

Where a healthy Windows enumeration's shape comes from: hidapi's
windows/hid.c (one entry per top-level collection, the interface number
parsed from the path's MI_xx), and the Mirabox/Ajazz drivers named in
transport/hidapi.py.
"""

from __future__ import annotations

import errno
import os
import platform
import subprocess
import sys
from pathlib import Path

from . import faces
from .i18n import _
from .transport import hidapi

# The model this library drives, and the relatives an operator may own by
# mistake: same family, different protocol or layout, never measured. The
# first-revision AKP03E (0300:1002) in particular sends no key releases
# (mirajazz: protocol 2), so a held PTT could never end - it must stay
# refused until it is measured. A relative is named in the report and
# refused in words, never silently ignored. Ids from opendeck-akp03
# src/mappings.rs (read 12/10/2026).
SUPPORTED = {(0x0300, 0x3002): ("Ajazz AKP03E (rev. 2)", "ajazz-akp03e")}
RELATIVES = {
    (0x0300, 0x1001): "Ajazz AKP03",
    (0x0300, 0x1002): "Ajazz AKP03E (first revision)",
    (0x0300, 0x1003): "Ajazz AKP03R",
    (0x0300, 0x3003): "Ajazz AKP03R (rev. 2)",
    (0x6602, 0x1000): "Mirabox N3",
    (0x6602, 0x1002): "Mirabox N3",
    (0x6603, 0x1002): "Mirabox N3",
    (0x6603, 0x1003): "Mirabox N3",
}
# Vendor ids worth listing whatever the product: Ajazz's and Mirabox's.
FAMILY_VENDORS = (0x0300, 0x6602, 0x6603)
OUTPUT_REPORT = 1024
# Process names (lower case, substrings) of software that drives these pads
# itself: the makers' "Stream Dock" apps, OpenDeck. Matched loosely on
# purpose: a false "may be using it" costs a sentence, a missed one costs a
# pad whose keys flicker between two programs' pictures. Windows does not
# refuse the second program's open, so this is the only warning there is.
VENDOR_SOFTWARE = ("streamdock", "stream dock", "stream_dock", "mirabox", "ajazz", "opendeck", "hotspotek")


# --- Report descriptors --------------------------------------------------------------
def parse_descriptor(desc: bytes) -> dict:
    """Usage pages and report lengths (bytes, without the report id) from a
    HID report descriptor: {"usage_pages": [int], "input": {id: n},
    "output": {id: n}, "feature": {id: n}}. Short items only; enough to say
    whether the pad wants the 1024-byte reports the driver writes."""
    out: dict = {"usage_pages": [], "input": {}, "output": {}, "feature": {}}
    g = {"page": 0, "size": 0, "count": 0, "id": 0}
    stack: list[dict] = []
    bits: dict[str, dict[int, int]] = {"input": {}, "output": {}, "feature": {}}
    i = 0
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:                      # a long item: skip it whole
            if i + 1 >= len(desc):
                break
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        data = int.from_bytes(desc[i + 1:i + 1 + size], "little")
        i += 1 + size
        if typ == 1:                       # global items
            if tag == 0:
                g["page"] = data
                if data not in out["usage_pages"]:
                    out["usage_pages"].append(data)
            elif tag == 7:
                g["size"] = data
            elif tag == 8:
                g["id"] = data
            elif tag == 9:
                g["count"] = data
            elif tag == 10:
                stack.append(dict(g))
            elif tag == 11 and stack:
                g = stack.pop()
        elif typ == 0 and tag in (8, 9, 11):   # Input, Output, Feature
            kind = {8: "input", 9: "output", 11: "feature"}[tag]
            bits[kind][g["id"]] = bits[kind].get(g["id"], 0) + g["size"] * g["count"]
    for kind, per in bits.items():
        out[kind] = {rid: (n + 7) // 8 for rid, n in per.items()}
    return out


def _sysfs_descriptor(node: Path) -> bytes | None:
    try:
        return (node / "device" / "report_descriptor").read_bytes()
    except OSError:
        return None


def _hidapi_descriptor(path: str) -> bytes | None:
    try:
        t = hidapi.HidapiTransport(path)
    except Exception:
        return None
    try:
        return t.report_descriptor()
    finally:
        t.close()


# --- What is plugged in --------------------------------------------------------------
def _name(vid: int, pid: int) -> tuple[str, bool]:
    if (vid, pid) in SUPPORTED:
        return SUPPORTED[(vid, pid)][0], True
    return RELATIVES.get((vid, pid), "unknown Ajazz/Mirabox device"), False


def _ours(vid: int, pid: int) -> bool:
    return (vid, pid) in SUPPORTED or (vid, pid) in RELATIVES or vid in FAMILY_VENDORS


def _linux_devices(sysfs: Path, dev: Path, descriptors: bool) -> list[dict]:
    """Every hidraw node of a family device, grouped by device."""
    try:
        nodes = sorted(sysfs.iterdir())
    except OSError:
        return []
    by_dev: dict[tuple, dict] = {}
    for node in nodes:
        try:
            uevent = (node / "device" / "uevent").read_text()
        except OSError:
            continue
        kv = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        try:
            _bus, v, p = kv.get("HID_ID", "").split(":")
            vid, pid = int(v, 16), int(p, 16)
        except ValueError:
            continue
        if not _ours(vid, pid):
            continue
        phys = kv.get("HID_PHYS", "")
        name, supported = _name(vid, pid)
        d = by_dev.setdefault((vid, pid, phys.rsplit("/", 1)[0]),
                              {"vid": vid, "pid": pid, "name": name, "supported": supported,
                               "product": kv.get("HID_NAME", ""), "interfaces": []})
        path = str(dev / node.name)
        iface = {"path": path, "interface": phys.rsplit("/", 1)[-1] if "/" in phys else "",
                 "access": os.access(path, os.R_OK | os.W_OK)}
        iface["chosen"] = supported and iface["interface"] == "input0"     # as AKP03E.find on Linux
        desc = _sysfs_descriptor(node) if descriptors else None
        if desc:
            iface["descriptor"] = parse_descriptor(desc)
        d["interfaces"].append(iface)
    return list(by_dev.values())


def _hidapi_devices(entries: list[dict], descriptors: bool) -> list[dict]:
    by_dev: dict[tuple, dict] = {}
    raw: dict[tuple, list[dict]] = {}
    for e in entries:
        vid, pid = e.get("vendor_id", 0), e.get("product_id", 0)
        if not _ours(vid, pid):
            continue
        name, supported = _name(vid, pid)
        d = by_dev.setdefault((vid, pid), {"vid": vid, "pid": pid, "name": name, "supported": supported,
                                           "product": e.get("product_string") or "", "interfaces": []})
        raw.setdefault((vid, pid), []).append(e)
        d["interfaces"].append({"path": hidapi.path_text(e.get("path")),
                                "interface": e.get("interface_number", -1),
                                "usage_page": e.get("usage_page", 0) or 0, "usage": e.get("usage", 0) or 0})
    for key, d in by_dev.items():
        # The same choice AKP03E.find makes, so the report names the
        # interface the driver really opens.
        chosen = {hidapi.path_text(e.get("path")) for e in hidapi.choose(raw[key])} if d["supported"] else set()
        for iface in d["interfaces"]:
            iface["chosen"] = iface["path"] in chosen
            if descriptors and iface["chosen"]:
                desc = _hidapi_descriptor(iface["path"])
                if desc:
                    iface["descriptor"] = parse_descriptor(desc)
    return list(by_dev.values())


def vendor_software(plat: str | None = None, runner=subprocess.run, proc: Path = Path("/proc")) -> list[str]:
    """Names of running programs that look like a pad maker's software."""
    plat = plat or sys.platform
    names: list[str] = []
    try:
        if plat == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)    # no console flash from a windowed app
            r = runner(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10,
                       creationflags=flags)
            names = [line.split('","', 1)[0].strip('"') for line in (r.stdout or "").splitlines() if line.strip()]
        elif plat.startswith("linux"):
            for p in proc.iterdir():
                if p.name.isdigit():
                    try:
                        names.append((p / "comm").read_text().strip())
                    except OSError:
                        pass
        else:
            r = runner(["ps", "-axo", "comm"], capture_output=True, text=True, timeout=10)
            names = [Path(line.strip()).name for line in (r.stdout or "").splitlines()[1:]]
    except Exception:
        return []
    return sorted({n for n in names if any(s in n.lower() for s in VENDOR_SOFTWARE)})


def libraries() -> dict:
    """What this installation has for the pad."""
    try:
        import PIL  # noqa: PLC0415
        pillow = getattr(PIL, "__version__", "unknown")
    except ImportError:
        pillow = None
    from . import __version__  # noqa: PLC0415
    return {"shack_pad": __version__, "hidapi": hidapi.library_version(), "pillow": pillow,
            "font": faces.font_path()}


def describe_libraries(plat: str | None = None) -> str:
    """One line for an app's start-up facts. A frozen build that lost hidapi
    or Pillow says so here (NB's Windows build's smoke test reads it)."""
    plat = plat or sys.platform
    lib = libraries()
    if plat.startswith("linux"):
        hid = "hidraw" + (f" (hidapi {lib['hidapi']} also installed)" if lib["hidapi"] else "")
    else:
        hid = f"hidapi {lib['hidapi']}" if lib["hidapi"] else "hidapi not installed"
    pil = f"Pillow {lib['pillow']}" if lib["pillow"] else "Pillow not installed"
    return f"shack_pad {lib['shack_pad']}, {hid}, {pil}, font {lib['font'] or 'built-in'}"


def survey(plat: str | None = None, entries: list[dict] | None = None, sysfs: Path = Path("/sys/class/hidraw"),
           dev: Path = Path("/dev"), descriptors: bool = True, software: list[str] | None = None) -> dict:
    """Everything above, and the problems it adds up to. `entries` stands
    in for hid.enumerate(0, 0) and `software` for vendor_software() in
    tests; by default both are asked."""
    plat = plat or sys.platform
    linux = plat.startswith("linux")
    lib = libraries()
    if linux:
        devices = _linux_devices(sysfs, dev, descriptors)
    else:
        devices = _hidapi_devices(hidapi.enumerate_devices(0, 0) if entries is None else entries, descriptors)
    if software is None:
        software = vendor_software(plat)
    problems: list[str] = []
    supported = [d for d in devices if d["supported"]]
    if not linux and not lib["hidapi"]:
        problems.append(_("This installation is missing the hidapi library, so the controller cannot be reached. "
                          "Reinstall the app; if that does not help, send this report."))
    for d in devices:
        if not d["supported"]:
            problems.append(_("Found {name} (USB {vid:04x}:{pid:04x}). Only the Ajazz AKP03E rev. 2 (USB 0300:3002) "
                              "works with this app so far: send this report so support can be added.")
                            .format(name=d["name"], vid=d["vid"], pid=d["pid"]))
    if not devices and (linux or lib["hidapi"]):
        problems.append(_("No Ajazz or Mirabox controller is plugged in (looked for USB 0300:3002). Check the "
                          "cable - a data cable, not a charge-only one - and try another USB port."))
    for d in supported:
        chosen = [i for i in d["interfaces"] if i.get("chosen")]
        if not chosen:
            problems.append(_("The {name} is plugged in, but its control interface is not where it was expected: "
                              "send this report.").format(name=d["name"]))
        for i in chosen:
            if linux and not i.get("access"):
                problems.append(_("This computer does not let the app open the controller ({path}: no permission). "
                                  "The app's udev rule is missing: reinstall the package, or see the guide's "
                                  "controller section; then unplug the controller and plug it in again.")
                                .format(path=i["path"]))
            out_len = (i.get("descriptor") or {}).get("output", {})
            if out_len and OUTPUT_REPORT not in out_len.values():
                problems.append(_("The controller's output report is {n} bytes, not the {want} this app sends: it "
                                  "is a different revision. Send this report.")
                                .format(n=", ".join(str(v) for v in out_len.values()), want=OUTPUT_REPORT))
    if software:
        problems.append(_("{names} is running and may be using the controller. Close it (also from the system "
                          "tray, by the clock), then unplug the controller and plug it in again.")
                        .format(names=", ".join(software)))
    if not lib["pillow"]:
        problems.append(_("The Pillow library is missing: the keys work, but show no pictures."))
    chosen_path = next((i["path"] for d in supported for i in d["interfaces"] if i.get("chosen")), None)
    return {"platform": plat, "os": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
            "libraries": lib, "devices": devices, "chosen": chosen_path, "software": software,
            "problems": problems}


# --- Errors in words -----------------------------------------------------------------
def explain(exc: BaseException, plat: str | None = None, doing: str = "open") -> str:
    """An OSError from the pad as a sentence that says what to do.
    doing: "open", "read" or "write"."""
    plat = plat or sys.platform
    text = str(exc)
    low = text.lower()
    code = getattr(exc, "errno", None)
    if isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM):
        if plat.startswith("linux"):
            return _("This computer does not let the app open the controller (no permission). The app's udev rule "
                     "is missing: reinstall the package, or see the guide's controller section; then unplug the "
                     "controller and plug it in again.")
        return _("The system refused access to the controller. Another program probably has it: close the Ajazz or "
                 "Mirabox Stream Dock software (also from the system tray), then unplug the controller and plug it "
                 "in again.")
    if code == errno.EBUSY or "busy" in low or "access is denied" in low or "being used by another" in low:
        return _("Another program has the controller open. Close the Ajazz or Mirabox Stream Dock software (also "
                 "from the system tray), OpenDeck, or another OpenHamShack app's window, then unplug the controller "
                 "and plug it in again.")
    gone = code in (errno.ENODEV, errno.ENXIO, errno.EIO) or any(
        s in low for s in ("no such device", "read error", "not connected", "device gone", "closed"))
    if gone and doing != "open":
        return _("The controller was unplugged, or its USB connection reset. Plug it in again; nothing transmits "
                 "while it is away.")
    if doing == "write":
        return _("The controller would not take a picture for its keys ({why}). If the Ajazz or Mirabox software "
                 "is running, close it; otherwise send the controller report - this may be a different revision of "
                 "the pad.").format(why=text)
    if doing == "open":
        if plat == "win32":
            return _("Windows would not open the controller ({why}). Usually another program has it: close the Ajazz "
                     "or Mirabox Stream Dock software (also from the system tray), then unplug the controller and "
                     "plug it in again. If it still fails, send the controller report.").format(why=text)
        return _("The controller could not be opened ({why}). Unplug it and plug it in again; if it still fails, "
                 "send the controller report.").format(why=text)
    return _("The controller stopped working ({why}). Unplug it and plug it in again; if it keeps happening, send "
             "the controller report.").format(why=text)


# --- The report ------------------------------------------------------------------------
def report(s: dict, extra: list[str] | None = None) -> str:
    """The survey as text to paste into an email. `extra`: the app's own
    lines (its name and version, the pad's state, its last errors)."""
    lib = s["libraries"]
    lines = ["OpenHamShack controller report", ""]
    if extra:
        lines += list(extra) + [""]
    lines += [f"os: {s['os']} ({s['platform']}), python {s['python']}",
              f"shack_pad: {lib['shack_pad']}",
              f"hidapi: {lib['hidapi'] or 'not installed'}",
              f"pillow: {lib['pillow'] or 'not installed'}",
              f"font: {lib['font'] or 'built-in'}",
              f"maker's software running: {', '.join(s['software']) or 'none seen'}",
              f"will open: {s['chosen'] or 'nothing'}", ""]
    if not s["devices"]:
        lines.append("devices: no Ajazz/Mirabox device found")
    for d in s["devices"]:
        lines.append(f"device: {d['name']}, USB {d['vid']:04x}:{d['pid']:04x}, \"{d['product']}\", "
                     f"{'supported' if d['supported'] else 'NOT supported'}")
        for i in d["interfaces"]:
            bits = [f"  interface {i.get('interface')}"]
            if "usage_page" in i:
                bits.append(f"usage {i['usage_page']:04x}:{i['usage']:04x}")
            if "access" in i:
                bits.append("read/write ok" if i["access"] else "NO ACCESS")
            if i.get("chosen"):
                bits.append("<- the pad")
            desc = i.get("descriptor")
            if desc:
                bits.append("pages " + ",".join(f"{p:04x}" for p in desc["usage_pages"]))
                for kind in ("input", "output", "feature"):
                    if desc[kind]:
                        bits.append(f"{kind} " + ",".join(f"{rid}:{n}" for rid, n in sorted(desc[kind].items())))
            bits.append(i["path"])
            lines.append(" ".join(bits))
    lines.append("")
    lines.append("problems:" if s["problems"] else "problems: none found")
    lines += [f"- {p}" for p in s["problems"]]
    return "\n".join(lines) + "\n"
