"""tools/build-wheels-safe.sh picks a wheel folder nobody uses: never one
that exists, never one any openhamshack-nb lock names (in a worktree or
only on a branch), and never one twice. Run for real with a fake builder
that writes small pure-Python "wheels" (check-encumbered really runs on
them), in scratch git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "tools" / "build-wheels-safe.sh"

FAKE_BUILDER = """#!/usr/bin/env python3
import os, sys, zipfile
out = sys.argv[sys.argv.index("--out") + 1]
open(os.environ["BUILDER_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("BUILDER_FAILS"):
    sys.exit(1)
for tag in ("manylinux_2_35_x86_64", "win_amd64"):
    with zipfile.ZipFile(f"{out}/shack_sdr-0.2.0-cp311-abi3-{tag}.whl", "w") as z:
        z.writestr("shack_sdr/__init__.py", f"TAG = {tag!r}\\n")
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def rig(tmp_path):
    # A copy of ohs-libs' tools in a clean repository of its own: the
    # script refuses a dirty tree, and the real checkout may be mid-change.
    libs = tmp_path / "libs"
    (libs / "tools").mkdir(parents=True)
    for f in ("build-wheels-safe.sh", "check-encumbered.py"):
        shutil.copy2(ROOT / "tools" / f, libs / "tools")
    (libs / "spec").mkdir()
    shutil.copy2(ROOT / "spec" / "encumbered.json", libs / "spec")     # check-encumbered's list
    git(libs, "init", "-q")
    git(libs, "add", "-A")
    git(libs, "commit", "-q", "-m", "libs")
    nb = tmp_path / "nb"
    (nb / "tools" / "vendor").mkdir(parents=True)
    (nb / "tools" / "vendor" / "shack-sdr.lock").write_text("# from ~/build/ohs-dist/shack-sdr-adsb1 (shack-sdr-adsb2).\n")
    git(nb, "init", "-q", "-b", "main")
    git(nb, "add", "-A")
    git(nb, "commit", "-q", "-m", "lock")
    builder = tmp_path / "builder"
    builder.write_text(FAKE_BUILDER)
    builder.chmod(0o755)
    dist = tmp_path / "dist"
    env = {**os.environ, "OHS_DIST": str(dist), "OHS_NB_REPOS": str(nb), "SHACK_SDR_BUILDER": str(builder),
           "BUILDER_LOG": str(tmp_path / "builder.log")}

    def run(*args, **more):
        return subprocess.run(["bash", str(libs / "tools" / "build-wheels-safe.sh"), *args], env={**env, **more},
                              capture_output=True, text=True, timeout=120)

    return {"libs": libs, "nb": nb, "dist": dist, "run": run, "tmp": tmp_path}


def test_a_name_a_lock_uses_is_skipped(rig):
    res = rig["run"]("adsb")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"folder: {rig['dist']}/shack-sdr-adsb3" in res.stdout      # adsb1 and adsb2 are in the lock
    out = rig["dist"] / "shack-sdr-adsb3"
    sums = (out / "SHA256SUMS").read_text()
    assert "manylinux_2_35_x86_64.whl" in sums and "win_amd64.whl" in sums
    info = (out / "BUILD-INFO").read_text()
    assert "status=built" in info
    assert f"commit={git(rig['libs'], 'rev-parse', 'HEAD').strip()}" in info
    assert "check-encumbered: clean" in res.stdout
    assert "tools/vendor-shack-sdr.sh" in res.stdout


def test_an_existing_folder_is_never_reused(rig):
    (rig["dist"] / "shack-sdr-dmr1").mkdir(parents=True)
    assert "shack-sdr-dmr2" in rig["run"]("dmr").stdout
    assert "shack-sdr-dmr3" in rig["run"]("dmr").stdout


def test_a_name_only_on_a_branch_is_skipped(rig):
    nb = rig["nb"]
    git(nb, "checkout", "-q", "-b", "feature/x")
    (nb / "tools" / "vendor" / "shack-sdr.lock").write_text("# from shack-sdr-psk1\n")
    git(nb, "commit", "-q", "-am", "psk")
    git(nb, "checkout", "-q", "main")
    assert "shack-sdr-psk2" in rig["run"]("psk").stdout


def test_a_name_in_another_worktree_is_skipped(rig):
    nb = rig["nb"]
    wt = rig["tmp"] / "wt"
    git(nb, "worktree", "add", "-q", "-b", "feature/y", str(wt))
    (wt / "tools" / "vendor" / "shack-sdr.lock").write_text("# uncommitted: shack-sdr-fm1\n")
    assert "shack-sdr-fm2" in rig["run"]("fm").stdout


def test_a_failed_build_keeps_its_name(rig):
    res = rig["run"]("ais", BUILDER_FAILS="1")
    assert res.returncode == 1
    assert "status=failed" in (rig["dist"] / "shack-sdr-ais1" / "BUILD-INFO").read_text()
    assert "shack-sdr-ais2" in rig["run"]("ais").stdout


def test_targets_reach_the_builder(rig):
    rig["run"]("dab", "linux-amd64", "windows-amd64")
    log = (rig["tmp"] / "builder.log").read_text()
    assert f"--out {rig['dist']}/shack-sdr-dab1 linux-amd64 windows-amd64" in log


def test_a_dirty_tree_is_refused(rig):
    (rig["libs"] / "new.txt").write_text("x")
    res = rig["run"]("adsb")
    assert res.returncode == 1
    assert "uncommitted changes" in res.stderr
    assert not rig["dist"].exists()


@pytest.mark.parametrize("args", [[], ["Adsb"], ["m2"], ["adsb", "linux-386"], ["../x"]])
def test_bad_arguments_are_refused(rig, args):
    res = rig["run"](*args)
    assert res.returncode == 2, args
    assert not rig["dist"].exists()
