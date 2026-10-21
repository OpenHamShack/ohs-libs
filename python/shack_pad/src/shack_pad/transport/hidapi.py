"""The hidapi package (pip `hidapi`, module `hid`): the pad on Windows and macOS.

Which of the device's HID entries is the pad. hidapi lists one entry per
top-level collection, and the AKP03E shows (at least) two faces: its
vendor interface, which speaks the CRT/ACK protocol, and a keyboard face
that must never be opened as the pad. On Linux hidraw tells them apart by
interface (input0); on Windows an interface can carry several collections,
each its own path (`...&mi_00&col01#...`), and macOS may report no
interface number at all. So the vendor collection is chosen by its usage:
usage page 0xFFA0, usage 1, as the open-source Mirabox/Ajazz drivers do
(4ndv/mirajazz + opendeck-akp03 src/mappings.rs, "Map all queries to usage
page 65440 and usage id 1", every AKP03 variant including 0300:3002).
Interface 0 that is not a keyboard is the fallback, for a backend that
reports no usages (libusb, very old hidapi) - which is what this module
did alone before 12/10/2026, unmeasured on Windows.

Still UNTESTED on a real pad under Windows and macOS (CONTROLLER-PAD.md
§8). Assumed, from hidapi's source rather than measured:

- Input reports come without the report id on every platform: Windows'
  ReadFile prepends a 0 for a device with unnumbered reports and hidapi
  strips it, so the driver's ACK test sees the same bytes as on hidraw.
- Writes carry the report id first (0: the AKP03E has no numbered reports),
  the same bytes the hidraw path writes. Windows insists the length is the
  output report length plus one; 1 + 1024 is that for the AKP03E rev. 2
  (mirajazz: protocol 3, 1024-byte packets). A pad whose output report is
  another size refuses every write, and the diagnostics report says so
  (shack_pad.diagnose reads the report descriptor).
- Windows shares HID devices: opening the pad does not fail while the
  Ajazz/Mirabox "Stream Dock" software has it open, both simply fight over
  the screens. shack_pad.diagnose looks for that software instead.
- macOS may ask for Input Monitoring permission the first time [guess].

hid is imported when it is needed, not at import time, so the core library
works without the package.
"""

from __future__ import annotations

VENDOR_USAGE_PAGE = 0xFFA0
VENDOR_USAGE = 1
# Usage pages that are never the pad: generic desktop (the keyboard face,
# a mouse) and consumer control (media keys some firmwares add).
NOT_THE_PAD = (0x01, 0x0C)


def _hid():
    import hid  # noqa: PLC0415  (optional dependency: shack-pad[hid])
    return hid


def library_version() -> str | None:
    """hidapi's version ("0.15.0"), or None when the package is missing."""
    try:
        hid = _hid()
    except ImportError:
        return None
    v = getattr(hid, "version_str", None)
    try:
        return str(v() if callable(v) else v or getattr(hid, "__version__", "") or "unknown")
    except Exception:       # a C library that will not say is still a library
        return "unknown"


def path_text(path) -> str:
    return path.decode(errors="replace") if isinstance(path, bytes) else str(path)


def choose(entries: list[dict], interface: int = 0) -> list[dict]:
    """The entries (as hid.enumerate gives them, for one vendor/product)
    that are the pad's vendor collection, best first. Pure, so the tests
    run it on enumerations shaped like each platform's."""
    vendor = [e for e in entries if e.get("usage_page") == VENDOR_USAGE_PAGE]
    if vendor:
        exact = [e for e in vendor if e.get("usage") == VENDOR_USAGE]
        picked = exact or vendor
    else:
        # No usages reported: fall back on the interface number, never a
        # collection whose usage says it is a keyboard or media keys.
        picked = []
        for e in entries:
            if e.get("usage_page") in NOT_THE_PAD:
                continue
            n = e.get("interface_number", -1)
            if n == interface or n == -1:
                picked.append(e)
    seen, out = set(), []
    for e in picked:
        p = path_text(e.get("path"))
        if p not in seen:
            seen.add(p)
            out.append(e)
    return out


def enumerate_devices(vendor: int = 0, product: int = 0) -> list[dict]:
    """hid.enumerate(vendor, product), or [] without the package."""
    try:
        hid = _hid()
    except ImportError:
        return []
    return list(hid.enumerate(vendor, product))


def find(vendor: int, product: int, interface: int = 0) -> list[str]:
    """hidapi paths (opaque, per OS) of the vendor collection of each device."""
    return [path_text(e.get("path")) for e in choose(enumerate_devices(vendor, product), interface)]


class HidapiTransport:
    def __init__(self, path: str):
        hid = _hid()
        self.path = path
        self.dev = hid.device()
        try:
            self.dev.open_path(path.encode())
        except (OSError, ValueError) as exc:
            # hidapi says only "open failed"; its error() has the system's
            # words ("Access is denied."), which diagnose.explain() reads.
            why = _last_error(self.dev)
            self.dev = None
            raise OSError(f"{exc}: {why}" if why else str(exc)) from exc

    def report_descriptor(self) -> bytes | None:
        try:
            return bytes(self.dev.get_report_descriptor()) if self.dev is not None else None
        except Exception:       # hidapi before 0.14, or a backend without it
            return None

    def read(self, timeout: float) -> bytes | None:
        if self.dev is None:
            raise OSError("closed")
        try:
            data = self.dev.read(1024, max(1, int(timeout * 1000)))
        except (OSError, ValueError) as exc:     # hidapi raises IOError("read error") when unplugged
            raise OSError(str(exc)) from exc
        return bytes(data) if data else None

    def write(self, data: bytes) -> None:
        if self.dev is None:
            raise OSError("closed")
        try:
            n = self.dev.write(data)
        except (OSError, ValueError) as exc:
            why = _last_error(self.dev)
            raise OSError(f"{exc}: {why}" if why else str(exc)) from exc
        if n is not None and n < 0:
            raise OSError(f"write failed: {_last_error(self.dev) or 'no reason given'}")

    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None


def _last_error(dev) -> str:
    try:
        e = dev.error() if dev is not None and hasattr(dev, "error") else ""
    except Exception:
        return ""
    return str(e or "").strip()
