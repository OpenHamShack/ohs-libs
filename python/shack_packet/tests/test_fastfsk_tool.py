"""tools/fastfsk_test.py, the two-Pluto fast-mode harness, against two fake
Plutos joined by a fake air: the keyed gate and the caps refuse before
anything opens, a UI run is counted at the far end, and a connected
transfer arrives with its CRC checked - the harness's own code paths, end
to end, before Brian runs them on the air."""

from __future__ import annotations

import collections
import importlib.util
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from sdrhelp import FakePluto

TOOLS = Path(__file__).parent.parent / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tool = _load("fastfsk_test")
RATE_IQ = 2_457_600


class Air:
    """One direction of the channel: what one fake Pluto pushes with its
    TX LO up arrives at the other's receiver, in order, in white noise.
    The listener starts on an over only once 0.2 s of it is waiting, or the
    over has ended, so its real-time reader never overtakes the talker's
    real-time writer mid-over (a hole a real channel does not have)."""

    def __init__(self, sigma=0.01, seed=1):
        self.q: collections.deque[np.ndarray] = collections.deque()
        self.have = 0
        self.flowing = False
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(seed)
        self.sigma = sigma
        self.last_push = 0.0

    def pushed(self, raw: bytes, live: bool) -> None:
        if not live:
            return
        x = (np.frombuffer(raw, np.int16) >> 4).astype(np.int16)
        with self.lock:
            self.q.append(x)
            self.have += len(x)
            self.last_push = time.monotonic()

    def read(self, n: int) -> np.ndarray:
        out = np.round(self.rng.normal(0, self.sigma * 2047, n)).astype(np.int16)
        with self.lock:
            ended = self.have and time.monotonic() - self.last_push > 0.1
            if not self.flowing and (self.have >= 2 * int(0.2 * RATE_IQ) or ended):
                self.flowing = True
            if not self.flowing:
                return out
            at = 0
            while at < n and self.q:
                head = self.q[0]
                take = min(n - at, len(head))
                out[at:at + take] += head[:take]
                at += take
                self.have -= take
                if take == len(head):
                    self.q.popleft()
                else:
                    self.q[0] = head[take:]
            if at < n:
                self.flowing = False
        return out


class Pushed(list):
    def __init__(self, air: Air):
        super().__init__()
        self.air = air

    def append(self, item):
        super().append(item)
        self.air.pushed(*item)


class FakeGuard:
    """Stands in for shack_pluto.guard.Guard (its own tests cover it)."""

    def __init__(self, runner, max_key_s, gpo=False, max_gain_db=None, **kw):
        self.confirmed = False
        self.gpo, self.max_gain_db = gpo, max_gain_db

    def start(self):
        self.confirmed = True
        return self

    def stop(self):
        self.confirmed = False


@pytest.fixture
def two_plutos(monkeypatch):
    a, b = FakePluto(), FakePluto()
    ab, ba = Air(seed=1), Air(seed=2)
    a.pushed, b.pushed = Pushed(ab), Pushed(ba)
    a.rx_source, b.rx_source = ba.read, ab.read
    a.rx_realtime = b.rx_realtime = True
    which = {"ip:10.0.0.1": a, "ip:10.0.0.2": b}
    monkeypatch.setattr(tool, "load_iio", lambda: _Router(which))
    monkeypatch.setattr(tool, "guard_runner", lambda uri, ssh_port=None: None)
    import shack_pluto.guard
    monkeypatch.setattr(shack_pluto.guard, "Guard", FakeGuard)
    return a, b


class _Router:
    """One iio module for two fake Plutos, by URI."""

    def __init__(self, which):
        self.which = which
        self.Buffer = _RouterBuffer(which)

    def Context(self, uri):
        return self.which[uri].module().Context(uri)


class _RouterBuffer:
    def __init__(self, which):
        self.which = which

    def __call__(self, dev, samples, cyclic):
        pluto = self.which[dev.ctx.uri]
        return pluto.module().Buffer(dev, samples, cyclic)


def test_nothing_keys_without_both_flags_and_the_caps_hold(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(tool, "Station", lambda *a, **k: opened.append(a) or pytest.fail("opened"))
    base = ["--role", "A", "--log-dir", str(tmp_path)]
    for cmd in (["ui"], ["transfer"], ["serve"]):
        for flags in ([], ["--keyed"], ["--operator-present"]):
            with pytest.raises(SystemExit):
                tool.main(base + flags + cmd)
    keyed = ["--keyed", "--operator-present"]
    for bad in (["--max-key-s", "20"], ["--max-airtime-s", "3600"], ["--tx-gain-db", "-3"]):
        with pytest.raises(SystemExit):
            tool.main(base + keyed + bad + ["ui"])
    with pytest.raises(SystemExit):
        tool.main(base + keyed + ["ui", "--count", "5000"])
    with pytest.raises(SystemExit):
        tool.main(base + keyed + ["transfer", "--kb", "1024"])
    assert opened == []


def test_the_openssh_runner_keeps_the_password_off_the_command_line():
    r = tool.OpenSshRunner("192.168.2.1")
    argv = r.argv("sh -c true")
    assert argv[:3] == ["sshpass", "-e", "ssh"] and "analog" not in " ".join(argv)
    assert argv[-2:] == ["root@192.168.2.1", "sh -c true"]
    # The controller box's Pluto: SSH on its own port, and time for
    # PlutoDVB2's slow banner.
    argv = tool.OpenSshRunner("ohs-box", port=30422).argv("true")
    assert argv[argv.index("-p") + 1] == "30422" and "ConnectTimeout=20" in argv


def test_a_ui_run_is_counted_at_the_far_end(two_plutos, tmp_path):
    a_pluto, b_pluto = two_plutos
    log = ["--log-dir", str(tmp_path)]
    done = {}
    listener = threading.Thread(target=lambda: done.update(rc=tool.main(
        ["--role", "B", "--uri", "ip:10.0.0.2", *log, "listen", "--seconds", "14"])))
    listener.start()
    rc = tool.main(["--role", "A", "--uri", "ip:10.0.0.1", "--txdelay-ms", "150", "--keyed", "--operator-present",
                    *log, "ui", "--count", "6", "--per-over", "2", "--gap-s", "0.3"])
    listener.join(30)
    assert rc == 0 and done.get("rc") == 0
    assert a_pluto.tx_quiet() and b_pluto.tx_quiet()
    # Both stations run in this one process, so their lines share the log
    # files; on the air each has its own.
    text = "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "heard 6/6 (100 %)" in text, text[-3000:]


@pytest.mark.parametrize("mode,baud,window,extra", [("g3ruh", 9600, 7, []), ("g3ruh", 38_400, 16, []),
                                                     ("4fsk", 38_400, 16, ["--srej", "--paclen", "512"]),
                                                     ("g3ruh", 9600, 32, ["--adapt", "--srej", "--paclen", "512"])])
def test_a_transfer_arrives_with_its_crc_and_a_throughput(two_plutos, tmp_path, mode, baud, window, extra):
    # 38 400 with a window of 16 is modulo 128: SABME, and 16 frames an
    # over. The modes wider than 25 kHz key only in a band-plan segment
    # for wide data: 1299.375 MHz, the 23 cm DD channel.
    log = ["--log-dir", str(tmp_path)]
    keyed = ["--keyed", "--operator-present", "--txdelay-ms", "150", "--mode", mode, "--baud", str(baud),
             "--window", str(window), "--freq", "1299.375e6", *extra]
    done = {}
    server = threading.Thread(target=lambda: done.update(rc=tool.main(
        ["--role", "B", "--uri", "ip:10.0.0.2", *keyed, *log, "serve", "--seconds", "60", "--once"])))
    server.start()
    rc = tool.main(["--role", "A", "--uri", "ip:10.0.0.1", *keyed, *log, "transfer", "--kb", "4"])
    server.join(60)
    text = "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert rc == 0 and done.get("rc") == 0, text[-3000:]
    assert "MATCHES" in text and "sent and acknowledged" in text and "bit/s of data" in text
    if "--adapt" in extra:
        # Both ends adapted (XID) and said so; I frames came at the top.
        assert "'peer_adapts': True" in text and "by mode {" in text and "'4fsk76800': " in text, text[-3000:]


def test_the_keyed_pair_counts_either_side_of_the_command():
    p = tool.parser()
    after = p.parse_args(["--role", "A", "ui", "--count", "5", "--keyed", "--operator-present"])
    before = p.parse_args(["--role", "A", "--keyed", "--operator-present", "ui"])
    neither = p.parse_args(["--role", "A", "ui"])
    assert tool.keyed(after) and tool.keyed(before) and not tool.keyed(neither)


def test_a_wide_mode_keys_only_inside_the_band_plan(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(tool, "Station", lambda *a, **k: opened.append(a) or pytest.fail("opened"))
    keyed = ["--role", "A", "--log-dir", str(tmp_path), "--keyed", "--operator-present"]
    for refused in (["--mode", "4fsk", "--baud", "38400"],                               # 432.650: 70 cm
                    ["--mode", "gfsk", "--baud", "153600", "--freq", "1299.375e6"],     # 166 kHz > 150
                    ["--mode", "gfsk", "--baud", "76800", "--freq", "1298.5e6"],        # 20 kHz channels
                    ["--mode", "4fsk", "--baud", "76800", "--freq", "1299.700e6"]):     # over the top edge
        with pytest.raises(SystemExit, match="not keyed"):
            tool.main(keyed + refused + ["ui"])
    with pytest.raises(SystemExit, match="no 4fsk mode at 153600"):
        tool.main(keyed + ["--mode", "4fsk", "--baud", "153600", "--freq", "1299.375e6", "ui"])
    assert opened == []
    # Inside a segment the run goes ahead (to the stand-in Station); the LO
    # and image are named when they fall outside it.
    a = tool.parser().parse_args(["--role", "A", "--mode", "4fsk", "--baud", "76800", "ui"])
    m = tool.resolve_mode(a)
    assert tool.plan_check(1_299_525_000, m, 200_000) == (None, [])
    refusal, notes = tool.plan_check(1_299_225_000, m, 200_000)
    assert refusal is None and len(notes) == 1 and "image" in notes[0]
    assert tool.plan_check(2_312_000_000, tool.resolve_mode(
        tool.parser().parse_args(["--role", "A", "--mode", "gfsk", "--baud", "153600", "ui"])), 200_000)[0] is None
    # Receive-only runs anywhere; 9600 (10 kHz) is not a wide mode.
    assert tool.plan_check(432_650_000, tool.resolve_mode(
        tool.parser().parse_args(["--role", "A", "ui"])), 100_000) == (None, [])
