"""The two-station rig's tools, against fakes: pluto_guard.py (the Pluto
quieted when the process using it dies, or when its TX has been up too
long) and twostation.py's airtime caps and carrier accounting."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from sdrhelp import FakePluto

TOOLS = Path(__file__).parent.parent / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pluto_guard = _load("pluto_guard")


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_the_guard_quiets_a_pluto_whose_process_has_died():
    radio = FakePluto()                 # as a killed process leaves it: LO up, -10 dB
    assert not radio.tx_quiet()
    assert pluto_guard.run("ip:192.168.2.1", _dead_pid(), 8, iio_module=radio.module(),
                           sleep=lambda s: None) == "gone-quiet"
    assert radio.tx_quiet()


def test_the_guard_quiets_a_transmitter_up_past_its_limit_and_leaves_a_short_over_alone():
    radio = FakePluto()
    now = [0.0]
    seen = []

    def sleep(s):
        now[0] += s
        seen.append((now[0], radio.tx_quiet()))

    # Our own pid: alive throughout. TX up from the start; the limit is
    # max_key_s + 2 = 3 s, so it must still be up at 2.9 s and quiet by 3.3 s.
    pluto_guard.run("ip:192.168.2.1", __import__("os").getpid(), 1.0, iio_module=radio.module(),
                    clock=lambda: now[0], sleep=sleep, stop=lambda: now[0] > 4)
    assert all(not q for t, q in seen if t < 2.9)
    assert any(q for t, q in seen if t < 3.3)


def test_the_guard_writes_nothing_but_the_two_quiet_values():
    radio = FakePluto()
    pluto_guard.run("ip:192.168.2.1", _dead_pid(), 8, iio_module=radio.module(), sleep=lambda s: None)
    assert sorted(set(radio.tx_writes())) == sorted({("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", "-89.75")})


twostation = _load("twostation")


def _feed(watch, samples):
    """samples: (seconds, level_db, b_keyed), fed every 50 ms."""
    t = 0.0
    for secs, level, b_keyed in samples:
        for _ in range(int(round(secs / 0.05))):
            watch.feed(level, b_keyed, t)
            t += 0.05
    return t


def test_a_carrier_is_counted_as_a_and_bs_own_over_is_not():
    air = twostation.Airtime(total_s=100, max_key_a_s=17)
    w = twostation.CarrierWatch(air, threshold_db=12)
    _feed(w, [(1, 2, False), (3, 30, False), (1, 2, False), (4, 30, True), (1, 2, False)])
    assert air.a_overs == [3.0] and air.b_s == 0
    assert not w.on


def test_an_overlong_carrier_from_a_is_reported_once():
    air = twostation.Airtime(total_s=100, max_key_a_s=2)
    said = []
    w = twostation.CarrierWatch(air, threshold_db=12, on_overlong=said.append)
    _feed(w, [(5, 30, False)])
    assert len(said) == 1 and 2.0 < said[0] < 2.2
    assert w.on


def test_the_airtime_cap_counts_both_stations():
    air = twostation.Airtime(total_s=10, max_key_a_s=17)
    air.add("a", 6)
    assert not air.spent()
    air.add("b", 4)
    assert air.spent()


def test_nothing_transmits_without_both_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))      # the run log goes under ~/build
    for argv in (["run"], ["run", "--keyed"], ["run", "--operator-present"], ["stuck", "--keyed"],
                 ["_b_long_over", "--operator-present"]):
        with pytest.raises(SystemExit, match="--keyed --operator-present"):
            twostation.main(argv + ["--radio-json", "/nonexistent"])


def test_the_capped_link_refuses_to_key_once_the_airtime_is_spent():
    from shack_packet import Station
    from shack_packet.radios import LinkRefused
    air = twostation.Airtime(total_s=1, max_key_a_s=17)
    air.add("a", 1)
    link = twostation.capped_link_class()("ip:192.168.2.1", 432_650_000, iio_module=FakePluto().module(),
                                          station=Station.of("GM4BRI", tx_enabled=True))
    link.airtime = air
    with link:
        with pytest.raises(LinkRefused):
            link.key()
        assert link.tx_quiet
