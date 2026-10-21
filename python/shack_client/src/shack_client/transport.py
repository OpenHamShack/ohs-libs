"""HTTP to a controller, stdlib only (SYNC.md §9.1: no third-party
dependency in the core, so Qt apps, servers and PyInstaller bundles take
it without baggage).

Every URL is resolved against the controller's base URL as a relative
reference, never built absolute (CONTROLLER-CONTRACT.md §3; the "never
build an absolute URL" rule): the same controller is reached under a path
prefix on one address and at the root of another.

`request()` answers (status, JSON body) for any HTTP status, and raises
`Offline` only when no answer came at all - refused, unreachable, timed
out, or the connection dropped mid-answer - which is the one case the
sync loop treats as "the controller is away" rather than "the controller
said no".
"""
from __future__ import annotations

from ._i18n import _

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

TIMEOUT_S = 10.0     # §6.3: request timeout 10 s


class Offline(Exception):
    """No answer from the controller."""


def join(base_url: str, path: str, query: dict | None = None) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    url = urllib.parse.urljoin(base, path.lstrip("/"))
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    return url


class HttpTransport:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = TIMEOUT_S):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(join(self.base_url, path, query), data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read())
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, HTTPException, OSError) as e:
            raise Offline(str(e)) from None


def _json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return {"error": _("The controller answered with something that is not JSON."), "code": "bad_response"}
    return v if isinstance(v, dict) else {"value": v}
