"""The server side of a shell launch: what an app's server process reads.

When the shell starts the app's server as a separate process (run(command=
...)), it puts three variables in that process's environment (guard.py
ENV_*) and holds a pipe to its stdin. The server:

    env = shack_desktop.server_env()        # host, port, token
    shack_desktop.watch_parent()            # exit when the window's process does
    app = env.guard.wsgi(app)                # or .aiohttp_middleware(), .check_handler()
    serve(app, host=env.host, port=env.port)

watch_parent() matters because a window can die without saying goodbye
(killed, crashed, logged out). Two signals, either enough: the stdin pipe
reaching end-of-file, which every OS reports the moment the other end's
process is gone, and the parent's process id disappearing, checked every
second, for a frozen Windows GUI program whose C runtime may not have set
up file descriptor 0 at all. On Linux the shell also asks the kernel to
signal the child when it dies (PR_SET_PDEATHSIG, shell.py).
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from socketserver import TCPServer
from typing import Callable

from .guard import ENV_HOST, ENV_PORT, ENV_TOKEN, Guard

ENV_PARENT = "SHACK_DESKTOP_PARENT"


@dataclass(frozen=True)
class ServerEnv:
    host: str
    port: int
    token: str

    @property
    def guard(self) -> Guard:
        return Guard(self.token, self.port)


class LoopbackHTTPServer(ThreadingHTTPServer):
    """http.server's ThreadingHTTPServer without its reverse DNS lookup.

    HTTPServer.server_bind calls socket.getfqdn() on the address it bound,
    and on macOS that reverse lookup of 127.0.0.1 goes to mDNS, which an
    app opened from Finder may not use without the Local Network
    permission: the frozen example hung there before it ever listened. The
    name is only used in CGI variables, so the address stands in for it.
    Use this for any http.server-based app server."""

    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def is_server_child() -> bool:
    """True inside a server the shell started (the environment is set)."""
    return bool(os.environ.get(ENV_TOKEN))


def server_env() -> ServerEnv:
    host = os.environ.get(ENV_HOST, "127.0.0.1")
    # Loopback or nothing. A server told anything else was not started by
    # this shell, and binding it would put the app on the network.
    if host not in ("127.0.0.1", "::1"):
        raise RuntimeError(f"{ENV_HOST}={host!r}: a desktop app's server binds loopback only")
    g = Guard.from_env()
    return ServerEnv(host, int(g.port or 0), g.token)


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    if os.getppid() != pid:          # re-parented: the shell is gone
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch_parent(on_gone: Callable[[], None] | None = None) -> None:
    """Exit (or call on_gone) when the shell that started us has gone."""
    done = threading.Event()

    def gone() -> None:
        if done.is_set():
            return
        done.set()
        if on_gone is not None:
            on_gone()
        else:
            os._exit(0)     # no cleanup to wait for: nobody is left to show it to

    def read_stdin() -> None:
        try:
            while os.read(0, 4096):
                pass
        except OSError:
            return          # no usable stdin: the pid watch below decides
        gone()

    def poll_parent(pid: int) -> None:
        while not done.wait(1.0):
            if not _parent_alive(pid):
                gone()

    threading.Thread(target=read_stdin, daemon=True, name="shack-desktop stdin").start()
    parent = os.environ.get(ENV_PARENT)
    if parent and parent.isdigit():
        threading.Thread(target=poll_parent, args=(int(parent),), daemon=True, name="shack-desktop parent").start()


def self_command(module: str, *args: str) -> list[str]:
    """The command that runs this same program again with `args`: for an app
    whose server is its own entry point with a flag (`--server`).

    Frozen (PyInstaller), the program is sys.executable itself; from source
    it is `python -m <module>`. Either way the server is the code the window
    was built from, never something found on PATH.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", module, *args]
