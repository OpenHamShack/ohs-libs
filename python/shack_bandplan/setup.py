"""Copy the repository's data/bandplan into the wheel at build time.

REPO-LAYOUT.md §2: "data copied in at build from data/". The JSON lives
once, at the repository root, where the other languages' readers and the
validator find it; a second copy committed under the package would drift.
Build wheels from this directory with `python -m build --wheel` (not via
an sdist, which would be cut off from ../../data).
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "bandplan"


class BuildWithData(build_py):
    def run(self):
        super().run()
        if not (DATA / "manifest.json").is_file():
            raise SystemExit(f"{DATA} is missing: build shack-bandplan from an ohs-libs checkout")
        dest = Path(self.build_lib) / "shack_bandplan" / "data" / "bandplan"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(DATA, dest)


setup(cmdclass={"build_py": BuildWithData})
