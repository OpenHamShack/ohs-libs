"""A stand-in for pywebview, for the shell's subprocess tests.

It behaves as a window would, without a display: start() loads the URL
the shell gives it, as the webview would (following the token redirect and
keeping the cookie), reports focus through the JS bridge, and then stays
"open" until the test creates the close file or the shell calls destroy().
Everything it sees and does goes to $FAKE_WEBVIEW_LOG as JSON lines.
"""
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request

_windows = []


def _log(**event):
    with open(os.environ["FAKE_WEBVIEW_LOG"], "a") as f:
        f.write(json.dumps(event) + "\n")


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in self.handlers:
            fn()


class _Events:
    def __init__(self):
        self.loaded = _Event()


class Window:
    def __init__(self, title, url, js_api=None, **kw):
        self.title, self.url, self.js_api = title, url, js_api
        self.events = _Events()
        self.destroyed = False
        self._on_top = False
        _log(call="create_window", title=title, url=url, kw={k: str(v) for k, v in kw.items()})

    def evaluate_js(self, script):
        _log(call="evaluate_js", focus_script="shack_focus" in script)

    def restore(self):
        _log(call="restore")

    def show(self):
        _log(call="show")

    @property
    def on_top(self):
        return self._on_top

    @on_top.setter
    def on_top(self, value):
        self._on_top = value
        _log(call="on_top", value=value)

    def destroy(self):
        _log(call="destroy")
        self.destroyed = True


def create_window(title, url, **kw):
    w = Window(title, url, **kw)
    _windows.append(w)
    return w


def _load(url):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        r = opener.open(url, timeout=5)
        status, final, body = r.status, r.geturl(), r.read().decode()
    except urllib.error.HTTPError as e:
        status, final, body = e.code, url, e.read().decode()
    return status, final, body, [c.name for c in jar]


def start(gui=None, private_mode=None, icon=None, **kw):
    w = _windows[-1]
    status, final, body, cookies = _load(w.url)
    _log(call="start", gui=gui, private_mode=private_mode, icon=icon, pid=os.getpid(),
         status=status, final_url=final, body=body, cookies=cookies)
    w.events.loaded.fire()
    if w.js_api is not None:
        w.js_api.shack_focus(True)
        w.js_api.shack_focus(False)
    close = os.environ.get("FAKE_WEBVIEW_CLOSE")
    while not w.destroyed and not (close and os.path.exists(close)):
        time.sleep(0.05)
    _log(call="closed")
