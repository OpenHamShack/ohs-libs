"""Finding controllers on the LAN (CONTROLLER-CONTRACT.md §2).

One record type is browsed: `_openhamshack._tcp`, always with TXT
`platform=1`. (Classic, the single-function 0.9.x appliance, was archived
and removed in the family rename of 21/10/2026; nothing discovers it any
more, so there is only one advert shape left to parse.)

The parsing and merging are pure functions, tested without a network.
Browsing needs python-zeroconf (`shack-client[discovery]`, LGPL-2.1); without
it `discover()` returns nothing and says why in `DiscoveryUnavailable`, and
a typed address is always the fallback (§2.2 step 4).
"""
from __future__ import annotations

from ._i18n import _

import time
from dataclasses import dataclass, field

from .transport import HttpTransport, Offline

SERVICE = "_openhamshack._tcp.local."


class DiscoveryUnavailable(Exception):
    pass


@dataclass
class Controller:
    name: str
    id: str                    # the advert's 8-hex `id`; the full controllerId comes from well_known()
    url: str
    kinds: list = field(default_factory=list)
    version: str = ""
    contract: str = ""
    service: str = SERVICE
    address: str = ""
    port: int = 0


def _txt(props: dict) -> dict:
    out = {}
    for k, v in (props or {}).items():
        k = k.decode("ascii", "replace") if isinstance(k, bytes) else str(k)
        if isinstance(v, bytes):
            v = v.decode("ascii", "replace")
        out[k] = "" if v is None else str(v)
    return out


def parse_advert(service_type: str, instance: str, address: str, port: int, props: dict) -> Controller | None:
    """One resolved DNS-SD record as a Controller, or None if it is not
    ours. `instance` is the service instance name (the callsign)."""
    txt = _txt(props)
    st = service_type if service_type.endswith(".") else service_type + "."
    name = txt.get("name") or instance.split("." + st.split(".")[0])[0]
    if not st.startswith("_openhamshack._tcp") or txt.get("platform") != "1":
        return None
    scheme_port = (f"https://{address}:{txt['https']}/" if txt.get("https") else f"http://{address}:{port}/")
    kinds = sorted(k for k in txt.get("kinds", "").split(",") if k)
    return Controller(name, txt.get("id", ""), scheme_port, kinds, txt.get("version", ""), txt.get("contract", ""),
                      SERVICE, address, port)


def merge_adverts(found: list[Controller]) -> list[Controller]:
    """One entry per host and port (a duplicate answer from more than one
    interface collapses to one Controller)."""
    best: dict[tuple, Controller] = {}
    for c in found:
        best[(c.address, c.port)] = c
    return sorted(best.values(), key=lambda c: (c.name, c.url))


def well_known(base_url: str, transport=None) -> dict:
    """GET <base>.well-known/openhamshack, with every relative reference
    in it resolved against the base URL. Raises ValueError if the answer
    is not a controller this library speaks to.

    Resolved against the base, not against the document's own URL as §3
    literally says: by RFC 3986 "api/shack/token" against
    ".../.well-known/openhamshack" is ".../.well-known/api/shack/token",
    which is not where the contract means. The contract text is corrected
    to say so."""
    from .transport import join
    t = transport or HttpTransport(base_url, timeout=5.0)
    status, doc = t.request("GET", ".well-known/openhamshack")
    if status != 200 or "openhamshack" not in doc:
        raise ValueError(_("{address} is not an OpenHamShack controller").format(address=base_url))
    if doc["openhamshack"] != 1:
        raise ValueError(_("This controller is newer than this app: update the app to use it"))
    resolved = dict(doc)
    resolved["auth"] = {k: (join(base_url, v) if k in ("token", "pair") and isinstance(v, str) else v)
                        for k, v in (doc.get("auth") or {}).items()}
    if "base" in (doc.get("services") or {}):
        resolved["services"] = dict(doc["services"], base=join(base_url, doc["services"]["base"]))
    resolved["apps"] = [dict(a, base=join(base_url, a["base"]), **({"icon": join(base_url, a["icon"])} if a.get("icon")
                                                                    else {})) for a in doc.get("apps", [])]
    return resolved


def discover(timeout: float = 3.0) -> list[Controller]:
    """Browse both service types for `timeout` seconds."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        raise DiscoveryUnavailable("Finding controllers on the network needs python-zeroconf "
                                   "(install shack-client[discovery]); type the controller's address instead") from None
    zc = Zeroconf()
    found: list[Controller] = []

    class Listener:
        def add_service(self, z, st, name):
            info = z.get_service_info(st, name, timeout=int(timeout * 1000))
            if info is None:
                return
            for addr in info.parsed_addresses():
                if ":" in addr:
                    continue              # IPv4 only (§2.1)
                c = parse_advert(st, name, addr, info.port, info.properties)
                if c is not None:
                    found.append(c)

        def update_service(self, z, st, name):
            self.add_service(z, st, name)

        def remove_service(self, z, st, name):
            pass

    try:
        ServiceBrowser(zc, [SERVICE], Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return merge_adverts(found)


__all__ = ["Controller", "DiscoveryUnavailable", "Offline", "SERVICE", "discover", "merge_adverts",
           "parse_advert", "well_known"]
