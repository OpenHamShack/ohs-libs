"""Standalone desktop apps: the pad belongs to the app the operator is using.

No platform, no service and no listening socket - the "no servers inside
desktop apps" rule - so ownership is a lock file (CONTROLLER-PAD.md §6.2):

- Per user: `<store dir>/pad/<model>.lock`, the store dir being the one
  every OpenHamShack app on the machine shares (SYNC.md §2.1). The owner
  holds an OS lock on it for as long as it has the device open; a crashed
  owner's lock is released by the OS, so a dead app never keeps the pad.
- An app that gains focus writes `<model>.want` ({"pid", "app", "at"}) and
  waits for the lock.
- The owner looks at `<model>.want` every 250 ms. When another app wants
  the pad and this one is not on air from it, it blanks the keys, closes
  the device and lets go (target: hand-over under 1 s). On air, it keeps
  the pad until unkeyed, with every key blank but transmit, which shows ON
  AIR and the app's name.
- An app that loses focus does nothing until asked, so switching to a
  browser does not take the pad from the last OpenHamShack app.

Windows locking (msvcrt.locking, the same LockFile family as LockFileEx) is
UNTESTED; Linux and macOS use flock.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import diagnose
from .drivers import by_model
from .drivers.base import Driver
from .router import InputHost

POLL_SECONDS = 0.25
OPEN_RETRY = 2.0


def default_store_dir() -> Path:
    """The per-user directory every OpenHamShack app shares (SYNC.md §2.1)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "OpenHamShack"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenHamShack"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "openhamshack"


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        return True                     # no cheap check; a stale want only costs one hand-over [untested]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PadFocus:
    """The lock and the want file for one pad model. Not thread-safe: one
    DesktopPad drives it."""

    def __init__(self, model: str, app: str, store_dir: Path | None = None, token: str | None = None):
        self.model, self.app = model, app
        self.dir = Path(store_dir or default_store_dir()) / "pad"
        self.lock_path = self.dir / f"{model}.lock"
        self.want_path = self.dir / f"{model}.want"
        # Who "we" are in the want file. A pid is enough between processes;
        # the token tells two claimants in one process apart (tests, or an
        # app with two windows that each claim).
        self.token = token or f"{os.getpid()}"
        self.fd: int | None = None

    @property
    def owns(self) -> bool:
        return self.fd is not None

    def want(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.want_path.with_name(f"{self.want_path.name}.{self.token}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "token": self.token, "app": self.app, "at": time.time()}))
        tmp.replace(self.want_path)

    def read_want(self) -> dict | None:
        try:
            w = json.loads(self.want_path.read_text())
        except (OSError, ValueError):
            return None
        return w if isinstance(w, dict) else None

    def i_want(self) -> bool:
        w = self.read_want()
        return bool(w and w.get("token") == self.token)

    def wanted_by_other(self) -> dict | None:
        """The other app that wants the pad, if a live one does."""
        w = self.read_want()
        if not w or w.get("token") == self.token:
            return None
        pid = w.get("pid")
        if not isinstance(pid, int) or not _alive(pid):
            return None
        return w

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt  # noqa: PLC0415
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        if self.i_want():
            try:
                self.want_path.unlink()
            except OSError:
                pass
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt  # noqa: PLC0415
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


class DesktopPad:
    """A desktop app's pad: focus, the device and its InputHost together.

        host = InputHost([InProcessApp(...)])
        pad = DesktopPad(host, app="shack-qo100nb")
        pad.start()                    # a thread polls every 250 ms
        window.focusIn -> pad.focus_gained()
        app exit       -> pad.close()

    open_device() returns an opened Driver or None (default: the first pad
    of this model that find() sees); tests pass one built on FakeHid.
    """

    def __init__(self, host: InputHost, app: str, model: str = "ajazz-akp03e", store_dir: Path | None = None,
                 open_device: Callable[[], Driver | None] | None = None, token: str | None = None):
        self.host, self.model = host, model
        self.focus = PadFocus(model, app, store_dir, token)
        self.open_device = open_device or self._open_default
        self.device: Driver | None = None
        self.thread: threading.Thread | None = None
        self.stop_serving = threading.Event()
        self.wanted = False
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        # Why the last open failed, in words (diagnose.explain), and when to
        # look again: every 250 ms is fine for a pad that is there, but on
        # Windows each look is a full HID enumeration, so a pad that is
        # missing or refused is looked for every OPEN_RETRY seconds instead.
        self.open_error = ""
        self._retry_at = 0.0

    def _open_default(self) -> Driver | None:
        drv = by_model(self.model)
        paths = drv.find({}) if drv else []
        return drv.open(paths[0]) if paths else None

    def focus_gained(self) -> None:
        if not self.focus.owns:
            self.wanted = True
            self.focus.want()
        self.poll()

    def serving(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def poll(self) -> None:
        """One look at the lock and the want file (every POLL_SECONDS)."""
        if not self.focus.owns:
            if self.wanted and self.focus.i_want() and self.focus.try_acquire():
                self.wanted = False
                self._open()
            elif self.wanted and not self.focus.i_want():
                self.wanted = False     # a later focus elsewhere superseded ours
            return
        other = self.focus.wanted_by_other()
        if other:
            if self.host.on_air_apps():
                # On air from this app: keep the pad until unkeyed, every
                # key blank but the one that stops it.
                if self.host.handover != other.get("app"):
                    self.host.handover = str(other.get("app") or "")
                    self.host.generation += 1
                return
            self._let_go()
            return
        if self.host.handover:
            self.host.handover = None
            self.host.generation += 1
        if not self.serving():
            if self.device is not None:
                # Its loop ended by itself (unplugged, or a read error).
                # Not straight back: each open wakes the pad, which blanks
                # every key and dims them to nothing first, so a pad that
                # keeps failing reopened every 250 ms would flicker at 4 Hz.
                self.device = None
                self._retry_at = max(self._retry_at, time.monotonic() + OPEN_RETRY)
            self._open()                # unplugged while we owned it: look again

    def status(self) -> dict:
        """Where the pad stands, for a settings page: state is one of
        "serving" (open and working), "waiting" (another OpenHamShack app
        has it), "missing" (none found), "error" (found, would not open or
        stopped; `error` says why and what to do), "idle" (not asked for)."""
        dev = self.device if self.serving() else None
        error = self.open_error or self.host.last_error
        if dev is not None:
            state = "serving"
        elif not self.focus.owns:
            state = "waiting" if self.wanted else "idle"
        elif error:
            state = "error"
        else:
            state = "missing"
        return {"state": state, "model": self.model, "name": type(dev).name if dev else "",
                "path": dev.path if dev else "", "error": "" if dev else error,
                "testing": self.host.testing()}

    def _open(self) -> None:
        if self.serving():
            return
        now = time.monotonic()
        if now < self._retry_at:
            return
        try:
            dev = self.open_device()
        except OSError as exc:
            dev = None
            self.open_error = diagnose.explain(exc, doing="open")
            self.host.log(f"pad open: {exc}")
        else:
            if dev is None:
                self.open_error = ""
        if dev is None:
            self._retry_at = now + OPEN_RETRY
            return
        self.open_error = ""
        self._retry_at = 0.0
        self.device = dev
        self.stop_serving = threading.Event()
        self.thread = threading.Thread(target=self.host.serve, args=(dev, self.stop_serving), daemon=True,
                                       name="shack-pad desktop")
        self.thread.start()

    def _let_go(self) -> None:
        """Blank, close (InputHost.detach, which unkeys first should anything
        be keyed after all), then release the lock."""
        self.stop_serving.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        self.thread, self.device = None, None
        self.host.handover = None
        self.focus.release()

    def start(self) -> None:
        def run() -> None:
            while not self._stop.wait(POLL_SECONDS):
                try:
                    self.poll()
                except Exception as exc:   # a bad want file or a full disk must not end the poller
                    self.host.log(f"pad focus: {exc}")
        self._poller = threading.Thread(target=run, daemon=True, name="shack-pad focus")
        self._poller.start()

    def close(self) -> None:
        self._stop.set()
        if self.focus.owns:
            self._let_go()
