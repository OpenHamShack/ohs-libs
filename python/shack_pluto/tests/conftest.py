"""A stand-in for the Pluto's sysfs: the three files the guard reads and
writes, under a root the guard script is told about."""

from __future__ import annotations

from pathlib import Path

import pytest


class Sysfs:
    def __init__(self, root: Path, powerdown="0", gain="-10.000000 dB", name="ad9361-phy"):
        self.root = root
        self.dev = root / "sys/bus/iio/devices/iio:device0"
        self.dev.mkdir(parents=True)
        (self.dev / "name").write_text(name + "\n")
        self.pd = self.dev / "out_altvoltage1_TX_LO_powerdown"
        self.gain = self.dev / "out_voltage0_hardwaregain"
        self.pd.write_text(powerdown + "\n")
        self.gain.write_text(gain + "\n")

    def key(self) -> None:
        """What a link keying the radio leaves behind."""
        self.pd.write_text("0\n")
        self.gain.write_text("-10.000000 dB\n")

    def quiet(self) -> bool:
        return self.pd.read_text().strip() == "1" and self.gain.read_text().startswith("-89.75")

    def log(self) -> str:
        p = self.root / "guard.log"
        return p.read_text() if p.exists() else ""


@pytest.fixture
def sysfs(tmp_path):
    return Sysfs(tmp_path)
