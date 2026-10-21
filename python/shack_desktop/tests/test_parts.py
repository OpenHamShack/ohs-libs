"""The pieces the shell is made of, in this process: the data folder per
OS, the single-instance lock, the engine choice and the pad hook."""

import os
import socket
import sys
from pathlib import Path

import pytest

import shack_desktop.shell as shell
from shack_desktop import child, paths
from shack_desktop.instance import SingleInstance


@pytest.mark.parametrize("platform, env, want", [
    ("linux", {"XDG_DATA_HOME": "/x/data"}, "/x/data/openhamshack/icom"),
    ("win32", {"LOCALAPPDATA": r"C:\Users\op\AppData\Local"}, r"C:\Users\op\AppData\Local/OpenHamShack/icom"),
    ("darwin", {}, "HOME/Library/Application Support/OpenHamShack/icom"),
])
def test_data_dir_per_os(monkeypatch, platform, env, want):
    monkeypatch.setattr(sys, "platform", platform)
    for k in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    got = str(paths.data_dir("icom", create=False)).replace("\\", "/")
    assert got == want.replace("HOME", Path.home().as_posix()).replace("\\", "/")


def test_data_dir_is_made_private(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    d = paths.data_dir("qo100wb")
    assert d.is_dir()
    if os.name == "posix":
        assert d.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError):
        paths.data_dir("../etc")


def test_one_instance_holds_the_lock_and_takes_requests(tmp_path):
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    second.request_focus()
    assert first.take_request() and not first.take_request()
    first.release()
    assert second.acquire()
    second.release()


def test_a_stale_request_is_ignored_by_a_fresh_start(tmp_path):
    SingleInstance(tmp_path).request_focus()
    inst = SingleInstance(tmp_path)
    assert inst.acquire()
    assert not inst.take_request()
    inst.release()


def test_engine_choice_falls_back_when_pywebview_will_not_import(monkeypatch):
    monkeypatch.delenv("SHACK_DESKTOP_UI", raising=False)
    monkeypatch.setitem(sys.modules, "webview", None)    # import raises ImportError
    assert shell.choose_engine() is None


def test_engine_choice_obeys_the_browser_switch(monkeypatch):
    monkeypatch.setenv("SHACK_DESKTOP_UI", "browser")
    assert shell.choose_engine() is None


def test_linux_without_a_display_uses_the_browser(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert shell._probe_engine() is None


class FakePad:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def focus_gained(self):
        self.calls.append("gained")

    def close(self):
        self.calls.append("close")


def test_window_focus_reaches_the_pad(monkeypatch):
    monkeypatch.setattr(shell.importlib.util, "find_spec", lambda name: object())
    pad = FakePad()
    hook = shell.PadHook.make(pad)
    bridge = shell._Bridge(hook.focus)
    hook.start()
    bridge.shack_focus(True)
    bridge.shack_focus(False)     # DesktopPad has no focus_lost: nothing happens
    hook.close()
    assert pad.calls == ["start", "gained", "close"]


def test_a_pad_factory_is_called_once(monkeypatch):
    monkeypatch.setattr(shell.importlib.util, "find_spec", lambda name: object())
    made = []
    hook = shell.PadHook.make(lambda: made.append(FakePad()) or made[-1])
    assert len(made) == 1 and hook.pad is made[0]


def test_no_pad_hook_without_shack_pad(monkeypatch):
    monkeypatch.setattr(shell.importlib.util, "find_spec", lambda name: None)
    assert shell.PadHook.make(FakePad()) is None


def test_the_real_desktop_pad_takes_the_hook(tmp_path):
    shack_pad = pytest.importorskip("shack_pad")
    pad = shack_pad.DesktopPad(shack_pad.InputHost([]), app="shack-test", store_dir=tmp_path,
                               open_device=lambda: None)
    hook = shell.PadHook.make(pad)
    hook.focus(True)      # claims: writes the want file and takes the free lock
    assert pad.focus.owns
    hook.focus(False)
    assert pad.focus.owns  # losing focus keeps the pad (CONTROLLER-PAD.md §6.2)
    hook.close()
    assert not pad.focus.owns


def test_server_env_insists_on_loopback(monkeypatch):
    monkeypatch.setenv("SHACK_DESKTOP_TOKEN", "x" * 32)
    monkeypatch.setenv("SHACK_DESKTOP_PORT", "8123")
    monkeypatch.setenv("SHACK_DESKTOP_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError):
        child.server_env()
    monkeypatch.setenv("SHACK_DESKTOP_HOST", "127.0.0.1")
    env = child.server_env()
    assert (env.host, env.port, env.guard.cookie) == ("127.0.0.1", 8123, "shack_desktop_8123")


def test_self_command(monkeypatch):
    assert child.self_command("shack_hello", "--server") == [sys.executable, "-m", "shack_hello", "--server"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert child.self_command("shack_hello", "--server") == [sys.executable, "--server"]


def test_the_privacy_check_refuses_an_open_server():
    # A plain server that answers everyone: the shell must not show it.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Open(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Open)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(shell.ShellError):
            shell.check_private(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_loopback_server_makes_no_reverse_lookup(monkeypatch):
    # On macOS getfqdn("127.0.0.1") goes to mDNS, which hung the frozen
    # example at start; the servers must never ask.
    import socket as socket_mod
    from http.server import BaseHTTPRequestHandler

    def no(*a, **k):
        raise AssertionError("reverse DNS lookup")
    monkeypatch.setattr(socket_mod, "getfqdn", no)
    httpd = child.LoopbackHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    assert httpd.server_name == "127.0.0.1"
    httpd.server_close()
    inproc = shell.InProcessServer(lambda: (lambda e, s: [b""]), "t" * 32)
    inproc.stop()
