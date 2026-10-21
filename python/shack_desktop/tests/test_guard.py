"""The launch token (guard.py): the decision itself, and each adapter
serving real requests on loopback."""

import http.cookiejar
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from shack_desktop.guard import Guard

TOKEN = "t" * 43


def host(port):
    return {"Host": f"127.0.0.1:{port}"}


def test_no_token_is_refused():
    g = Guard(TOKEN, 8123)
    d = g.decide("GET", "/", "", host(8123))
    assert not d.allowed and d.status == 403


def test_a_wrong_token_is_refused_in_every_form():
    g = Guard(TOKEN, 8123)
    assert g.decide("GET", "/", "shack_token=nope", host(8123)).status == 403
    assert g.decide("GET", "/", "", {**host(8123), "X-Shack-Token": "nope"}).status == 403
    assert g.decide("GET", "/", "", {**host(8123), "Cookie": "shack_desktop_8123=nope"}).status == 403


def test_the_token_in_the_url_becomes_a_cookie_and_leaves_the_address():
    g = Guard(TOKEN, 8123)
    d = g.decide("GET", "/app/", f"tab=scope&shack_token={TOKEN}", host(8123))
    assert not d.allowed and d.status == 303
    h = dict(d.headers)
    assert h["Location"] == "/app/?tab=scope"
    cookie = h["Set-Cookie"]
    assert cookie.startswith(f"shack_desktop_8123={TOKEN};")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie


def test_the_cookie_and_the_header_let_requests_through():
    g = Guard(TOKEN, 8123)
    assert g.decide("POST", "/api/x", "", {**host(8123), "Cookie": f"a=b; shack_desktop_8123={TOKEN}"}).allowed
    assert g.decide("GET", "/", "", {**host(8123), "x-shack-token": TOKEN}).allowed


def test_another_apps_cookie_is_not_ours():
    # Two apps on 127.0.0.1 share one cookie jar in a browser; the port in
    # the name keeps one app's token from opening the other.
    g = Guard(TOKEN, 8123)
    assert g.decide("GET", "/", "", {**host(8123), "Cookie": f"shack_desktop_9000={TOKEN}"}).status == 403


def test_a_foreign_host_is_refused_even_with_the_token():
    # DNS rebinding: the browser sends the attacker's name as Host.
    g = Guard(TOKEN, 8123)
    assert g.decide("GET", "/", "", {"Host": "evil.example:8123", "X-Shack-Token": TOKEN}).status == 403
    assert g.decide("GET", "/", "", {"Host": "127.0.0.1:9999", "X-Shack-Token": TOKEN}).status == 403
    assert g.decide("GET", "/", "", {"X-Shack-Token": TOKEN}).status == 403
    assert g.decide("GET", "/", "", {"Host": "localhost:8123", "X-Shack-Token": TOKEN}).allowed


def test_a_short_token_is_rejected():
    with pytest.raises(ValueError):
        Guard("short", 1)


def browse(url):
    """A browser's view: follows the redirect, keeps the cookie."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener, opener.open(url, timeout=5)


def serve(httpd):
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def exercise(port):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/data", timeout=5)
    assert e.value.code == 403
    opener, r = browse(f"http://127.0.0.1:{port}/data?shack_token={TOKEN}")
    assert r.status == 200 and r.read() == b"ok" and "shack_token" not in r.geturl()
    # Later requests ride on the cookie alone.
    assert opener.open(f"http://127.0.0.1:{port}/other", timeout=5).read() == b"ok"


class _Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass


def test_wsgi_adapter():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]
    holder = {}
    httpd = make_server("127.0.0.1", 0, lambda e, s: holder["app"](e, s), handler_class=_Quiet)
    port = httpd.server_address[1]
    holder["app"] = Guard(TOKEN, port).wsgi(app)
    serve(httpd)
    try:
        exercise(port)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_server_adapter():
    box = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if not box["guard"].check_handler(self):
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    box["guard"] = Guard(TOKEN, httpd.server_address[1])
    serve(httpd)
    try:
        exercise(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_aiohttp_adapter():
    web = pytest.importorskip("aiohttp.web")
    import asyncio
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    ready, stop = threading.Event(), threading.Event()

    def run():
        async def main():
            async def handler(request):
                return web.Response(text="ok")
            app = web.Application(middlewares=[Guard(TOKEN, port).aiohttp_middleware()])
            app.router.add_get("/{tail:.*}", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.SockSite(runner, sock).start()
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.05)
            await runner.cleanup()
        asyncio.run(main())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5)
    try:
        exercise(port)
    finally:
        stop.set()
        t.join(5)
