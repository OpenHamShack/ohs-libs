"""The launch token: what makes a loopback server private to its own window.

A web-UI app's desktop form runs its own server on 127.0.0.1 for its own
window (BETA-PLAN arch change 3). Loopback keeps the network out, but not
the other programs on the same machine, nor a web page in the user's
browser that guesses the port. So the shell makes a fresh secret for every
launch (secrets.token_urlsafe, 256 bits) and the server refuses every
request that does not carry it. docs/DESKTOP.md says why this together
satisfies "no servers inside the desktop apps".

The token travels three ways:

1. In the first URL the window opens, `?shack_token=<token>`. The server
   answers that request with a 303 to the same address without the token,
   setting a cookie, so the token leaves the address bar (and anything that
   copies addresses, such as a bookmark or the browser fallback's history
   of later pages) after the first load.
2. As that cookie on every later request: pages, scripts, fetch() and
   WebSocket upgrades, which all send it without the app's code knowing.
   HttpOnly (page scripts never see it) and SameSite=Strict (another site's
   page cannot make the browser send it). Its name carries the port,
   because cookies are scoped by host and not by port: two apps on
   127.0.0.1 open in one browser would otherwise overwrite each other's.
3. As an `X-Shack-Token` header, for the shell's own probes and tests.

The Host header must also name loopback and this port. That stops DNS
rebinding (a hostile page whose name is made to resolve to 127.0.0.1 so
the browser treats our server as its own origin): such a request carries
the hostile name in Host and is refused before the token is even looked
at.

The core, Guard.decide(), knows nothing about any server framework, so the
three adapters below (WSGI, aiohttp, http.server) are a few lines each and
behave identically. An app wires exactly one in; the shell checks that it
did (shell.py probes for a 403 before it opens a window).
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode

from ._i18n import _

QUERY = "shack_token"
HEADER = "X-Shack-Token"
COOKIE_PREFIX = "shack_desktop_"

# The environment the shell gives a server it starts as a separate process
# (child.server_env() reads it). The token is passed in the environment and
# never on the command line: any user can read another's command line
# (ps, /proc/<pid>/cmdline), but a process's environment only its owner.
ENV_HOST = "SHACK_DESKTOP_HOST"
ENV_PORT = "SHACK_DESKTOP_PORT"
ENV_TOKEN = "SHACK_DESKTOP_TOKEN"

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


@dataclass
class Decision:
    """What to do with one request: pass it to the app (allowed), or answer
    it here with status, headers and body."""
    allowed: bool
    status: int = 200
    reason: str = "OK"
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


def _refuse(why: str) -> Decision:
    # Plain text and no detail beyond the reason class: the caller is, by
    # definition, not this app's own window.
    return Decision(False, 403, "Forbidden", [("Content-Type", "text/plain; charset=utf-8"),
                                             ("Cache-Control", "no-store")], why.encode())


class Guard:
    """The per-launch token check. `port` is the server's port (it names
    the cookie and is what Host must say); without it Host is not checked,
    which only tests that build a Guard by hand should rely on."""

    def __init__(self, token: str, port: int | None = None):
        if not token or len(token) < 16:
            raise ValueError(_("A launch token must be at least 16 characters"))
        self.token = token
        self.port = port
        self.cookie = f"{COOKIE_PREFIX}{port}" if port else f"{COOKIE_PREFIX}0"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Guard":
        env = os.environ if environ is None else environ
        token, port = env.get(ENV_TOKEN), env.get(ENV_PORT)
        if not token or not port:
            raise RuntimeError(f"{ENV_TOKEN} and {ENV_PORT} are not set: this server was not started by shack_desktop")
        return cls(token, int(port))

    def _ok(self, candidate: str | None) -> bool:
        # compare_digest: no timing signal about how much of a guess was right.
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    def _host_ok(self, host: str | None) -> bool:
        if self.port is None:
            return True
        if not host:
            return False
        return host.lower() in {f"{name}:{self.port}" for name in LOOPBACK_NAMES}

    def decide(self, method: str, path: str, query: str, headers: Mapping[str, str]) -> Decision:
        """`headers` is matched case-insensitively; `query` is the raw query
        string without "?"."""
        h = {k.lower(): v for k, v in headers.items()}
        if not self._host_ok(h.get("host")):
            return _refuse("not this window's address\n")
        pairs = parse_qsl(query, keep_blank_values=True)
        offered = [v for k, v in pairs if k == QUERY]
        if offered:
            if not self._ok(offered[-1]):
                return _refuse("wrong launch token\n")
            if method in ("GET", "HEAD"):
                rest = urlencode([(k, v) for k, v in pairs if k != QUERY])
                # A relative Location: the app may sit behind a path, and
                # the address it was opened at is the one it stays on.
                where = (path or "/") + (f"?{rest}" if rest else "")
                return Decision(False, 303, "See Other", [
                    ("Location", where),
                    ("Set-Cookie", f"{self.cookie}={self.token}; Path=/; HttpOnly; SameSite=Strict"),
                    ("Cache-Control", "no-store"),
                    ("Referrer-Policy", "no-referrer"),
                    ("Content-Length", "0"),
                ])
            return Decision(True)
        if self._ok(h.get(HEADER.lower())):
            return Decision(True)
        jar = SimpleCookie()
        try:
            jar.load(h.get("cookie", ""))
        except CookieError:
            return _refuse("no launch token\n")
        morsel = jar.get(self.cookie)
        if morsel is not None and self._ok(morsel.value):
            return Decision(True)
        return _refuse("no launch token\n")

    # ------------------------------------------------------------- adapters

    def wsgi(self, app: Callable) -> Callable:
        """WSGI middleware: `application = guard.wsgi(application)`."""
        def guarded(environ: dict, start_response: Callable) -> Iterable[bytes]:
            headers = {k[5:].replace("_", "-"): v for k, v in environ.items() if k.startswith("HTTP_")}
            d = self.decide(environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/"),
                            environ.get("QUERY_STRING", ""), headers)
            if d.allowed:
                return app(environ, start_response)
            start_response(f"{d.status} {d.reason}", d.headers)
            return [d.body]
        return guarded

    def aiohttp_middleware(self) -> Any:
        """`web.Application(middlewares=[guard.aiohttp_middleware()])`.
        aiohttp is imported here, not at module load: the library itself
        stays stdlib-only."""
        from aiohttp import web

        @web.middleware
        async def shack_guard(request: Any, handler: Callable) -> Any:
            d = self.decide(request.method, request.path, request.query_string, request.headers)
            if d.allowed:
                return await handler(request)
            return web.Response(status=d.status, reason=d.reason, body=d.body, headers=d.headers)
        return shack_guard

    def check_handler(self, handler: Any) -> bool:
        """For http.server: call first in every do_GET/do_POST/...; when it
        returns False it has already answered, so return at once.

            def do_GET(self):
                if not guard.check_handler(self):
                    return
        """
        path, _, query = handler.path.partition("?")
        d = self.decide(handler.command, path, query, dict(handler.headers.items()))
        if d.allowed:
            return True
        handler.send_response(d.status, d.reason)
        for k, v in d.headers:
            handler.send_header(k, v)
        if not any(k.lower() == "content-length" for k, _ in d.headers):
            handler.send_header("Content-Length", str(len(d.body)))
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(d.body)
        return False
