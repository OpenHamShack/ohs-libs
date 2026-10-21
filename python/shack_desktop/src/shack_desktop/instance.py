"""One running copy per app, and a second launch brings the first forward.

Through files in the app's data folder, the same way shack_pad hands the
pad between apps (CONTROLLER-PAD.md §6.2), and for the same reason: a
desktop app opens no socket for anything else to talk to. A loopback
listener for "please show yourself" would be exactly the kind of server
the rule forbids, and a named pipe or D-Bus name is different on each OS.

- `instance.lock`: the running copy holds an OS lock on it (flock on
  POSIX, msvcrt.locking on Windows). The OS drops the lock when the
  process dies however it dies, so a crash never leaves the app refusing
  to start.
- `instance.focus`: a second launch that cannot take the lock writes this
  file and exits; the running copy looks for it every 250 ms (the pad's
  poll interval, so a click feels the same on both) and, finding it,
  deletes it and raises its window.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, IO

POLL_SECONDS = 0.25


def _lock(f: IO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f: IO) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class SingleInstance:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.lock_path = self.dir / "instance.lock"
        self.request_path = self.dir / "instance.focus"
        self._file: IO | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        """True when this process is now the app's one running copy."""
        self.dir.mkdir(parents=True, exist_ok=True)
        f = open(self.lock_path, "a+")
        if not _lock(f):
            f.close()
            return False
        self._file = f
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
        # A request left by a launch that raced the previous copy's exit is
        # not for us: we have only just started, and are in front anyway.
        self.request_path.unlink(missing_ok=True)
        return True

    def request_focus(self) -> None:
        """From a second launch: ask the running copy to come forward."""
        tmp = self.request_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}))
        os.replace(tmp, self.request_path)   # atomic: never a half-written request

    def take_request(self) -> bool:
        try:
            self.request_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def watch(self, on_request: Callable[[], None]) -> None:
        def run() -> None:
            while not self._stop.wait(POLL_SECONDS):
                if self.take_request():
                    try:
                        on_request()
                    except Exception:   # a window mid-close must not end the watcher
                        pass
        self._thread = threading.Thread(target=run, daemon=True, name="shack-desktop instance")
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._file is not None:
            _unlock(self._file)
            self._file.close()
            self._file = None
