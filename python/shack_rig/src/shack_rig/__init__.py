"""shack_rig: the OpenHamShack rig-control library (PLATFORM-PLAN §1.4).

One library for the per-maker rig apps: transports (serial, Icom LAN/UDP,
USB discovery and USB audio), the CI-V codec, a maker-neutral rig surface
(`Rig`, `RigModel`, the state keys) with an Icom backend, and a hamlib NET
rigctld server an app may run for third-party software.

Transmit is lease-only: `rig.set_ptt(True, lease)` with the app's TxLease is
the one way to key, and it raises PttRefused without one (lease.py).
docs/RIG.md is the adoption guide.
"""

from __future__ import annotations

from .lease import Lease, PttRefused, TxLease
from .model import DATA_CAPABLE, MODES, RigModel, band_buttons, mode_label, split_mode_label
from .rig import STATE_KEYS, Rig
from .rigctl import RigctlServer

__version__ = "0.1.0"

__all__ = ["DATA_CAPABLE", "Lease", "MODES", "PttRefused", "Rig", "RigModel", "RigctlServer",
           "STATE_KEYS", "TxLease", "band_buttons", "mode_label", "split_mode_label"]
