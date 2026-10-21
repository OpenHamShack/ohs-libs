"""Standalone desktop hand-over (focus.py, CONTROLLER-PAD.md §6.2 and §9
case 6): two apps share one pad through a lock file, never a socket."""

import os
import subprocess
import sys
import textwrap
import time

import pytest
from helpers import make_app

from shack_pad import InputHost
from shack_pad.drivers import AKP03E
from shack_pad.fake import FakeHid, akp03e_report
from shack_pad.focus import DesktopPad, PadFocus


class Desk:
    """One desktop app: its own host, app and (fake) pad."""

    def __init__(self, name, decl, layout, store):
        self.app = make_app(name, decl, layout, name=name.upper())
        self.host = InputHost([self.app], host="desk")
        self.fakes = []
        self.pad = DesktopPad(self.host, app=f"shack-{name}", store_dir=store, open_device=self.open, token=name)

    def open(self):
        fake = FakeHid()
        self.fakes.append(fake)
        return AKP03E(fake)

    def serving(self):
        return self.pad.serving() and not self.fakes[-1].closed


def wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture
def desks(nb, tmp_path):
    a, b = Desk("radio", *nb, tmp_path), Desk("qo100nb", *nb, tmp_path)
    yield a, b
    a.pad.close()
    b.pad.close()


def test_focus_moves_the_pad_within_a_second(desks):
    a, b = desks
    a.pad.start()
    b.pad.start()
    a.pad.focus_gained()
    assert a.pad.focus.owns and wait(a.serving)
    moved = time.monotonic()
    b.pad.focus_gained()
    assert wait(lambda: b.pad.focus.owns and b.serving())
    assert time.monotonic() - moved < 1.0
    assert not a.pad.focus.owns and a.fakes[-1].closed
    # A let go cleanly: its keys were blanked on the way out.
    assert any(w[6:9] == b"CLE" for w in a.fakes[-1].writes[5:])


def test_losing_focus_alone_keeps_the_pad(desks):
    a, _ = desks
    a.pad.focus_gained()
    for _ in range(3):
        a.pad.poll()
    assert a.pad.focus.owns and a.serving()


def test_the_owner_on_air_keeps_the_pad_until_unkeyed(desks):
    a, b = desks
    a.pad.focus_gained()
    assert wait(a.serving)
    a.fakes[-1].push(akp03e_report(0x01, 1))          # ptt held on A
    assert wait(lambda: a.app.tx.on_air)
    b.pad.focus_gained()
    a.pad.poll()
    b.pad.poll()
    assert a.pad.focus.owns and not b.pad.focus.owns and a.app.tx.on_air
    # Every key blank but the one that stops it, which says who is on air.
    faces = a.host.faces("ajazz-akp03e")
    assert (faces["K1"].l1, faces["K1"].l2, faces["K1"].tone) == ("ON AIR", "RADIO", "onair")
    assert {faces[k].tone for k in ("K2", "K3", "K4", "K5", "K6")} == {"blank"}
    a.fakes[-1].push(akp03e_report(0x02, 1))          # other keys do nothing meanwhile
    a.fakes[-1].push(akp03e_report(0x01, 0))          # release: unkeyed
    assert wait(lambda: not a.app.tx.on_air)
    assert a.app.performed == []
    a.pad.poll()
    b.pad.poll()
    assert b.pad.focus.owns and wait(b.serving) and a.fakes[-1].closed


def test_a_later_focus_supersedes_an_earlier_waiting_one(desks, nb, tmp_path):
    a, b = desks
    c = Desk("atv", *nb, tmp_path)
    a.pad.focus_gained()
    b.pad.focus.want()
    b.pad.wanted = True
    c.pad.focus_gained()                # C's want replaces B's
    a.pad.poll()
    b.pad.poll()
    c.pad.poll()
    assert c.pad.focus.owns and not b.pad.focus.owns and not b.pad.wanted
    c.pad.close()


def test_a_killed_owners_lock_is_freed_by_the_os(tmp_path):
    src = os.path.dirname(os.path.dirname(os.path.abspath(sys.modules["shack_pad"].__file__)))
    child = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {src!r})
        from shack_pad.focus import PadFocus
        f = PadFocus("ajazz-akp03e", "shack-radio", {str(tmp_path)!r})
        assert f.try_acquire()
        print("owned", flush=True)
        time.sleep(60)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "owned"
        mine = PadFocus("ajazz-akp03e", "shack-qo100nb", tmp_path, token="me")
        assert not mine.try_acquire()
        child.kill()
        child.wait(5)
        assert wait(mine.try_acquire)
        mine.release()
    finally:
        if child.poll() is None:
            child.kill()


def test_a_want_from_a_dead_process_is_ignored(tmp_path):
    owner = PadFocus("ajazz-akp03e", "shack-radio", tmp_path, token="owner")
    assert owner.try_acquire()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (tmp_path / "pad").mkdir(exist_ok=True)
    owner.want_path.write_text(f'{{"pid": {dead.pid}, "token": "x", "app": "shack-atv", "at": 0}}')
    assert owner.wanted_by_other() is None
    owner.release()


def test_a_pad_that_keeps_failing_is_not_woken_four_times_a_second(nb, tmp_path):
    """Each open wakes the pad: every key cleared, the brightness to
    nothing and back. A pad whose reads fail as soon as it is served is
    opened again once per OPEN_RETRY, not on every 250 ms look."""
    from shack_pad import focus

    class Failing(FakeHid):
        def read(self, timeout):
            raise OSError(5, "read error")

    desk = Desk("qo100nb", *nb, tmp_path)

    def open_failing():
        desk.fakes.append(Failing())
        return AKP03E(desk.fakes[-1])
    desk.pad.open_device = open_failing
    desk.pad.focus_gained()
    end = time.monotonic() + 1.8
    while time.monotonic() < end:
        desk.pad.poll()
        time.sleep(focus.POLL_SECONDS)
    desk.pad.close()
    assert 1 <= len(desk.fakes) <= 2
