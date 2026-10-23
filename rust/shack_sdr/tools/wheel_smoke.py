"""Install a shack-sdr wheel into a fresh venv and prove it works there:
it imports from the wheel (not a checkout), decodes RTTY from a synthetic
signal through the Host and two PSK31 stations in one passband through one
session, plays an old DAB (MP2) service from the DAB
transmitter model through our own decoder, plays a stereo FM station with
RDS from the FM test transmitter and names a band of them, lists its
catalogue (with DAB partly available: no faad2 here), and reports every
encumbered codec
missing - nothing is hidden inside the wheel (RADIO-SDR-PLAN §5.5).

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
psk = [(1000.0, "CQ de GM4BRI k\n", 1), (1600.0, "QRZ de G4XYZ k\n", 2)]
x = np.zeros(9 * 12000, np.float32)
for f, t, seed in psk:
    s = np.frombuffer(_native.testsig_psk(t, centre=f, snr_db=0.0, seed=seed), np.float32)
    x[:len(s)] += s[:len(x)]
host = Host(cpu_max=0.0, ring_s=30.0)
sid = host.start("psk", {"channels": "-300,300"}, "audio", 12000, center_hz=1300.0)
for k in range(0, len(x), 1200):
    host.feed(sid, x[k:k + 1200], k / 12000, k)
chans, end = ["", ""], time.monotonic() + 20
while sum(map(len, chans)) < 30 and time.monotonic() < end:
    for i in host.poll():
        if i["type"] == "text":
            chans[0 if i["offsetHz"] < 0 else 1] += i["text"]
    time.sleep(0.02)
assert host.close()
assert chans == [t for _, t, _ in psk], chans
loader = codecs.Loader(None)
loader._system = lambda names: []      # only what the wheel itself could bring
st = {s["id"]: s["state"] for s in loader.status()}
assert st["faad2"] == st["fdk-aac"] == st["mbelib"] == "missing", st
x = np.frombuffer(_native.testsig_dab(14, snr_db=15.0), np.complex64)
host = Host(cpu_max=0.0, ring_s=4.0)
sid = host.start("dab", {"service": "C221"}, "iq", 2_048_000)
for k in range(0, len(x), 204_800):
    host.feed(sid, x[k:k + 204_800], k / 2.048e6, k)
end = time.monotonic() + 30
while host.status(sid)["audio"]["queued"] < 24_000 and time.monotonic() < end:
    time.sleep(0.05)
meta = host.status(sid)["meta"]
pcm = [c["pcm"] for c in host.audio(sid, 1.0, rate=12_000, channels=1)]
assert host.close()
assert meta["ensemble"]["label"] == "Shack Test Mux" and meta["playing"] == "C221", meta
assert sum(len(p) for p in pcm) > 5_000, pcm
loader._system = lambda names: []
codecs._loader = loader
entry = next(e for e in shack_sdr.catalogue() if e["id"] == "dab")
assert entry["available"] and entry["partial"] and entry["missing"] == ["faad2"], entry
from shack_sdr import wfm
x = np.frombuffer(_native.testsig_wfm(seconds=3.0, cnr_db=30.0, left_hz=1000.0), np.complex64)
rx = wfm.Receiver()
pcm = np.concatenate([rx.process(x[k:k + 4800], k / 240_000) for k in range(0, len(x), 4800)])
rds = rx.rds()
assert pcm.shape == (144_000, 2) and rds["ps"] == "TEST FM " and rds["ptyName"] == "Pop music" and rds["stereo"], rds
assert np.std(pcm[48_000:, 0]) > 10 * np.std(pcm[48_000:, 1])
# Within the scan's span at 2.4 MS/s: +-(0.45 x rate - 100 kHz).
band = [(-8e5, -30.0, 0xC201, "RADIO 1 ", 10, True), (5e5, -40.0, 0xC202, "RADIO 2 ", 1, False)]
x = np.frombuffer(_native.testsig_fm_band(band, rate=2_400_000.0, seconds=2.0), np.complex64)
found = [(s["hz"], s["ps"]) for s in wfm.scan_band(x, 2_400_000, 98_000_000)]
assert found == [(97_200_000, "RADIO 1 "), (98_500_000, "RADIO 2 ")], found
print("wheel ok:", shack_sdr.__version__, sys.version.split()[0], got.strip(), "| PSK:", [c.strip() for c in chans], "| DAB:", meta["services"][0]["label"],
      "| FM:", rds["ps"], [ps for _, ps in found])
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
