"""Receive-only IQ sources (RADIO-SDR-PLAN §6): an RTL-SDR through
librtlsdr, and an SDRplay RSP through SDRplay's API the user installed
(never bundled), each over ctypes, so every desktop OS gets both radios
without SoapySDR and nothing is compiled per Python version.

Moved from shack_packet.sdr (Packet Radio), whose own copies stay until
Packet switches to these (shack_sdr README, "Packet's switch-over").

    from shack_sdr.sources import RtlSdrSource, SdrplaySource, SdrError
    src = RtlSdrSource(centre, 2_400_000)        # RtlSdrSource.rates() -> [1.2e6, 2.4e6]
    with src:
        block = src.read()                       # IqBlock: complex64 at full scale 1.0, index, time, dropped
    sdrplay.availability()                       # {"available": False, "why": "sdrplay_api_missing", ...}

Receive only by construction: nothing here has a transmit method.
"""

from __future__ import annotations

from . import rtlsdr, sdrplay
from .rtlsdr import RtlSdrSource
from .sdrplay import SdrplaySource
from .source import REASONS, IqBlock, IqSource, SdrError, complex_to_cu8, cu8_to_complex

__all__ = ["REASONS", "IqBlock", "IqSource", "RtlSdrSource", "SdrError", "SdrplaySource", "complex_to_cu8",
           "cu8_to_complex", "rtlsdr", "sdrplay"]
