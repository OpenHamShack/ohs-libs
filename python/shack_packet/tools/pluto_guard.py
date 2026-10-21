#!/usr/bin/env python3
"""A Pluto transmitter guard in a process of its own, for the two-station
rig (twostation.py): quiets the Pluto if the process using it dies, or if
its transmitter has been up longer than it may be.

PlutoLink quiets the transmitter after every over, at close, on any error,
and from a watchdog thread of its own - but every one of those runs inside
the process that keyed it. Kill that process (kill -9, a crash in native
code, the harness's own stuck-TX test) and none of them runs: the AD9361
keeps whatever its attributes last said, so the TX LO stays powered at the
over's gain. That is the same state a rebooted Pluto comes up in (GUIDE §5,
"a Pluto reboot keys its TX LO"), and it radiates until something quiets it.

So this runs beside it, with its own libiio context, and every POLL_S:

- if the watched process is gone: quiet, read back, report, exit;
- if TX LO powerdown has read 0 for longer than --max-key-s + 2 s: quiet
  (the same limit as PlutoLink's own watchdog plus a second, so the two do
  not race, and this one only acts when that one has not).

It writes only the two quiet values (powerdown = 1, hardwaregain = -89.75),
never anything that could key.

    pluto_guard.py --uri ip:192.168.2.1 --pid 12345 --max-key-s 8
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time

POLL_S = 0.2
TX_ATTEN_MAX = "-89.75"


def load_iio():
    try:
        import iio
        return iio
    except ImportError:
        # The distribution's python3-libiio lives outside a venv.
        spec = importlib.util.spec_from_file_location("iio", "/usr/lib/python3/dist-packages/iio.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["iio"] = mod
        return mod


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie child still answers kill(0); its state says it is gone.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class Guard:
    def __init__(self, uri: str, iio_module=None):
        iio = iio_module or load_iio()
        self.ctx = iio.Context(uri)
        phy = self.ctx.find_device("ad9361-phy")
        self.lo = phy.find_channel("altvoltage1", True)
        self.tx = phy.find_channel("voltage0", True)

    def state(self) -> tuple[str, str]:
        return self.lo.attrs["powerdown"].value.strip(), self.tx.attrs["hardwaregain"].value.strip()

    def quiet(self) -> bool:
        for attr, value in ((self.lo.attrs["powerdown"], "1"), (self.tx.attrs["hardwaregain"], TX_ATTEN_MAX)):
            try:
                attr.value = value
            except OSError as e:
                print(f"pluto_guard: write failed: {e}", flush=True)
        pd, gain = self.state()
        return pd == "1" and float(gain.split()[0]) <= float(TX_ATTEN_MAX) + 0.01


def run(uri: str, pid: int, max_key_s: float, iio_module=None, clock=time.monotonic, sleep=time.sleep,
        stop=lambda: False) -> str:
    g = Guard(uri, iio_module)
    up_since = None
    while not stop():
        if not alive(pid):
            ok = g.quiet()
            print(f"pluto_guard: process {pid} is gone; transmitter {'quiet' if ok else 'NOT QUIET'} "
                  f"({g.state()})", flush=True)
            return "gone-quiet" if ok else "gone-not-quiet"
        try:
            pd, _gain = g.state()
        except OSError as e:
            print(f"pluto_guard: read failed: {e}", flush=True)
            sleep(POLL_S)
            continue
        now = clock()
        if pd == "0":
            up_since = now if up_since is None else up_since
            if now - up_since > max_key_s + 2.0:
                ok = g.quiet()
                print(f"pluto_guard: TX up {now - up_since:.1f} s, past {max_key_s} + 2 s; quieted: {ok}",
                      flush=True)
                up_since = None
        else:
            up_since = None
        sleep(POLL_S)
    return "stopped"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="ip:192.168.2.1")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--max-key-s", type=float, default=8.0)
    a = ap.parse_args()
    result = run(a.uri, a.pid, a.max_key_s)
    return 0 if result in ("gone-quiet", "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
