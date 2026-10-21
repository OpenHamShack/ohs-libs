"""Station A's hands: drives OpenHamShack Packet Radio's window on the
Windows VM for tools/twostation.py, through UI Automation (pywinauto).

The app has no automation hook of its own - no servers inside the desktop
apps (GUIDE §1) - so the harness does what an operator would: clicks the
page list, types into the fields, presses the buttons and reads the tables
and the chat screen. Qt exposes all of that to UI Automation (object names
become AutomationIds, labels become names), which the first dump on the VM
showed.

Why a loopback socket rather than running pywinauto over SSH: an SSH
session on Windows is not the logged-on user's desktop session, and UI
Automation only sees windows in its own session. So this script runs in
labadmin's session (started by a scheduled task with /IT) and answers JSON
lines on 127.0.0.1:47001, which the harness reaches through an SSH tunnel.
A file queue polled over SSH came first and was dropped: each SSH round trip
through office to PowerShell took about 7 s, too slow to kill an app
mid-over or follow a chat.

    python twostation_win.py serve          # in the desktop session (schtasks /IT)
    python twostation_win.py once dump      # the same commands, for poking by hand

Nothing here keys a radio by itself: transmitting needs Arm, which the
harness only clicks under --keyed --operator-present, and the app's own
limits (arm expiry, max key) stay in force.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(r"C:\ohs\rig")
EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OpenHamShack" / "Packet Radio" / "shack-packet.exe"
TITLE = "OpenHamShack Packet Radio"
NAV = {"monitor": "Monitor", "aprs": "APRS", "messages": "Messages", "chat": "Connect / Chat",
       "mailbox": "Mailbox", "ip": "IP", "sdr": "SDR monitor"}


def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


_maximized: set = set()


def main_window(timeout: float = 20.0):
    end = time.time() + timeout
    while True:
        for w in _desktop().windows():
            if w.window_text() == TITLE:
                # Maximized once per window: at its default size on the VM's
                # 1280x800 display the station bar (and Arm) sits under the
                # taskbar, where a click lands on the taskbar instead.
                if w.handle not in _maximized:
                    _maximized.add(w.handle)
                    try:
                        w.maximize()
                        w.set_focus()
                    except Exception:               # noqa: BLE001 - best effort
                        pass
                return w
        if time.time() > end:
            raise RuntimeError("the app's window is not open")
        time.sleep(0.5)


def _all(win, control_type=None, auto_contains=None, name=None):
    out = []
    for e in win.descendants(control_type=control_type) if control_type else win.descendants():
        info = e.element_info
        if auto_contains and auto_contains not in (info.automation_id or ""):
            continue
        if name is not None and info.name != name:
            continue
        out.append(e)
    return out


def _visible(elems):
    shown = []
    for e in elems:
        try:
            if e.is_visible():
                shown.append(e)
        except Exception:                           # noqa: BLE001 - gone between list and check
            pass
    return shown


def _close_dialogs(win) -> list[str]:
    """Close any message box the app put up, returning what it said."""
    said = []
    for w in _desktop().windows():
        if w.element_info.class_name == "QMessageBox" or (w.window_text() and w.window_text() != TITLE and
                                                          w.element_info.class_name.startswith("Q")):
            said.append(" | ".join(t.window_text() for t in w.descendants(control_type="Text")))
            for b in w.descendants(control_type="Button"):
                if b.window_text() in ("OK", "Close"):
                    b.invoke()
                    break
    for d in _visible(_all(win, "Window")):
        if "QMessageBox" in (d.element_info.automation_id or "") or d.element_info.class_name == "QMessageBox":
            said.append(" | ".join(t.window_text() for t in d.descendants(control_type="Text")))
            for b in d.descendants(control_type="Button"):
                if b.window_text() in ("OK", "Close"):
                    b.invoke()
                    break
    return said


# -- commands ------------------------------------------------------------------------------

def c_ping(_a):
    return {"pong": time.time()}


def c_quit(_a):
    return {"bye": True}


def c_start_app(a):
    if any(p for p in _procs()):
        return {"already": True, "pids": _procs()}
    args = [str(EXE)] + list(a.get("args", []))
    subprocess.Popen(args, close_fds=True, creationflags=0x00000008)    # DETACHED_PROCESS
    main_window(a.get("timeout", 30))
    return {"started": True, "pids": _procs()}


def _procs() -> list[int]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq shack-packet.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return [int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"shack-packet.exe"')]


def c_kill_app(_a):
    pids = _procs()
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    return {"killed": pids, "at": time.time()}


def c_close_app(a):
    win = main_window(5)
    win.close()
    end = time.time() + a.get("timeout", 15)
    while _procs() and time.time() < end:
        time.sleep(0.3)
    return {"closed": not _procs()}


def c_running(_a):
    return {"pids": _procs()}


def c_status(_a):
    win = main_window(5)
    bar = [e.window_text() for e in _all(win, "Text", "StationBar")]
    arm = [e.window_text() for e in _all(win, None, "StationBar.arm")]
    return {"bar": bar, "arm": arm[0] if arm else None, "dialogs": _close_dialogs(win)}


def _press(e) -> None:
    """Press a button through UI Automation, not the mouse: Invoke, or
    Toggle for a checkable one (Arm is a checkable QPushButton, which UIA
    shows as a CheckBox), with a real click only as the last resort."""
    for how in ("invoke", "toggle", "click_input"):
        fn = getattr(e, how, None)
        if fn is None:
            continue
        try:
            fn()
            return
        except Exception:                           # noqa: BLE001 - next way
            continue
    raise RuntimeError(f"could not press {e.window_text()!r}")


def _arm_button(win):
    found = _all(win, None, "StationBar.arm")
    if not found:
        raise RuntimeError("no Arm button")
    return found[0]


def c_arm(a):
    win = main_window(5)
    b = _arm_button(win)
    if b.window_text().startswith("ARMED"):
        return {"armed": True, "already": True}
    if not a.get("keyed"):
        raise RuntimeError("arm asked for without keyed=True")
    _press(b)
    time.sleep(1.0)
    said = _close_dialogs(win)
    text = _arm_button(win).window_text()
    return {"armed": text.startswith("ARMED"), "button": text, "dialogs": said}


def c_disarm(_a):
    win = main_window(5)
    b = _arm_button(win)
    if b.window_text().startswith("ARMED"):
        _press(b)
        time.sleep(0.5)
    text = _arm_button(win).window_text()
    return {"armed": text.startswith("ARMED"), "button": text}


def c_page(a):
    win = main_window(5)
    want = NAV[a["page"]]
    for item in _all(win, "ListItem", "MainWindow.QWidget.nav"):
        if item.window_text() == want:
            # A real click: Qt switches pages on the list's current row, which
            # UIA's SelectionItem.Select sets the highlight of but not the
            # current row (seen on the VM: Messages highlighted, Monitor shown).
            item.click_input()
            time.sleep(0.4)
            return {"page": want}
    raise RuntimeError(f"no page {want}")


def _edits(win, page_cls):
    return _visible(_all(win, "Edit", page_cls))


def c_send_message(a):
    win = main_window(5)
    c_page({"page": "messages"})
    edits = _edits(win, "MessagesPage")
    to, text = edits[0], edits[1]
    to.set_edit_text(a["to"])
    text.set_edit_text(a["text"])
    send = [b for b in _visible(_all(win, "Button", "MessagesPage")) if b.window_text() == "Send"]
    _press(send[0])
    time.sleep(0.5)
    return {"sent": True, "dialogs": _close_dialogs(win)}


def c_messages(_a):
    """The Messages page's thread table, as rows of 4 cells."""
    win = main_window(5)
    c_page({"page": "messages"})
    rows, cells = [], [e.window_text() for e in _all(win, "DataItem", "MessagesPage")]
    lists = [e.window_text() for e in _all(win, "ListItem", "MessagesPage")]
    for i in range(0, len(cells) - len(cells) % 4, 4):
        rows.append(cells[i:i + 4])
    return {"rows": rows, "peers": lists}


def c_select_peer(a):
    win = main_window(5)
    c_page({"page": "messages"})
    for item in _all(win, "ListItem", "MessagesPage"):
        if a["peer"].upper() in item.window_text().upper():
            item.click_input()
            time.sleep(0.3)
            return {"selected": item.window_text()}
    return {"selected": None}


def c_connect(a):
    win = main_window(5)
    c_page({"page": "chat"})
    edits = _edits(win, "ChatPage")
    edits[0].set_edit_text(a["call"])
    go = [b for b in _visible(_all(win, "Button", "ChatPage")) if b.window_text() == "Connect"]
    _press(go[0])
    time.sleep(0.5)
    return {"connecting": a["call"], "dialogs": _close_dialogs(win)}


def _session(win):
    """The visible chat session's widgets: state label, screen, line, buttons."""
    views = _visible(_all(win, None, "SessionView"))
    if not views:
        raise RuntimeError("no chat session is showing")
    return views


def c_chat_state(_a):
    win = main_window(5)
    c_page({"page": "chat"})
    texts = [e.window_text() for e in _visible(_all(win, "Text", "SessionView"))]
    tabs = [e.window_text() for e in _visible(_all(win, "TabItem", "ChatPage"))]
    return {"labels": texts, "tabs": tabs}


def c_chat_screen(_a):
    win = main_window(5)
    c_page({"page": "chat"})
    out = []
    for e in _visible(_all(win, None, "SessionView.QPlainTextEdit")):
        try:
            out.append(e.iface_value.CurrentValue)
        except Exception:                           # noqa: BLE001 - try the text pattern
            try:
                out.append(e.iface_text.DocumentRange.GetText(-1))
            except Exception:                       # noqa: BLE001
                out.append(e.window_text())
    return {"screens": out}


def c_chat_send(a):
    win = main_window(5)
    c_page({"page": "chat"})
    line = [e for e in _visible(_all(win, "Edit", "SessionView")) if "QPlainTextEdit" not in e.element_info.automation_id]
    send = [b for b in _visible(_all(win, "Button", "SessionView")) if b.window_text() == "Send"]
    for text in a["lines"]:
        line[0].set_edit_text(text)
        _press(send[0])
        time.sleep(a.get("gap_s", 0.2))
    return {"sent": len(a["lines"])}


def c_chat_disconnect(_a):
    win = main_window(5)
    c_page({"page": "chat"})
    bye = [b for b in _visible(_all(win, "Button", "SessionView")) if b.window_text() == "Disconnect"]
    if not bye:
        return {"disconnect": False}
    _press(bye[0])
    return {"disconnect": True}


def c_dump(a):
    win = main_window(5)
    lines = []

    def walk(e, d=0):
        info = e.element_info
        try:
            vis = e.is_visible()
        except Exception:                           # noqa: BLE001
            vis = None
        if a.get("visible_only") and not vis:
            return
        lines.append("  " * d + f"{info.control_type} name={info.name[:70]!r} auto={info.automation_id!r} vis={vis}")
        if d < 14:
            for c in e.children():
                walk(c, d + 1)
    walk(win)
    return {"tree": lines}


def c_log_tail(a):
    path = Path(os.environ["APPDATA"]) / "openhamshack-packet" / "data" / "logs" / "shack-packet.log"
    if not path.exists():
        return {"lines": [], "path": None}
    return {"lines": path.read_text(encoding="utf-8", errors="replace").splitlines()[-a.get("n", 50):],
            "path": str(path)}


COMMANDS = {k[2:]: v for k, v in globals().items() if k.startswith("c_")}


def run(cmd: dict) -> dict:
    try:
        return {"ok": True, **COMMANDS[cmd["op"]](cmd)}
    except Exception as e:                          # noqa: BLE001 - the harness reads it
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}


PORT = 47001


def serve(port: int = PORT) -> None:
    """One JSON request per line, one JSON answer per line, on loopback
    only: the harness reaches it through an SSH tunnel. A request at a time
    (UI Automation calls are not made from two threads at once)."""
    import socket
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "serve.pid").write_text(str(os.getpid()))
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        try:
            with conn, conn.makefile("rwb") as f:
                for raw in f:
                    try:
                        cmd = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    res = run(cmd)
                    res["id"] = cmd.get("id")
                    f.write((json.dumps(res) + "\n").encode())
                    f.flush()
                    if cmd.get("op") == "quit":
                        return
        except OSError:
            # The harness went away mid-request (its tunnel closed, it was
            # stopped): the next one connects afresh.
            continue


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve()
    else:
        print(json.dumps(run({"op": sys.argv[2] if len(sys.argv) > 2 else "ping",
                              **json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}")}), indent=1))
