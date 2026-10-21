"""OpenHamShack Hello's server: static files and one JSON endpoint, on
http.server, behind the launch token (shack_desktop.child)."""
from __future__ import annotations

import json
import mimetypes
import os
import platform
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import shack_desktop

from . import __version__

WWW = Path(__file__).resolve().parent / "static"
STARTED = time.time()


def serve() -> int:
    env = shack_desktop.server_env()
    guard = env.guard

    class Handler(BaseHTTPRequestHandler):
        server_version = "shack-hello"

        def log_message(self, format, *args):   # noqa: A002 - the stdlib's name
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Everything the page uses is served from here (no external
            # assets), and the policy says so to the engine as well.
            # 'unsafe-eval' because pywebview's JS bridge, which carries
            # the window-focus report to the pad, is injected with
            # evaluate_js; without it WebKitGTK refuses the bridge outright
            # (found on the first real-window run). The page itself never
            # evaluates strings.
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-eval'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not guard.check_handler(self):
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/hello":
                info = {"app": "shack-hello", "version": __version__, "python": platform.python_version(),
                        "os": platform.platform(terse=True), "frozen": bool(getattr(sys, "frozen", False)),
                        "pid": os.getpid(), "uptime": round(time.time() - STARTED, 1)}
                self._send(200, json.dumps(info).encode(), "application/json")
                return
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WWW / name).resolve()
            if WWW not in target.parents or not target.is_file():
                self._send(404, b'{"error": "not found"}', "application/json")
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    httpd = shack_desktop.LoopbackHTTPServer((env.host, env.port), Handler)
    shack_desktop.watch_parent(on_gone=httpd.shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0
