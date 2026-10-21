"""Where a Pluto is: a host, the port its iiod answers on and the port its
SSH answers on - which need not be the defaults (30431 and 22) when the
Pluto is shared over a network by something in front of it.

Asked for 18/10/2026: the OpenHamShack controller box (ohs-box) shares
Brian's PlutoDVB2 Pluto over the tailnet with `tailscale serve`, iiod at
:30431 and the Pluto's own SSH (dropbear) at :30422, so a Mac - which has
no RNDIS, so no USB network to a Pluto in its default mode - can use it.
iiod needs only the host and port; the transmitter guard (guard.py), the
AD9364 enable and the crystal tools need SSH, on its own port.

Forms taken (`parse`):

- `ip:192.168.2.1` - iiod 30431, SSH 22 (a Pluto on USB or the LAN);
- `ip:host:30431` - iiod on that port (libiio's own form), SSH 22;
- `pluto://host?iiod=30431&ssh=30422` - both said; either may be left out;
- a bare host - as `ip:host`.

`ssh_port=` given to `parse` wins over the URI's. IPv6 literals are not
handled (none of the Plutos or the box is reached that way).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

IIOD_PORT = 30431
SSH_PORT = 22


class EndpointError(ValueError):
    """Not a network address for a Pluto (a USB URI, an empty host)."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    iiod_port: int = IIOD_PORT
    ssh_port: int = SSH_PORT

    @property
    def iio_uri(self) -> str:
        """What libiio's Context takes."""
        return f"ip:{self.host}" if self.iiod_port == IIOD_PORT else f"ip:{self.host}:{self.iiod_port}"

    @property
    def uri(self) -> str:
        """This endpoint written so that `parse` gives it back whole."""
        if self.ssh_port == SSH_PORT:
            return self.iio_uri
        return f"pluto://{self.host}?iiod={self.iiod_port}&ssh={self.ssh_port}"


def _port(text, what: str) -> int:
    try:
        port = int(text)
    except (TypeError, ValueError):
        raise EndpointError(f"{what} port {text!r} is not a number") from None
    if not 0 < port < 65536:
        raise EndpointError(f"{what} port {port} is out of range")
    return port


def parse(uri: str, ssh_port: int | None = None) -> Endpoint:
    uri = (uri or "").strip()
    if uri.startswith(("usb:", "local:", "serial:")):
        raise EndpointError(f"{uri} is not a network address")
    if uri.startswith("pluto://"):
        parts = urlsplit(uri)
        query = parse_qs(parts.query)
        host = parts.hostname or ""
        iiod = _port(query["iiod"][0], "iiod") if "iiod" in query else IIOD_PORT
        ssh = _port(query["ssh"][0], "SSH") if "ssh" in query else SSH_PORT
    else:
        rest = uri[3:] if uri.startswith("ip:") else uri
        host, _, port = rest.partition(":")
        iiod = _port(port, "iiod") if port else IIOD_PORT
        ssh = SSH_PORT
    if not host:
        raise EndpointError(f"{uri!r} has no host")
    return Endpoint(host, iiod, _port(ssh_port, "SSH") if ssh_port is not None else ssh)
