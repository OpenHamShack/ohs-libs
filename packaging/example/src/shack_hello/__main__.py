"""OpenHamShack Hello's entry point: the window, or (with --server) the
server the window starts.

The server is a separate process even though it is tiny, because that is
the harder of shack_desktop's two shapes to bundle: a frozen program has
to start itself again as its own server (self_command), on every OS. If
the example passes, the real apps' servers will start the same way.
"""
from __future__ import annotations

import sys
from pathlib import Path

import shack_desktop

KIND = "hello"


def _icon() -> str | None:
    # The window icon on Linux (Windows and macOS take it from the
    # executable): the brand PNG the .deb installs, or the page's own SVG
    # rendered nowhere else - pywebview's GTK backend wants a file.
    for p in (Path(sys.prefix) / "share/icons/hicolor/256x256/apps/com.openhamshack.hello.png",
              Path("/usr/share/icons/hicolor/256x256/apps/com.openhamshack.hello.png")):
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    if "--server" in sys.argv:
        from .server import serve
        return serve()
    return shack_desktop.run(command=shack_desktop.self_command("shack_hello", "--server"),
                             title="OpenHamShack Hello", kind=KIND, icon=_icon(), size=(900, 640))


if __name__ == "__main__":
    sys.exit(main())
