#!/usr/bin/env python3
"""Type text into a Proxmox VM's console from here, for a Windows VM that has
nothing yet but a display (at OOBE: Shift+F10, then the kit's setup.cmd).

    sendkeys.py VMID --keys shift-f10
    sendkeys.py VMID --text 'for %d in (D E F G) do if exist %d:\\setup.cmd %d:\\setup.cmd' --enter

The guest is set to a UK keyboard (en-GB at OOBE), so the characters that
differ from US positions are mapped for UK: " is shift-2, @ is
shift-apostrophe, # is the US backslash key, \\ and | are the ISO key left of
Z ("less" to QEMU). Each key goes through `qm monitor` on office as one
sendkey, in one SSH session, a little apart so the guest keeps up.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

PLAIN = {c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
PLAIN.update({" ": "spc", "-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right",
              ";": "semicolon", "'": "apostrophe", ",": "comma", ".": "dot", "/": "slash",
              "\\": "less", "#": "backslash", "`": "grave_accent"})
SHIFTED = {"!": "1", '"': "2", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "minus", "+": "equal", "{": "bracket_left", "}": "bracket_right", ":": "semicolon",
           "@": "apostrophe", "<": "comma", ">": "dot", "?": "slash", "|": "less", "~": "backslash"}


def keys_for(text: str) -> list[str]:
    out = []
    for c in text:
        if c in PLAIN:
            out.append(PLAIN[c])
        elif c.isalpha() and c.lower() in PLAIN:
            out.append("shift-" + c.lower())
        elif c in SHIFTED:
            out.append("shift-" + SHIFTED[c])
        else:
            raise SystemExit(f"no key for {c!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vmid")
    ap.add_argument("--text", default="")
    ap.add_argument("--keys", default="", help="comma-separated QEMU key names, sent first")
    ap.add_argument("--enter", action="store_true")
    ap.add_argument("--host", default="office")
    a = ap.parse_args()
    keys = [k for k in a.keys.split(",") if k] + keys_for(a.text) + (["ret"] if a.enter else [])
    # The monitor reads commands from stdin; a short sleep between keys (done
    # on office, so the pacing is not at the mercy of the SSH stream).
    script = "".join(f"echo 'sendkey {k}'; sleep 0.08; " for k in keys)
    cmd = f"( {script} sleep 0.5 ) | sudo qm monitor {a.vmid} >/dev/null 2>&1"
    return subprocess.call(["ssh", a.host, cmd])


if __name__ == "__main__":
    sys.exit(main())
