"""Install a shack-sdr wheel into a fresh venv and prove it works there:
it imports from the wheel (not a checkout), decodes RTTY from a synthetic
signal through the Host, lists its catalogue, and reports every encumbered
codec missing - nothing is hidden inside the wheel (RADIO-SDR-PLAN §5.5).

    python wheel_smoke.py <wheel> <venv dir>
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

CHECK = r"""
import os, sys, time, numpy as np, shack_sdr
from shack_sdr import Host, _native, codecs
assert "site-packages" in shack_sdr.__file__, shack_sdr.__file__
text = "CQ CQ DE GM4BRI GM4BRI K\n"
x = np.frombuffer(_native.testsig_rtty(text, snr_db=0.0, seed=3), np.float32)
host = Host(cpu_max=0.0, ring_s=30.0)
sid = host.start("rtty", {}, "audio", 12000, center_hz=1000.0)
for k in range(0, len(x), 1200):
    host.feed(sid, x[k:k + 1200], k / 12000, k)
got, end = "", time.monotonic() + 20
while not got.endswith("K\n") and time.monotonic() < end:
    got += "".join(i["text"] for i in host.poll() if i["type"] == "text")
    time.sleep(0.02)
assert host.close()
assert got == text, repr(got)
assert any(e["id"] == "rtty" for e in shack_sdr.catalogue())
loader = codecs.Loader(None)
loader._system = lambda names: []      # only what the wheel itself could bring
st = {s["id"]: s["state"] for s in loader.status()}
assert st["faad2"] == st["fdk-aac"] == st["mbelib"] == "missing", st
print("wheel ok:", shack_sdr.__version__, sys.version.split()[0], got.strip())
"""


def main() -> int:
    wheel, where = Path(sys.argv[1]), Path(sys.argv[2])
    venv.EnvBuilder(with_pip=True, clear=True).create(where)
    py = where / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", str(wheel)], check=True)
    subprocess.run([str(py), "-c", CHECK], check=True, cwd=str(where))
    return 0


if __name__ == "__main__":
    sys.exit(main())
