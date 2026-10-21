"""The Kenwood backend: the TM-V71's PC port commands, its model row,
KenwoodRig, its data set-up, and a fake TM-V71.

Packet on a TM-V71 is two cables: the PC port for control (`cat`,
`rig.KenwoodRig`) and the DATA jack through a sound-card interface for
audio and PTT. `cat` is the codec, `models` the table, `datasetup` what a
data station sets and puts back, `fake.FakeTmV71` the radio the tests
talk to.
"""

from __future__ import annotations

from . import cat, models
from .models import KenwoodModel
from .rig import KenwoodRig

__all__ = ["KenwoodModel", "KenwoodRig", "cat", "models"]
