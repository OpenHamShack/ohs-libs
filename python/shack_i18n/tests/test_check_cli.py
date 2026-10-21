"""The command line: exit codes, output, configuration, file selection."""

import subprocess
import sys
from pathlib import Path

from shack_i18n import check

BAD = 'class W:\n    def f(self):\n        self.l.setText("Transmit blocked")\n'
GOOD = 'class W:\n    def f(self):\n        self.l.setText(self.tr("Transmit blocked"))\n'


def _cli(repo, *args):
    # The installed console script, as CI will call it.
    exe = Path(sys.executable).with_name("shack-i18n-check")
    return subprocess.run([str(exe), *args], cwd=repo.root, capture_output=True, text=True)


def test_console_script_exits_1_with_one_line_per_finding(repo):
    repo.write("app/w.py", BAD)
    proc = _cli(repo)
    assert proc.returncode == 1
    assert proc.stdout.splitlines() == [
        'app/w.py:3:24: user-visible literal "Transmit blocked" passed to setText; use self.tr()'
    ]
    assert proc.stderr.strip() == "Findings: 1. Warnings: 0."


def test_console_script_exits_0_and_is_silent_when_clean(repo):
    repo.write("app/w.py", GOOD)
    repo.write(
        "app/i18n/w_en.ts",
        '<TS language="en"><context><name>W</name><message><source>Transmit blocked</source>'
        "<translation/></message></context></TS>",
    )
    proc = _cli(repo)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_python_dash_m_works_too(repo):
    repo.write("app/w.py", BAD)
    proc = subprocess.run([sys.executable, "-m", "shack_i18n.check"], cwd=repo.root, capture_output=True, text=True)
    assert proc.returncode == 1 and "Transmit blocked" in proc.stdout


def test_paths_limit_the_run(repo):
    repo.write("app/w.py", BAD)
    repo.write("other/w.py", BAD)
    assert {f.path for f in repo.run("other").findings} == {"other/w.py"}
    assert {f.path for f in repo.run("app/w.py").findings} == {"app/w.py"}


def test_paths_are_reported_relative_to_the_root_from_a_subdirectory(repo):
    repo.write("app/ui/w.py", BAD)
    report = check.run(["."], None, cwd=repo.root / "app")
    assert [f.path for f in report.findings] == ["app/ui/w.py"]


def test_include_and_exclude_globs(repo):
    repo.write(".i18n-check.toml", 'include = ["app/**"]\nexclude = ["**/tests/**"]\n')
    repo.write("app/w.py", BAD)
    repo.write("app/tests/test_w.py", BAD)
    repo.write("tools/w.py", BAD)
    assert {f.path for f in repo.run().findings} == {"app/w.py"}


def test_vendor_and_build_directories_are_skipped(repo):
    for d in ("node_modules", ".venv", "build", "__pycache__"):
        repo.write(f"{d}/w.py", BAD)
    assert repo.run().findings == []


def test_explicit_config_file_sets_the_root(repo):
    repo.write("conf/ci.toml", '[python]\nsinks = ["setBanner"]\n')
    repo.write("conf/app/w.py", 'def f(w):\n    w.setBanner("Hello there")\n')
    proc = _cli(repo, "--config", "conf/ci.toml")
    assert proc.stdout.startswith("app/w.py:2:17: ")


def test_unknown_setting_stops_the_check_with_exit_2(repo):
    repo.write(".i18n-check.toml", '[python]\nuser_facing_exception = ["ValueError"]\n')
    proc = _cli(repo)
    assert proc.returncode == 2
    assert 'has no setting "user_facing_exception"' in proc.stderr
    assert proc.stderr.rstrip().endswith(".")


def test_missing_path_is_exit_2(repo):
    proc = _cli(repo, "nowhere")
    assert proc.returncode == 2 and "does not exist" in proc.stderr
