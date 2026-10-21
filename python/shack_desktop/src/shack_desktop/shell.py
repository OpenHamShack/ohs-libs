"""The desktop shell: a web-UI app's own server, shown in a native window.

BETA-PLAN arch change 3: QO-100 WB, Icom, Flex and Digital Modes are web
apps, and their desktop form is "the same app server, run locally, inside
a native window ... or open in the default browser if a webview isn't
viable". run() is that, for any of them:

1. One copy per app (instance.py): a second launch raises the first.
2. The app's server starts on 127.0.0.1, on a port the OS picks, with a
   fresh launch token it must require (guard.py). Either in this process
   (run(app_factory), a WSGI app served from a thread) or as a separate
   process (run(command=[...]), any framework; child.py is its half).
3. Before any window opens, the shell checks the server really is private:
   a request without the token must get 403, and the port must not answer
   on the machine's network address. An app that forgot to wire the guard
   in, or bound 0.0.0.0, refuses to start rather than quietly serving the
   LAN.
4. The window is pywebview with the OS's own engine (WebKitGTK, WebView2,
   WKWebView), private_mode so nothing persists between launches. Never Qt
   (QtWebEngine is over 100 MB, PACKAGING.md), and never pywebview's
   Internet Explorer fallback on a Windows without WebView2: a modern web
   UI does not work in it, and the browser fallback does.
5. Without a usable engine: the default browser, plus a small window (Tk)
   with "Open again" and "Quit"; on a Windows Python without Tk, a message
   box (Retry/Cancel); in a terminal without either, Ctrl+C.
6. When the window closes, the server stops: the in-process server is shut
   down; a child is asked (its stdin closes), then terminated, then killed.

The pad (shack_pad, CONTROLLER-PAD.md §6.2) follows the window's focus when
the app hands run() its DesktopPad and shack_pad is installed.

SHACK_DESKTOP_UI=browser always uses the browser (a user whose webview
misbehaves); =webview skips the engine probe (diagnosis, and the tests'
stand-in webview).
"""
from __future__ import annotations

import errno
import http.client
import importlib.util
import logging
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from typing import Any, Callable, Sequence
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ._i18n import _
from .child import ENV_PARENT
from .guard import ENV_HOST, ENV_PORT, ENV_TOKEN, QUERY, Guard
from .instance import SingleInstance
from .paths import data_dir

log = logging.getLogger("shack_desktop")

LOOPBACK = "127.0.0.1"
EXIT_OK, EXIT_SERVER = 0, 3

# Runs in the page after every load: window focus and blur, reported to the
# shell through pywebview's JS bridge. pywebview has no focus event of its
# own on any backend, and the page's window events are what the native
# window's activation drives on all three engines.
FOCUS_JS = """
(function () {
  if (window.__shackFocus) { return; }
  window.__shackFocus = true;
  function send(f) {
    var api = window.pywebview && window.pywebview.api;
    if (api && api.shack_focus) { api.shack_focus(f); }
  }
  window.addEventListener('focus', function () { send(true); });
  window.addEventListener('blur', function () { send(false); });
  if (window.pywebview && window.pywebview.api) {
    if (document.hasFocus()) { send(true); }
  } else {
    window.addEventListener('pywebviewready', function () { if (document.hasFocus()) { send(true); } });
  }
})();
"""


class ShellError(RuntimeError):
    """The app cannot be shown safely; the message says why, for the user."""


# --------------------------------------------------------------- the server

class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:   # noqa: A002 - the stdlib's name
        log.debug("%s %s", self.address_string(), format % args)


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind asks socket.getfqdn() for its own name: a
        # reverse lookup that on macOS goes to mDNS for 127.0.0.1, which an
        # app started from Finder may not do without the Local Network
        # permission. Found as a frozen app hanging at start on the Mac
        # mini. The name is only used in CGI-style variables.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
        self.setup_environ()


class InProcessServer:
    """A WSGI app served from a thread of this process."""

    def __init__(self, app_factory: Callable[[], Callable], token: str):
        holder: dict[str, Callable] = {}

        def late(environ: dict, start_response: Callable) -> Any:
            return holder["app"](environ, start_response)
        # Port 0 on loopback: the OS picks a free port at bind, so there is
        # no window between choosing a port and taking it.
        self.httpd = make_server(LOOPBACK, 0, late, server_class=_ThreadingWSGIServer, handler_class=_QuietHandler)
        self.port = self.httpd.server_address[1]
        holder["app"] = Guard(token, self.port).wsgi(app_factory())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="shack-desktop server")
        self.thread.start()

    def alive(self) -> bool:
        return self.thread.is_alive()

    def wait(self) -> int:
        self.thread.join()
        return 0

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}


def _child_setup() -> None:
    """Runs in the child between fork and exec (POSIX)."""
    # The shell blocks SIGINT/SIGTERM for its own reasons (_Signals), and a
    # blocked mask survives exec: unblocked here, or the server could never
    # be terminated, only killed.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
    if sys.platform.startswith("linux"):
        # The kernel sends the child SIGTERM the moment this process dies,
        # even by SIGKILL.
        try:
            import ctypes
            PR_SET_PDEATHSIG = 1
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception:
            pass


class _Signals:
    """Ctrl+C and SIGTERM, delivered even while a GUI toolkit's main loop runs.

    A Python signal handler only runs when the main thread next executes
    Python, and pywebview's GTK and Cocoa loops sit in C for as long as the
    window is open: SIGTERM to the deb build was found doing nothing at all.
    So the signals are blocked in every thread (block() runs before run()
    starts any) and one thread waits for them with sigwait(), which needs
    no help from the main loop; it then closes the window, which every
    pywebview backend marshals onto its own thread.
    """

    def __init__(self) -> None:
        self.handler: Callable[[], None] | None = None
        self.requested = threading.Event()
        self.active = False

    def block(self) -> None:
        if os.name != "posix" or threading.current_thread() is not threading.main_thread():
            return
        signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        self.active = True
        threading.Thread(target=self._wait, daemon=True, name="shack-desktop signals").start()

    def _wait(self) -> None:
        while True:
            sig = signal.sigwait(STOP_SIGNALS)
            log.info("signal %s: closing", sig)
            self.requested.set()
            if self.handler is not None:
                self.handler()

    def unblock(self) -> None:
        if self.active:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
            self.active = False


class ChildServer:
    """The app's server as its own process (child.py is the other half)."""

    def __init__(self, command: Sequence[str], token: str, cwd: str | None = None,
                 extra_env: dict[str, str] | None = None):
        self.port = _free_port()
        env = dict(os.environ)
        env.update(extra_env or {})
        env.update({ENV_HOST: LOOPBACK, ENV_PORT: str(self.port), ENV_TOKEN: token, ENV_PARENT: str(os.getpid())})
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000          # CREATE_NO_WINDOW: no console flashing up
        else:
            kwargs["preexec_fn"] = _child_setup
            # Its own process group: Ctrl+C in a terminal signals the whole
            # foreground group, and the server must stop when the shell
            # says (after the window has gone), not race it with a
            # KeyboardInterrupt traceback - which the AppImage run on
            # bookworm showed.
            kwargs["process_group"] = 0
        # stdout and stderr go where ours go; a frozen GUI program has none,
        # and then the child's output is dropped rather than failing to start.
        out = None if sys.stdout is not None else subprocess.DEVNULL
        err = None if sys.stderr is not None else subprocess.DEVNULL
        self.proc = subprocess.Popen(list(command), env=env, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=out, stderr=err, **kwargs)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait(self) -> int:
        return self.proc.wait()

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        # Ask first: closing stdin is the child's signal to exit cleanly
        # (child.watch_parent), and lets a server finish writing its files.
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        for step in (self.proc.terminate, self.proc.kill):
            try:
                self.proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                step()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("server process %s did not exit", self.proc.pid)


def _wait_ready(server: InProcessServer | ChildServer, timeout: float) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not server.alive():
            raise ShellError(_("The app's server stopped while starting."))
        try:
            with socket.create_connection((LOOPBACK, server.port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise ShellError(_("The app's server did not start in time."))


def _network_address() -> str | None:
    """This machine's address on its default route, if it has one. A UDP
    connect sends nothing; it only makes the OS choose a source address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))     # TEST-NET-1: never routed anywhere real
            addr = s.getsockname()[0]
            return None if addr.startswith("127.") or addr == "0.0.0.0" else addr
    except OSError:
        return None


def check_private(port: int) -> None:
    """Refuse to show a server that is not private to this window."""
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=5)
    try:
        conn.request("GET", "/", headers={"Host": f"{LOOPBACK}:{port}"})
        status = conn.getresponse().status
    except OSError as exc:
        raise ShellError(_("The app's server did not answer.")) from exc
    finally:
        conn.close()
    if status != 403:
        raise ShellError(_("The app's server does not require its launch token, so it will not be shown."))
    addr = _network_address()
    if addr and _listens_on(addr, port):
        raise ShellError(_("The app's server is reachable from the network, so it will not be shown."))


def _listens_on(addr: str, port: int) -> bool:
    """Whether something accepts connections on addr:port, asked without
    sending anything to the network address where the OS allows it.

    Linux and macOS refuse to bind a specific address on a port someone has
    bound on every address (0.0.0.0), so a failed bind is the answer, and no
    connection is made: on macOS a connection to a LAN address would ask the
    user for the Local Network permission on every first launch. Windows
    lets both binds succeed, so there the question is a connection to our
    own address, which Windows has no prompt for."""
    if sys.platform == "win32":
        try:
            with socket.create_connection((addr, port), timeout=0.5):
                return True
        except OSError:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((addr, port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


# ------------------------------------------------------------ engine choice

def _webview2_installed() -> bool:
    """The Evergreen WebView2 runtime, per machine or per user (the keys
    pywebview's own winforms backend reads)."""
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}")):
        try:
            with winreg.OpenKey(root, path) as k:
                version, _kind = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _probe_engine() -> str | None:
    if sys.platform == "win32":
        if importlib.util.find_spec("clr") is None or not _webview2_installed():
            return None
        return "edgechromium"
    if sys.platform == "darwin":
        return "cocoa" if importlib.util.find_spec("WebKit") is not None else None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gtk, WebKit2  # noqa: F401 - loading them is the test
    except (ImportError, ValueError):
        return None
    return "gtk"


def choose_engine() -> str | None:
    """The pywebview GUI to use, or None for the browser fallback."""
    mode = os.environ.get("SHACK_DESKTOP_UI", "auto").lower()
    if mode == "browser":
        return None
    try:
        import webview  # noqa: F401
    except Exception as exc:        # missing, or broken in a way only importing shows
        log.info("pywebview unavailable (%s): using the browser", exc)
        return None
    if mode == "webview":
        return {"win32": "edgechromium", "darwin": "cocoa"}.get(sys.platform, "gtk")
    engine = _probe_engine()
    if engine is None:
        log.info("no usable web engine for pywebview: using the browser")
    return engine


# ------------------------------------------------------------------ the pad

class PadHook:
    """The window's focus, passed to a shack_pad DesktopPad (or anything
    with the same focus_gained/close methods). Losing focus calls
    focus_lost only if the pad has one: DesktopPad deliberately does nothing
    then (CONTROLLER-PAD.md §6.2: switching to a browser must not take the
    pad from the last OpenHamShack app)."""

    def __init__(self, pad: Any):
        self.pad = pad

    @classmethod
    def make(cls, pad: Any) -> "PadHook | None":
        if pad is None or importlib.util.find_spec("shack_pad") is None:
            return None
        if callable(pad) and not hasattr(pad, "focus_gained"):
            pad = pad()
        return cls(pad) if pad is not None else None

    def start(self) -> None:
        if hasattr(self.pad, "start"):
            self.pad.start()

    def focus(self, gained: bool) -> None:
        if gained:
            self.pad.focus_gained()
        elif hasattr(self.pad, "focus_lost"):
            self.pad.focus_lost()

    def close(self) -> None:
        if hasattr(self.pad, "close"):
            self.pad.close()


class _Bridge:
    """What the page may call (pywebview's js_api): the focus report, and a
    zoom step on the engines whose zoom the shell drives (ZOOM_JS)."""

    def __init__(self, on_focus: Callable[[bool], None], on_zoom: Callable[[int], None] | None = None):
        self._on_focus = on_focus
        self._on_zoom = on_zoom

    def shack_focus(self, focused: Any) -> None:
        self._on_focus(bool(focused))

    def shack_zoom(self, step: Any) -> None:
        if self._on_zoom is not None and step in (-1, 0, 1):
            self._on_zoom(int(step))


# ------------------------------------------------- window size and zoom

# Height a title bar and frame add outside the size pywebview is given on
# GTK and macOS (there it is the content's size; on Windows it is the whole
# window, so this only errs towards maximising there).
DECORATION = 40
# The smallest window an app is ever opened at, unless the screen is smaller.
MIN_SIZE = (640, 480)
# Browser-like zoom steps, as Ctrl+minus and Ctrl+plus walk them.
ZOOM_LEVELS = (0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


def fit_window(size: tuple[int, int], area: tuple[int, int] | None) -> tuple[int, int, bool]:
    """The window to open for an app that asked for `size`, on a screen
    whose usable area is `area` (logical pixels; None if unknown): (width,
    height, maximised).

    A size that fits is kept. One that does not opens maximised, which is
    the whole usable area on every backend, and its restored size is
    shrunk to fit too, so un-maximising does not throw the window off the
    screen. Found when a tester's 1440x960 window was cut off on a laptop
    at 150% scaling (1920x1080 is 1280x720 logical there)."""
    w, h = size
    if area is None:
        return w, h, False
    aw, ah = area
    if w <= aw and h + DECORATION <= ah:
        return w, h, False
    return min(w, int(aw * 0.9)), min(h, int((ah - DECORATION) * 0.9)), True


def min_window(area: tuple[int, int] | None) -> tuple[int, int]:
    """MIN_SIZE, or less on a screen smaller than that: a minimum larger
    than the screen would put the window's edge where it cannot be reached."""
    if area is None:
        return MIN_SIZE
    return min(MIN_SIZE[0], area[0]), min(MIN_SIZE[1], max(area[1] - DECORATION, 1))


def next_zoom(current: float, step: int) -> float:
    """The zoom after Ctrl+plus (step 1), Ctrl+minus (-1) or Ctrl+0 (0)."""
    if step == 0:
        return 1.0
    if step > 0:
        return next((z for z in ZOOM_LEVELS if z > current + 1e-6), ZOOM_LEVELS[-1])
    return next((z for z in reversed(ZOOM_LEVELS) if z < current - 1e-6), ZOOM_LEVELS[0])


def area_from_win32(user32: Any, gdi32: Any) -> tuple[int, int] | None:
    """The primary screen's work area (without the taskbar) in logical
    pixels, which is what pywebview's winforms backend multiplies by the
    window's DPI scale.

    Whether Windows answers in physical or logical pixels depends on the
    process's DPI awareness, which pywebview only declares inside start()
    and a frozen app's manifest may already have: an unaware process gets
    the work area scaled down for it and a screen DPI of 96, an aware one
    the physical pixels and the real DPI. Dividing one by the other gives
    logical pixels either way, so the answer is right before and after."""
    import ctypes
    from ctypes import wintypes
    SPI_GETWORKAREA, LOGPIXELSX = 0x0030, 88
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return None
    hdc = user32.GetDC(None)
    try:
        dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX) or 96
    finally:
        user32.ReleaseDC(None, hdc)
    scale = dpi / 96
    return round((rect.right - rect.left) / scale), round((rect.bottom - rect.top) / scale)


def area_from_gdk(Gdk: Any) -> tuple[int, int] | None:
    """The primary monitor's work area (without panels), in GTK's logical
    pixels; Wayland reports no primary, and then the first monitor."""
    display = Gdk.Display.get_default()
    if display is None:
        return None
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    if monitor is None:
        return None
    r = monitor.get_workarea()
    return r.width, r.height


def area_from_appkit(AppKit: Any) -> tuple[int, int] | None:
    """The menu-bar screen's visible frame (without the menu bar and the
    Dock), in points, which are macOS's logical pixels."""
    screens = AppKit.NSScreen.screens()
    if not screens:
        return None
    size = screens[0].visibleFrame().size
    return int(size.width), int(size.height)


def usable_area(engine: str) -> tuple[int, int] | None:
    """The primary screen's usable area in logical pixels, or None.

    Asked of the OS directly rather than through webview.screens(): before
    start() that loads pywebview's *default* toolkit (Qt on a KDE session,
    whatever gui start() is then given), it reports the whole monitor
    rather than the part panels and the Dock leave free, and on Windows its
    units change once start() makes the process DPI-aware."""
    try:
        if engine == "edgechromium":
            import ctypes
            return area_from_win32(ctypes.windll.user32, ctypes.windll.gdi32)
        if engine == "gtk":
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
            return area_from_gdk(Gdk)
        if engine == "cocoa":
            import AppKit
            return area_from_appkit(AppKit)
    except Exception as exc:          # a window at the asked size beats no window
        log.info("screen size unknown (%s): using the app's size", exc)
    return None


# Ctrl+minus/plus/0 and Ctrl+wheel, for the engines with no zoom of their own
# (WebKitGTK and WKWebView; WebView2 has both built in once pywebview stops
# blocking the wheel, zoomable=True). The page asks, and the shell sets the
# engine's own zoom, which reflows the page as a browser's does - CSS zoom
# on the page would shrink 100vh layouts and leave a gap below them.
ZOOM_JS = """
(function () {
  if (window.__shackZoom) { return; }
  window.__shackZoom = true;
  var steps = {'-': -1, '_': -1, '=': 1, '+': 1, '0': 0};
  function send(step) {
    var api = window.pywebview && window.pywebview.api;
    if (api && api.shack_zoom) { api.shack_zoom(step); }
  }
  window.addEventListener('keydown', function (e) {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || !(e.key in steps)) { return; }
    e.preventDefault();
    send(steps[e.key]);
  }, true);
  window.addEventListener('wheel', function (e) {
    if (!e.ctrlKey || !e.deltaY) { return; }
    e.preventDefault();
    send(e.deltaY < 0 ? 1 : -1);
  }, {passive: false, capture: true});
})();
"""


# ------------------------------------------------------------- the two UIs

class WebviewUI:
    def __init__(self, engine: str, title: str, url: str, icon: str | None, size: tuple[int, int],
                 on_focus: Callable[[bool], None]):
        self.engine, self.title, self.url, self.icon, self.size = engine, title, url, icon, size
        self.on_focus = on_focus
        self.window: Any = None
        self.closing = False
        self.zoom = 1.0

    def run(self) -> None:
        import webview
        if self.closing:
            return
        # Fitted to the screen it opens on: an app's size is what suits it
        # on a big monitor, and a window taller than the screen hides its
        # own bottom edge where it cannot be dragged back.
        area = usable_area(self.engine)
        width, height, maximized = fit_window(self.size, area)
        log.info("screen %s: window %sx%s%s", area, width, height, " maximised" if maximized else "")
        bridge = _Bridge(self.on_focus, self._zoom if self.engine in ("gtk", "cocoa") else None)
        # zoomable: pywebview otherwise blocks Ctrl+wheel in the page, and
        # zooming out is the user's own way to fit a layout to a small screen.
        self.window = webview.create_window(self.title, self.url, width=width, height=height,
                                            min_size=min_window(area), maximized=maximized, zoomable=True,
                                            js_api=bridge)
        self.window.events.loaded += self._loaded
        # private_mode: no cookies, cache or local storage outlive the
        # launch; the launch token is dead by then anyway. icon is used by
        # the GTK backend (Windows and macOS take it from the executable).
        webview.start(gui=self.engine, private_mode=True, icon=self.icon)

    def _loaded(self, *_args: Any) -> None:
        try:
            self.window.evaluate_js(FOCUS_JS)
            if self.engine in ("gtk", "cocoa"):
                self.window.evaluate_js(ZOOM_JS)
        except Exception as exc:
            log.debug("page scripts: %s", exc)
        shot = os.environ.get("SHACK_DESKTOP_SNAPSHOT")
        if shot and self.engine == "cocoa" and not getattr(self, "_shot_taken", False):
            self._shot_taken = True
            threading.Timer(2.0, self._snapshot_cocoa, args=(shot,)).start()

    def _snapshot_cocoa(self, path: str) -> None:
        """SHACK_DESKTOP_SNAPSHOT=<png>, macOS: the page as the window shows
        it, written by WKWebView itself. A diagnostic for proving a build
        on the Mac mini, where `screencapture` over SSH is refused (no
        Screen Recording permission, and none can be granted without
        someone at the screen); the web view's own snapshot needs none and
        cannot contain anything but this app's page."""
        try:
            import AppKit
            from PyObjCTools import AppHelper
        except ImportError:
            return

        def done(image: Any, error: Any) -> None:
            log.info("snapshot: image %s, error %s", image is not None, error)
            if image is None:
                log.warning("snapshot failed: %s", error)
                return
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None).writeToFile_atomically_(
                path, True)

        def take() -> None:
            log.info("snapshot: asking the web view")
            self.window.native.contentView().takeSnapshotWithConfiguration_completionHandler_(None, done)
        AppHelper.callAfter(take)

    def _zoom(self, step: int) -> None:
        """Set the engine's own zoom, on its own thread. Through pywebview's
        backend objects, which it does not offer as API: a pywebview that
        moves them loses the shortcut, never the window."""
        self.zoom = next_zoom(self.zoom, step)
        z = self.zoom
        try:
            if self.engine == "gtk":
                from gi.repository import GLib
                from webview.platforms.gtk import BrowserView
                view = BrowserView.instances[self.window.uid].webview
                GLib.idle_add(lambda: view.set_zoom_level(z) and False)
            elif self.engine == "cocoa":
                from PyObjCTools import AppHelper
                from webview.platforms.cocoa import BrowserView
                view = BrowserView.instances[self.window.uid].webview
                AppHelper.callAfter(view.setPageZoom_, z)     # macOS 11 and later
        except Exception as exc:
            log.warning("zoom %s: %s", z, exc)

    def bring_forward(self) -> None:
        w = self.window
        if w is None:
            return
        w.restore()
        w.show()
        # Raise above whatever is in front without staying there.
        w.on_top = True
        w.on_top = False

    def close(self) -> None:
        self.closing = True
        if self.window is not None:
            self.window.destroy()


class BrowserUI:
    """The default browser, and a small control window to quit with."""

    def __init__(self, title: str, url: str, base_url: str, icon: str | None):
        self.title, self.url, self.base_url, self.icon = title, url, base_url, icon
        self._quit = threading.Event()
        self._raise: "queue.Queue[bool]" = queue.Queue()

    def open_browser(self) -> None:
        # The token URL every time: a browser that lost the cookie (another
        # profile, cleared data) gets it back, and the server's redirect
        # takes the token straight out of the address again.
        if not webbrowser.open(self.url):
            log.warning("no web browser could be started")

    def run(self) -> None:
        if self._quit.is_set():
            return
        self.open_browser()
        if not self._run_tk() and not self._run_win32_box():
            sys.stderr.write(_("{app} is open in your web browser at {url}. Press Ctrl+C to quit.").format(
                app=self.title, url=self.base_url) + "\n")
            while not self._quit.wait(0.25):
                if not self._raise.empty():
                    self._raise.get()
                    self.open_browser()

    def _run_tk(self) -> bool:
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:     # no Tk in this Python, or no display
            log.info("no control window (%s)", exc)
            return False
        root.title(self.title)
        root.resizable(False, False)
        if self.icon and self.icon.lower().endswith(".png"):
            try:
                root.iconphoto(True, tk.PhotoImage(file=self.icon))
            except Exception:
                pass
        frame = tk.Frame(root, padx=16, pady=12)
        frame.pack()
        tk.Label(frame, text=_("{app} is open in your web browser.").format(app=self.title)).pack(pady=(0, 10))
        row = tk.Frame(frame)
        row.pack()
        tk.Button(row, text=_("Open again"), command=self.open_browser).pack(side="left", padx=4)
        tk.Button(row, text=_("Quit"), command=self._quit.set).pack(side="left", padx=4)
        root.protocol("WM_DELETE_WINDOW", self._quit.set)

        def tick() -> None:
            # Tk is only touched from its own thread: other threads post to
            # the event and queue, and this loop acts on them.
            if self._quit.is_set():
                root.destroy()
                return
            if not self._raise.empty():
                self._raise.get()
                root.deiconify()
                root.lift()
                self.open_browser()
            root.after(200, tick)
        root.after(200, tick)
        root.mainloop()
        return True

    def _run_win32_box(self) -> bool:
        """Windows without Tk (python.org's NuGet Python, which the
        installers are built from, has none): a message box is the one
        control window the OS gives for nothing, and a windowed program has
        no console to press Ctrl+C in. Retry opens the browser again,
        Cancel quits; the system names the buttons in the user's language."""
        if sys.platform != "win32":
            return False
        import ctypes
        user32 = ctypes.windll.user32
        MB_RETRYCANCEL, MB_ICONINFORMATION, MB_SETFOREGROUND, IDRETRY, WM_CLOSE = 0x5, 0x40, 0x10000, 4, 0x0010
        text = _("{app} is open in your web browser.\n\nRetry opens it again; Cancel quits.").format(app=self.title)

        def watch() -> None:
            # Other threads ask through the event and queue; the box is
            # closed by posting WM_CLOSE to it (#32770 is the dialog class).
            while not self._quit.wait(0.2):
                if not self._raise.empty():
                    self._raise.get()
                    self.open_browser()
            hwnd = user32.FindWindowW("#32770", self.title)
            if hwnd:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        threading.Thread(target=watch, daemon=True, name="shack-desktop box").start()
        while not self._quit.is_set():
            answer = user32.MessageBoxW(None, text, self.title, MB_RETRYCANCEL | MB_ICONINFORMATION | MB_SETFOREGROUND)
            if answer == IDRETRY and not self._quit.is_set():
                self.open_browser()
                continue
            break
        self._quit.set()
        return True

    def bring_forward(self) -> None:
        self._raise.put(True)

    def close(self) -> None:
        self._quit.set()


# ------------------------------------------------------------------- run()

def _report(title: str, message: str) -> None:
    sys.stderr.write(f"{title}: {message}\n")
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def run(app_factory: Callable[[], Callable] | None = None, *, command: Sequence[str] | None = None,
        title: str, kind: str, icon: str | Path | None = None, pad: Any = None,
        size: tuple[int, int] = (1280, 820), cwd: str | None = None, env: dict[str, str] | None = None,
        ready_timeout: float = 30.0, state_dir: Path | None = None) -> int:
    """Show a web-UI app in its own window; return when it is closed.

    Exactly one of `app_factory` (returns a WSGI app, served in this
    process) or `command` (starts the server; it reads child.server_env())
    is given. `kind` names the app's data folder and its single-instance
    lock ("qo100wb", "icom", ...); `title` is the window's title; `icon` a
    PNG for Linux windows. `pad` is a shack_pad DesktopPad, or a callable
    that makes one, and is used only when shack_pad is installed.
    """
    if (app_factory is None) == (command is None):
        raise TypeError("give exactly one of app_factory or command")
    trace = os.environ.get("SHACK_DESKTOP_LOG")
    if trace and not logging.getLogger().handlers:
        # A frozen window app has no console: this is where its log can go
        # when someone needs to see why it did what it did.
        logging.basicConfig(filename=trace, level=logging.DEBUG,
                            format="%(asctime)s %(threadName)s %(name)s %(levelname)s %(message)s")
    instance = SingleInstance(Path(state_dir) if state_dir else data_dir(kind))
    if not instance.acquire():
        instance.request_focus()
        log.info("%s is already running: asked it to come forward", title)
        return EXIT_OK

    signals = _Signals()
    signals.block()            # before any thread exists, so every thread inherits it
    token = secrets.token_urlsafe(32)
    server: InProcessServer | ChildServer | None = None
    hook = PadHook.make(pad)
    ui: WebviewUI | BrowserUI | None = None
    result = {"code": EXIT_OK}
    try:
        try:
            server = InProcessServer(app_factory, token) if app_factory else ChildServer(command, token, cwd, env)
            _wait_ready(server, ready_timeout)
            check_private(server.port)
        except (ShellError, OSError) as exc:
            _report(title, str(exc))
            return EXIT_SERVER
        base = f"http://{LOOPBACK}:{server.port}/"
        url = f"{base}?{QUERY}={token}"
        icon_s = str(icon) if icon else None

        def on_focus(gained: bool) -> None:
            if hook:
                try:
                    hook.focus(gained)
                except Exception as exc:   # a pad problem must never take the window down
                    log.warning("pad focus: %s", exc)

        engine = choose_engine()
        log.info("server on 127.0.0.1:%s, engine %s", server.port, engine or "browser")
        ui = WebviewUI(engine, title, url, icon_s, size, on_focus) if engine else BrowserUI(title, url, base, icon_s)
        if hook:
            hook.start()
            if isinstance(ui, BrowserUI):
                on_focus(True)     # just launched by the operator: the pad follows them here

        def server_watch() -> None:
            code = server.wait()
            if ui is not None and not stopping.is_set():
                log.warning("the app's server exited (%s): closing the window", code)
                result["code"] = EXIT_SERVER
                ui.close()
        stopping = threading.Event()
        threading.Thread(target=server_watch, daemon=True, name="shack-desktop server watch").start()
        instance.watch(ui.bring_forward)

        signals.handler = ui.close
        if signals.requested.is_set():     # Ctrl+C while the server was starting
            return EXIT_OK
        try:
            ui.run()
        finally:
            stopping.set()
        return result["code"]
    finally:
        if hook:
            try:
                hook.close()
            except Exception as exc:
                log.warning("pad close: %s", exc)
        if server is not None:
            server.stop()
        instance.release()
        signals.unblock()
