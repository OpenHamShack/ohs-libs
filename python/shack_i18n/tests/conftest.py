"""Shared helpers: build a throwaway repo from fixture files and run the check.

Every test runs the real check over real files and looks at what it
prints; none of them reads the library's own source.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from shack_i18n import check

FIXTURES = Path(__file__).parent / "fixtures"
# A fixture line that must produce a finding says so in a comment in its own
# language's syntax. The marker lives in a comment so the scanners must
# skip it like any other comment.
EXPECT = re.compile(r"expect-finding")


class Repo:
    def __init__(self, root: Path):
        self.root = root

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def copy(self, fixture: str, rel: str | None = None) -> Path:
        src = FIXTURES / fixture
        dst = self.root / (rel or fixture)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return dst

    def catalogs(self, system: str) -> None:
        """Copy the good fixture catalogs for one mechanism to their places."""
        places = {
            "qt": [("catalogs/qt/app_en.ts", "app/i18n/app_en.ts"), ("catalogs/qt/app_de.ts", "app/i18n/app_de.ts")],
            "gettext": [
                ("catalogs/gettext/messages.pot", "srv/locale/shack-srv.pot"),
                ("catalogs/gettext/de.po", "srv/locale/de/LC_MESSAGES/shack-srv.po"),
            ],
            "web": [("catalogs/web/en.json", "web/static/i18n/en.json"), ("catalogs/web/de.json", "web/static/i18n/de.json")],
            "android": [
                ("catalogs/android/values/strings.xml", "android/app/src/main/res/values/strings.xml"),
                ("catalogs/android/values-de/strings.xml", "android/app/src/main/res/values-de/strings.xml"),
            ],
            "apple": [("catalogs/apple/Localizable.xcstrings", "apple/Resources/Localizable.xcstrings")],
        }
        for src, dst in places[system]:
            self.copy(src, dst)

    def run(self, *paths: str, config: str | None = None) -> check.Report:
        return check.run(list(paths), config, cwd=self.root)

    def expected_lines(self, rel: str) -> set[int]:
        text = (self.root / rel).read_text(encoding="utf-8")
        return {i for i, line in enumerate(text.splitlines(), 1) if EXPECT.search(line)}


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    # The config file marks the root, as it would in a real repo, so the
    # check never wanders up into whatever repository holds the tmp dir.
    (tmp_path / ".i18n-check.toml").write_text("", encoding="utf-8")
    return Repo(tmp_path)


def lines_of(report: check.Report, rel: str) -> set[int]:
    return {f.line for f in report.findings if f.path == rel}
