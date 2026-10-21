"""The shell end to end, as separate processes (shell.py): the window is the
stand-in pywebview in tests/fakes, the browser a stand-in $BROWSER, and the
servers tests/launch.py and tests/server.py. No display is needed."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from shack_desktop.shell import _network_address

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the subprocess tests signal processes the POSIX way")


def read_log(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.05)
    return cond()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead for our purposes (its parent has not reaped it yet).
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (OSError, IndexError):     # gone between the two looks
        return False


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Shell:
    """One launch of tests/launch.py with the stand-ins wired in."""

    def __init__(self, tmp, *args, webview="fake", state=None):
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.log = self.tmp / "webview.jsonl"
        self.browser_log = self.tmp / "browser.jsonl"
        self.close_file = self.tmp / "close"
        self.pid_file = self.tmp / "server.pid"
        fakes = {"fake": HERE / "fakes", "broken": HERE / "fakes" / "broken"}[webview]
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "SHACK_DESKTOP_UI")}
        env.update({
            "PYTHONPATH": os.pathsep.join([str(fakes), str(SRC)]),
            "FAKE_WEBVIEW_LOG": str(self.log),
            "FAKE_WEBVIEW_CLOSE": str(self.close_file),
            "FAKE_BROWSER_LOG": str(self.browser_log),
            "BROWSER": f"{sys.executable} {HERE / 'fakes' / 'fakebrowser.py'} %s",
            "TEST_STATE_DIR": str(state or self.tmp / "state"),
            "TEST_PID_FILE": str(self.pid_file),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if webview == "fake":
            # The stand-in has no engine to probe for; this is the switch
            # the shell offers for exactly that.
            env["SHACK_DESKTOP_UI"] = "webview"
        # Its own process group, as a terminal gives a program it starts:
        # Ctrl+C then reaches the shell and anything left in its group.
        self.proc = subprocess.Popen([sys.executable, str(HERE / "launch.py"), *args], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, process_group=0)

    def events(self, call=None):
        return [e for e in read_log(self.log) if call is None or e["call"] == call]

    def started(self):
        got = self.events("start")
        return got[0] if got else None

    def close(self):
        self.close_file.write_text("")

    def finish(self, timeout=15):
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
            raise AssertionError(f"the shell did not exit:\n{err}")
        return self.proc.returncode, err

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.communicate()


@pytest.fixture
def shells():
    made = []

    def make(*a, **kw):
        s = Shell(*a, **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.kill()
        if s.pid_file.exists():
            pid = int(s.pid_file.read_text())
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


def port_of(url):
    return int(url.split(":")[2].split("/")[0])


def test_in_process_server_opens_a_private_window_and_drops_the_token(shells, tmp_path):
    s = shells(tmp_path, "inproc")
    st = wait(s.started)
    assert st, s.finish()[1]
    assert st["private_mode"] is True and st["gui"] in ("gtk", "edgechromium", "cocoa")
    # The window loaded the app through the token exchange: 200, the app's
    # body, the cookie kept, and the address it ended on has no token.
    assert st["status"] == 200 and st["body"] == "hello from in-process"
    assert "shack_token" not in st["final_url"]
    port = port_of(st["final_url"])
    assert st["cookies"] == [f"shack_desktop_{port}"]
    create = s.events("create_window")[0]
    assert "shack_token=" in create["url"] and create["url"].startswith(f"http://127.0.0.1:{port}/")
    # No display to measure here: the app's own size, zoomable.
    assert (create["kw"]["width"], create["kw"]["height"]) == ("1280", "820")
    assert create["kw"]["maximized"] == "False" and create["kw"]["zoomable"] == "True"
    # The focus script was put into the page on load.
    assert s.events("evaluate_js")[0]["focus_script"] is True

    # Anyone else on this machine: no token, no app.
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    assert e.value.code == 403
    # And nobody on the network at all.
    addr = _network_address()
    if addr:
        with pytest.raises(OSError):
            socket.create_connection((addr, port), timeout=1).close()

    s.close()
    code, err = s.finish()
    assert code == 0, err
    assert not port_open(port)


def test_closing_the_window_stops_a_child_server(shells, tmp_path):
    s = shells(tmp_path, "command")
    st = wait(s.started)
    assert st, s.finish()[1]
    assert st["status"] == 200 and st["body"] == "hello from the child"
    pid, port = int(s.pid_file.read_text()), port_of(st["final_url"])
    assert alive(pid) and port_open(port)
    s.close()
    code, err = s.finish()
    assert code == 0, err
    assert wait(lambda: not alive(pid), 5), "the server outlived its window"
    assert not port_open(port)


def test_a_killed_shell_takes_its_server_with_it(shells, tmp_path):
    s = shells(tmp_path, "command")
    assert wait(s.started)
    pid = int(s.pid_file.read_text())
    s.proc.send_signal(signal.SIGKILL)     # no finally, no cleanup: only the child's own watch
    s.proc.communicate()
    assert wait(lambda: not alive(pid), 5), "an orphaned server kept running"


def test_sigterm_closes_the_window_and_the_server(shells, tmp_path):
    s = shells(tmp_path, "command")
    assert wait(s.started)
    pid = int(s.pid_file.read_text())
    s.proc.send_signal(signal.SIGTERM)
    code, err = s.finish()
    assert code == 0, err
    assert s.events("destroy")
    assert wait(lambda: not alive(pid), 5)


def test_ctrl_c_in_a_terminal_stops_the_shell_and_then_its_server(shells, tmp_path):
    # A terminal's Ctrl+C signals the whole foreground group. The server
    # is in a group of its own, so it is stopped by the shell, cleanly,
    # rather than dying of its own KeyboardInterrupt first.
    s = shells(tmp_path, "command", webview="broken")
    assert wait(lambda: read_log(s.browser_log))
    pid = int(s.pid_file.read_text())
    os.killpg(s.proc.pid, signal.SIGINT)
    code, err = s.finish()
    assert code == 0, err
    assert "Traceback" not in err and "KeyboardInterrupt" not in err
    assert wait(lambda: not alive(pid), 5)


def test_without_pywebview_the_browser_is_used(shells, tmp_path):
    s = shells(tmp_path, "command", webview="broken")
    got = wait(lambda: read_log(s.browser_log))
    assert got, s.finish()[1]
    # The browser got the app, and the token left its address.
    assert got[0]["status"] == 200 and got[0]["body"] == "hello from the child"
    assert "shack_token" not in got[0]["final_url"]
    assert not s.log.exists()           # no window was attempted
    pid = int(s.pid_file.read_text())
    # No display here, so no Tk window: the terminal fallback, which says
    # where the app is without repeating the token.
    s.proc.send_signal(signal.SIGINT)
    code, err = s.finish()
    assert code == 0
    assert "open in your web browser at http://127.0.0.1:" in err and "shack_token" not in err
    assert wait(lambda: not alive(pid), 5)


def test_a_second_launch_brings_the_first_forward(shells, tmp_path):
    state = tmp_path / "state"
    a = shells(tmp_path / "a", "inproc", state=state)
    assert wait(a.started)
    b = shells(tmp_path / "b", "inproc", state=state)
    code, err = b.finish(timeout=10)
    assert code == 0, err
    assert not b.log.exists(), "the second launch opened a window of its own"
    def raised():
        calls = [e["call"] for e in a.events() if e["call"] in ("restore", "show", "on_top")]
        return calls if len(calls) >= 4 else None
    assert wait(raised) == ["restore", "show", "on_top", "on_top"]
    a.close()
    assert a.finish()[0] == 0
    # With the first gone, the next launch is the running copy itself.
    c = shells(tmp_path / "c", "inproc", state=state)
    assert wait(c.started)
    c.close()
    assert c.finish()[0] == 0


def test_a_server_without_the_guard_is_never_shown(shells, tmp_path):
    s = shells(tmp_path, "command", "--no-guard")
    code, err = s.finish()
    assert code == 3
    assert "does not require its launch token" in err
    assert not s.events("create_window")
    pid = int(s.pid_file.read_text())
    assert wait(lambda: not alive(pid), 5)


@pytest.mark.skipif(_network_address() is None, reason="this machine has no network address to test from")
def test_a_server_on_every_address_is_never_shown(shells, tmp_path):
    s = shells(tmp_path, "command", "--bind-all")
    code, err = s.finish()
    assert code == 3
    assert "reachable from the network" in err
    assert not s.events("create_window")


def test_a_server_that_dies_closes_the_window(shells, tmp_path):
    s = shells(tmp_path, "command", "--die-after", "1.5")
    assert wait(s.started)
    code, err = s.finish()
    assert code == 3
    assert s.events("destroy")
