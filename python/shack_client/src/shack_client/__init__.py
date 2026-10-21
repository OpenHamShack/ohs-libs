"""shack_client: every OpenHamShack app's local logbook and station profile,
offline first, and the sync with a controller when paired (SYNC.md).

    from shack_client import Station, discover
    station = Station.open(app="shack-qo100nb", app_version="1.0.0")

Stdlib only in the core; discovery's browsing needs the optional
python-zeroconf (`shack-client[discovery]`).
"""
from __future__ import annotations

from .discovery import Controller, DiscoveryUnavailable, discover, well_known
from .pairing import PairingError
from .station import Logbook, NeedsDecision, Pairing, Profile, Qso, Station
from .transport import Offline

__all__ = ["Controller", "DiscoveryUnavailable", "Logbook", "NeedsDecision", "Offline", "Pairing", "PairingError",
           "Profile", "Qso", "Station", "discover", "well_known"]
__version__ = "0.1.0"
