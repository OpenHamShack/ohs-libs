"""Running the skimmer beside a receiver (shack_cw.skim.runner): a child
process by default, a thread as the fallback; results through a callback;
a full queue drops blocks rather than stalling receive; a worker that dies
is reported and started again. And nothing the skimmer imports can key."""
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from shack_cw.skim import runner, synth

RATE = 24_000.0


class Sink:
    def __init__(self):
        self.lock = threading.Lock()
        self.got: list = []

    def __call__(self, kind, payload):
        with self.lock:
            self.got.append((kind, payload))

    def kinds(self, kind):
        with self.lock:
            return [p for k, p in self.got if k == kind]


def wait(cond, secs=60.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def feed(r, z, t0=0.0, pace=0.0):
    blk = int(0.075 * RATE)
    for i in range(0, len(z), blk):
        while not r.put(z[i:i + blk], t=t0 + i / RATE, centre=0.0, rate=RATE, lo=-0.42 * RATE, hi=0.42 * RATE,
                        band="20m"):
            if r.state != "running":
                return
            time.sleep(0.01)
        if pace:
            time.sleep(pace)


@pytest.mark.parametrize("mode", ["process", "thread"])
def test_runs_and_spots(mode):
    z, st = synth.selftest_band(RATE, 30.0)
    sink = Sink()
    r = runner.SkimRunner(sink, mode=mode, max_channels=16)
    r.start()
    try:
        feed(r, z)
        assert wait(lambda: {s["call"] for sp in sink.kinds("spots") for s in sp} >= {s.call for s in st}), \
            sink.kinds("spots")
        assert wait(lambda: sink.kinds("snapshot"))
        snap = sink.kinds("snapshot")[-1]
        assert {"channels", "carriers", "spotsRev", "segment", "cover", "t"} <= set(snap)
        assert wait(lambda: any(s.get("channels") for s in sink.kinds("status")))
        r.request_spots()
        assert wait(lambda: sink.kinds("spotlist"))
        assert {s["call"] for s in sink.kinds("spotlist")[-1]} >= {s.call for s in st}
    finally:
        r.stop()
    assert r.state == "off" and not r.alive()


def test_a_full_queue_drops_and_never_blocks():
    sink = Sink()
    r = runner.SkimRunner(sink, mode="thread")
    r.start()
    try:
        # Ten seconds of blocks as fast as they come: put() must answer at
        # once every time, counting what it could not take.
        z = (np.random.default_rng(0).standard_normal(int(10 * RATE)) * 0.1).astype(np.complex64)
        blk = int(0.075 * RATE)
        worst = 0.0
        for i in range(0, len(z), blk):
            t = time.perf_counter()
            r.put(z[i:i + blk], t=i / RATE, centre=0.0, rate=RATE, lo=-1e4, hi=1e4)
            worst = max(worst, time.perf_counter() - t)
        assert worst < 0.05
        assert r.dropped > 0
    finally:
        r.stop()


@pytest.mark.parametrize("mode", ["process", "thread"])
def test_a_dead_worker_is_reported_and_restarted(mode, monkeypatch):
    monkeypatch.setattr(runner, "BACKOFF", (0.5,))
    sink = Sink()
    r = runner.SkimRunner(sink, mode=mode)
    r.start()
    try:
        z, _ = synth.selftest_band(RATE, 3.0)
        feed(r, z)
        r.crash_for_test()
        assert wait(lambda: any(s["state"] == "error" for s in sink.kinds("status")), 30)
        err = [s for s in sink.kinds("status") if s["state"] == "error"][0]
        assert err["why"]
        assert wait(lambda: r.state == "running" and r.alive(), 30)
        assert r.restarts == 1
        feed(r, z, t0=10.0)
        assert wait(lambda: len(sink.kinds("snapshot")) > 0)
    finally:
        r.stop()


def test_the_skimmer_imports_nothing_that_can_key():
    # Import the skimmer and its runner in a clean interpreter and look at
    # what came with them: numpy, shack_cw and the standard library only -
    # no rig control, no sound output, no app.
    code = ("import sys, shack_cw.skim, shack_cw.skim.runner, shack_cw.skim.bench; "
            "print('\\n'.join(sorted(m.split('.')[0] for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True).stdout.split()
    tops = set(out)
    for bad in ("shack_rig", "shack_pad", "ohscore", "ohsradio", "ohsnb", "serial", "sounddevice", "pyaudio",
                "SoapySDR", "adi", "iio", "shack_client"):
        assert bad not in tops, bad
    stdlib = set(sys.stdlib_module_names)
    others = {m for m in tops if m not in stdlib and not m.startswith("_")}
    assert others - {"sitecustomize"} <= {"numpy", "shack_cw"}, others


def test_the_worker_does_not_import_the_apps_main(tmp_path):
    # An app started from a script (a desktop launcher, a frozen build's
    # entry point): spawn would run that script again in the worker as
    # __mp_main__ - the whole app's imports, transmit path and all. (Run as
    # `python3 -m ohsradio` it would not: multiprocessing skips a package's
    # __main__.) The script here leaves a mark each time it is run; the
    # worker must not leave one.
    marks = tmp_path / "marks"
    app = tmp_path / "app.py"
    app.write_text(f"""
import os, sys, time
with open({str(marks)!r}, "a") as f:
    f.write(str(os.getpid()) + "\\n")
if __name__ == "__main__":
    from shack_cw.skim import runner, synth
    got = []
    r = runner.SkimRunner(lambda k, p: got.append(k), mode="process")
    r.start()
    z, _ = synth.selftest_band(24000.0, 3.0)
    for i in range(0, len(z), 1800):
        r.put(z[i:i + 1800], t=i / 24000.0, centre=0.0, rate=24000.0, lo=-1e4, hi=1e4)
    end = time.monotonic() + 30
    while "snapshot" not in got and time.monotonic() < end:
        time.sleep(0.1)
    r.stop()
    print("snapshot" in got, sys.modules["__main__"].__file__ == {str(app)!r})
""")
    env = dict(__import__("os").environ, PYTHONPATH=":".join(sys.path))
    out = subprocess.run([sys.executable, str(app)], capture_output=True, text=True, env=env, timeout=120,
                         cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "True"], out.stdout + out.stderr
    assert len(marks.read_text().split()) == 1, "the worker ran the app's main script"
