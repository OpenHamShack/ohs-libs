"""Where a desktop app keeps its own files.

Under the same per-user OpenHamShack directory as the shared station store
(SYNC.md §2.1) and the pad lock (CONTROLLER-PAD.md §6.2), one folder per
app kind:

- Linux:   $XDG_DATA_HOME/openhamshack/<kind>  (default ~/.local/share/...)
- Windows: %LOCALAPPDATA%\\OpenHamShack\\<kind>
- macOS:   ~/Library/Application Support/OpenHamShack/<kind>

Windows uses LOCALAPPDATA, not the roaming APPDATA, for the reason SYNC.md
gives for station.db: a roaming profile copies files at logoff, and copying
a live SQLite file (an app's own database, beside its settings) corrupts
it. Keeping every OpenHamShack file under one base also means one folder
to back up or delete. The kind ("qo100wb", "icom", ...) rather than the
display name, so a renamed product keeps its settings; kinds never clash
with the shared names at that level (station.db, pad/).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ._i18n import _

_KIND = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def base_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "OpenHamShack"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenHamShack"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "openhamshack"


def data_dir(kind: str, create: bool = True) -> Path:
    """The app's writable folder, made (mode 0700 on POSIX) when missing."""
    if not _KIND.match(kind):
        raise ValueError(_("Not an app kind: {kind}").format(kind=kind))
    d = base_dir() / kind
    if create:
        d.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(d, 0o700)
    return d
