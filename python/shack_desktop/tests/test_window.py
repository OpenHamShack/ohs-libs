"""The window's size and zoom (shell.py): fitted to the screen it opens on,
whatever the OS says the screen is and however DPI scaling is set, and
zoomable from the keyboard and the wheel.

The OS's answers are stand-ins shaped like the real ones (user32/gdi32,
Gdk, AppKit), and the window is a stand-in webview module that records what
the shell asked pywebview for."""

import ctypes
import sys
import types
from types import SimpleNamespace as NS

import pytest

import shack_desktop.shell as shell


# ------------------------------------------------------------ fit_window

def test_a_size_that_fits_is_kept():
    assert shell.fit_window((1440, 960), (1920, 1032)) == (1440, 960, False)


@pytest.mark.parametrize("area", [
    (1280, 672),     # 1920x1080 at 150%, less the taskbar: Ray's case
    (1366, 728),     # a 1366x768 laptop at 100%
    (1536, 824),     # 1920x1080 at 125%
])
def test_too_big_for_the_screen_opens_maximised_and_restores_inside_it(area):
    w, h, maximised = shell.fit_window((1440, 960), area)
    assert maximised
    assert w <= area[0] and h + shell.DECORATION <= area[1]


def test_an_unknown_screen_gets_the_asked_size():
    assert shell.fit_window((1280, 820), None) == (1280, 820, False)


def test_the_minimum_never_exceeds_the_screen():
    assert shell.min_window((1920, 1032)) == shell.MIN_SIZE
    w, h = shell.min_window((533, 400))          # 800x600 at 150%
    assert w <= 533 and h + shell.DECORATION <= 400


# --------------------------------------------------- what the OS reports

class FakeWindows:
    """user32 and gdi32 as Windows answers a process that is, or is not,
    DPI-aware, on a 1920x1080 screen with a 48 px taskbar at `scale`."""

    def __init__(self, scale, aware):
        self.scale, self.aware = scale, aware

    def SystemParametersInfoW(self, action, _param, rect_ref, _flags):
        assert action == 0x0030
        rect = rect_ref._obj
        # An unaware process is shown a smaller screen: Windows divides
        # the pixels by the scale for it.
        k = 1 if self.aware else self.scale
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, round(1920 / k), round((1080 - 48 * self.scale) / k)
        return 1

    def GetDC(self, _hwnd):
        return 7

    def ReleaseDC(self, _hwnd, _hdc):
        return 1

    def GetDeviceCaps(self, hdc, index):
        assert (hdc, index) == (7, 88)
        # ...and told the screen is 96 dpi.
        return round(96 * self.scale) if self.aware else 96


@pytest.mark.parametrize("aware", [False, True])
@pytest.mark.parametrize("scale, want", [(1.0, (1920, 1032)), (1.25, (1536, 816)), (1.5, (1280, 672))])
def test_windows_work_area_is_logical_with_or_without_dpi_awareness(scale, want, aware):
    win = FakeWindows(scale, aware)
    assert shell.area_from_win32(win, win) == want


def test_windows_that_will_not_say_is_unknown():
    class Silent(FakeWindows):
        def SystemParametersInfoW(self, *a):
            return 0
    win = Silent(1.0, True)
    assert shell.area_from_win32(win, win) is None


def gdk(primary, monitors):
    display = NS(get_primary_monitor=lambda: primary, get_monitor=lambda i: monitors[i] if i < len(monitors) else None)
    return NS(Display=NS(get_default=lambda: display))


def monitor(w, h):
    return NS(get_workarea=lambda: NS(x=0, y=0, width=w, height=h))


def test_gtk_uses_the_primary_monitors_work_area():
    assert shell.area_from_gdk(gdk(monitor(1366, 740), [monitor(2560, 1440), monitor(1366, 740)])) == (1366, 740)


def test_gtk_without_a_primary_uses_the_first_monitor():
    # Wayland names no primary monitor.
    assert shell.area_from_gdk(gdk(None, [monitor(1280, 720)])) == (1280, 720)
    assert shell.area_from_gdk(NS(Display=NS(get_default=lambda: None))) is None


def test_macos_uses_the_menu_bar_screens_visible_frame():
    screen = NS(visibleFrame=lambda: NS(size=NS(width=1440.0, height=875.0)))
    other = NS(visibleFrame=lambda: NS(size=NS(width=3008.0, height=1667.0)))
    assert shell.area_from_appkit(NS(NSScreen=NS(screens=lambda: [screen, other]))) == (1440, 875)
    assert shell.area_from_appkit(NS(NSScreen=NS(screens=lambda: []))) is None


def test_a_failing_screen_query_still_opens_a_window(monkeypatch):
    def broken(*_a):
        raise OSError("no display")
    monkeypatch.setattr(shell, "area_from_gdk", broken)
    assert shell.usable_area("gtk") is None


# ------------------------------------------------------------ the window

@pytest.fixture
def fake_webview(monkeypatch):
    calls = []

    class Loaded(list):
        def __iadd__(self, fn):
            self.append(fn)
            return self

    class Window:
        uid = "master"

        def __init__(self, title, url, **kw):
            self.kw = kw
            self.events = NS(loaded=Loaded())
            self.scripts = []

        def evaluate_js(self, script):
            self.scripts.append(script)

    def create_window(title, url, **kw):
        w = Window(title, url, **kw)
        calls.append(w)
        return w

    mod = types.ModuleType("webview")
    mod.create_window = create_window
    mod.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, "webview", mod)
    return calls


def open_window(monkeypatch, engine, area, size=(1440, 960)):
    monkeypatch.setattr(shell, "usable_area", lambda e: area)
    ui = shell.WebviewUI(engine, "WB", "http://127.0.0.1:1/", None, size, lambda f: None)
    ui.run()
    return ui


def test_the_window_is_maximised_on_a_small_screen_and_zoomable(monkeypatch, fake_webview):
    open_window(monkeypatch, "edgechromium", (1280, 672))
    kw = fake_webview[0].kw
    assert kw["maximized"] is True and kw["zoomable"] is True
    assert kw["width"] <= 1280 and kw["height"] + shell.DECORATION <= 672
    assert kw["min_size"] == (640, 480)


def test_the_window_keeps_its_size_on_a_big_screen(monkeypatch, fake_webview):
    open_window(monkeypatch, "gtk", (2560, 1400))
    kw = fake_webview[0].kw
    assert (kw["width"], kw["height"], kw["maximized"], kw["zoomable"]) == (1440, 960, False, True)


def test_webview2_zooms_by_itself_and_the_shell_does_not(monkeypatch, fake_webview):
    ui = open_window(monkeypatch, "edgechromium", None)
    w = fake_webview[0]
    ui._loaded()
    assert not any("__shackZoom" in s for s in w.scripts)
    w.kw["js_api"].shack_zoom(-1)
    assert ui.zoom == 1.0


@pytest.mark.parametrize("engine", ["gtk", "cocoa"])
def test_gtk_and_macos_get_the_zoom_keys_from_the_shell(monkeypatch, fake_webview, engine):
    ui = open_window(monkeypatch, engine, None)
    w = fake_webview[0]
    ui._loaded()
    assert any("__shackZoom" in s for s in w.scripts)
    api = w.kw["js_api"]
    # The engine's own zoom is out of reach here (no backend loaded): the
    # shell logs that and keeps counting.
    api.shack_zoom(-1)
    api.shack_zoom(-1)
    assert ui.zoom == 0.8
    api.shack_zoom(0)
    assert ui.zoom == 1.0
    api.shack_zoom("drop table")          # the page is not trusted to send only steps
    assert ui.zoom == 1.0


def test_zoom_walks_browser_steps_and_stops_at_the_ends():
    z = 1.0
    for _ in range(20):
        z = shell.next_zoom(z, -1)
    assert z == shell.ZOOM_LEVELS[0]
    assert shell.next_zoom(z, 1) == 0.67
    assert shell.next_zoom(2.0, 1) == 2.0
    assert shell.next_zoom(1.1, 0) == 1.0
