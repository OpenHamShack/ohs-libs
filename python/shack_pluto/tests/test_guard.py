"""The on-device guard (guard.py), run for real: GUARD_SCRIPT under the
host's busybox sh (the Pluto's own shell) against a stand-in sysfs, driven
by the Guard client through LocalRunner. Each way the app can stop - a
crash (its pipe closes), a freeze (no heartbeat), a clean close - and a key
held past its limit, ends with the transmitter quiet."""

from __future__ import annotations

import os
import shlex
import time

import pytest

from shack_pluto.guard import GUARD_SCRIPT, REASONS, Guard, GuardError, LocalRunner, SshRunner, guard_command


def until(cond, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def started(sysfs, max_key_s=10, **kw):
    runner = LocalRunner()
    g = Guard(runner, max_key_s, root=str(sysfs.root), **kw).start(timeout=5)
    return g, runner


def test_ready_comes_only_after_the_transmitter_reads_back_quiet(sysfs):
    assert not sysfs.quiet()                      # as a rebooted Pluto: TX LO up, -10 dB
    g, _ = started(sysfs)
    try:
        assert g.confirmed and sysfs.quiet()
        assert g.lines[0].startswith("ready")
    finally:
        g.stop()


def test_the_app_dying_quiets_the_transmitter_at_once(sysfs):
    g, runner = started(sysfs)
    sysfs.key()
    t0 = time.monotonic()
    runner.sessions[0].kill()                     # the pipe closes with no goodbye: kill -9
    assert until(sysfs.quiet, 2)
    assert time.monotonic() - t0 < 1.0
    assert until(lambda: not g.confirmed, 2)
    assert "quiet eof" in g.lines and g.why_lost == "quiet eof"
    assert "quiet eof" in sysfs.log()


def test_a_frozen_app_is_noticed_by_its_missing_heartbeat(sysfs):
    # heartbeat_s far longer than the timeout: the app is alive (its pipe
    # open) but nothing comes, as under SIGSTOP or a hung interpreter.
    g, _ = started(sysfs, heartbeat_s=60, heartbeat_timeout_s=1)
    sysfs.key()
    t0 = time.monotonic()
    assert until(lambda: "quiet heartbeat" in g.lines, 3)
    assert 0.9 <= time.monotonic() - t0 <= 2.0
    assert sysfs.quiet() and not g.confirmed
    g.stop()


def test_a_beating_heart_keeps_the_guard_and_the_key_alone(sysfs):
    g, _ = started(sysfs, heartbeat_s=0.3, heartbeat_timeout_s=1)
    try:
        sysfs.key()
        time.sleep(2.0)                           # past the heartbeat timeout twice over
        assert g.confirmed and not sysfs.quiet()  # keyed is the link's business, within its limit
    finally:
        g.stop()
    assert sysfs.quiet() and "quiet closed" in g.lines


def test_a_key_held_past_its_limit_is_quieted_and_the_guard_carries_on(sysfs):
    g, _ = started(sysfs, max_key_s=1, heartbeat_s=0.3)
    try:
        sysfs.key()
        t0 = time.monotonic()
        assert until(sysfs.quiet, 5)
        took = time.monotonic() - t0
        assert 3.0 <= took <= 4.0                 # max_key_s + 2 s, in half-second looks
        assert until(lambda: "quiet overlong" in g.lines, 1)
        assert g.confirmed                        # still watching: the app is alive
    finally:
        g.stop()


def test_a_hangup_quiets_the_transmitter(sysfs):
    g, runner = started(sysfs)
    sysfs.key()
    runner.sessions[0].proc.send_signal(1)        # SIGHUP, as a session closing may send
    assert until(sysfs.quiet, 2)
    assert until(lambda: not g.confirmed, 2)


def test_a_pluto_that_will_not_read_quiet_gives_no_guard(sysfs):
    sysfs.pd.write_text("0\n")
    os.chmod(sysfs.pd, 0o444)                     # the write "fails": it never reads back 1
    if os.access(sysfs.pd, os.W_OK):
        pytest.skip("running as root: a read-only file is still writable")
    with pytest.raises(GuardError) as e:
        Guard(LocalRunner(), 10, root=str(sysfs.root)).start(timeout=5)
    assert e.value.reason == "guard-not-quiet"


def test_a_shell_without_read_timeouts_gives_no_guard_and_leaves_it_quiet(sysfs):
    # dash, Debian's /bin/sh: its `read` has no -t, so every look would
    # fail at once and read as EOF - a guard that says `ready` and then
    # quits blaming a live app. It must refuse before `ready` instead.
    import shutil
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("no dash on this host")
    with pytest.raises(GuardError) as e:
        Guard(LocalRunner(shell=dash), 10, root=str(sysfs.root)).start(timeout=5)
    assert e.value.reason == "guard-failed" and "no-read-timeout" in e.value.detail
    assert sysfs.quiet()


def test_no_ad936x_gives_no_guard(tmp_path):
    from conftest import Sysfs
    Sysfs(tmp_path, name="xadc")
    with pytest.raises(GuardError) as e:
        Guard(LocalRunner(), 10, root=str(tmp_path)).start(timeout=5)
    assert e.value.reason == "guard-failed" and "no-ad9361-phy" in e.value.detail


def test_a_runner_that_cannot_connect_is_no_ssh():
    class Refused:
        def start(self, command):
            raise OSError("connection refused")
    with pytest.raises(GuardError) as e:
        Guard(Refused(), 10).start()
    assert e.value.reason == "guard-no-ssh"


def test_the_ssh_runner_needs_a_network_address():
    assert SshRunner.for_uri("ip:192.168.2.1").host == "192.168.2.1"
    assert SshRunner.for_uri("ip:pluto.local:30431").host == "pluto.local"
    assert SshRunner.for_uri("192.168.3.1").host == "192.168.3.1"
    for uri in ("usb:1.5.5", "local:", "ip:"):
        with pytest.raises(GuardError) as e:
            SshRunner.for_uri(uri)
        assert e.value.reason == "guard-no-network"


def test_the_command_carries_the_script_whole_and_whole_seconds():
    words = shlex.split(guard_command(8.4))
    assert words == ["sh", "-c", GUARD_SCRIPT, "shack-pluto-guard", "8", "3", ""]


def test_every_reason_has_words():
    assert all(REASONS.values()) and all(k.startswith("guard-") for k in REASONS)


def test_the_guard_waits_long_enough_for_plutodvb2s_slow_banner():
    from shack_pluto.guard import READY_TIMEOUT_S, SSH_TIMEOUT_S
    # 5.9 s to `ready` on PlutoDVB2 0.5.16.7 through the controller box.
    assert READY_TIMEOUT_S >= 20 and SSH_TIMEOUT_S >= 20 and READY_TIMEOUT_S > SSH_TIMEOUT_S


def _debugfs(sysfs, value="0x10"):
    reg = sysfs.root / "sys/kernel/debug/iio/iio:device0/direct_reg_access"
    reg.parent.mkdir(parents=True)
    reg.write_text(value + "\n")
    return reg


def test_with_a_gpo_ptt_every_quiet_drops_the_gpo_too(sysfs):
    reg = _debugfs(sysfs)                                 # GPO0 high, as a key leaves it
    g, runner = started(sysfs, gpo=True)
    assert sysfs.quiet() and reg.read_text().strip() == "0x27 0x0"
    reg.write_text("0x27 0x10\n")
    sysfs.key()
    runner.sessions[0].kill()
    assert until(sysfs.quiet) and until(lambda: reg.read_text().strip() == "0x27 0x0")
    assert "quiet eof" in sysfs.log()


def test_a_gpo_ptt_without_debugfs_gives_no_guard(sysfs):
    with pytest.raises(GuardError) as e:
        started(sysfs, gpo=True)
    assert e.value.reason == "guard-no-gpo" and e.value.reason in REASONS


def test_an_amplifiers_drive_cap_is_kept_by_the_guard(sysfs):
    g, _ = started(sysfs, max_gain_db=-20.0)
    sysfs.pd.write_text("0\n")
    sysfs.gain.write_text("-25.000000 dB\n")               # under the cap: left alone
    time.sleep(1.2)
    assert sysfs.pd.read_text().strip() == "0"
    sysfs.gain.write_text("-10.000000 dB\n")               # over it: quieted at once, guard carries on
    assert until(sysfs.quiet, 3)
    assert until(lambda: "quiet overdrive" in sysfs.log()) and g.confirmed
    g.stop()


def test_the_command_carries_the_cap_and_the_gpo_flag():
    words = shlex.split(guard_command(8, max_gain_db=-20.5))
    assert words[-2:] == ["", "-21"]
    assert shlex.split(guard_command(8, gpo=True))[-1] == "gpo"
