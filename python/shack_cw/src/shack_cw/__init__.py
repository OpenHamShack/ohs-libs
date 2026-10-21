"""shack_cw: the OpenHamShack CW (Morse) decoder and Morse tables.

One decoder for every app with a receiver (GUIDE: shared components, one of
each): QO-100 NB and Radio, Icom, and next Flex. Pure numpy, at whatever
sample rate the app's audio comes:

    dec = shack_cw.Decoder(freq=600.0)          # the radio's CW pitch
    for ev in dec.feed(samples, 12_000):        # any block length, any rate
        ...  # {"mode": "cw", "text": "K", "freq": 600.0, "wpm": 18.2}
    dec.wpm, dec.pitch, dec.locked, dec.hearing

Events are one per character, in the shape QO-100 NB's `events` feed has
always carried (openhamshack-nb docs/SERVER-API.md), so clients share one CW
strip. `shack_cw.bench` measures the decoder (`python3 -m shack_cw.bench`).
"""

from __future__ import annotations

from .decoder import REF_OFFSETS, Decoder, amplitudes, window_frames
from .morse import CODE_TO_CHAR, CODE_TO_PROSIGN, MORSE, PROSIGNS, envelope, envelope_of, runs, text_to_codes

__version__ = "0.1.0"

__all__ = ["CODE_TO_CHAR", "CODE_TO_PROSIGN", "Decoder", "MORSE", "PROSIGNS", "REF_OFFSETS", "amplitudes", "envelope",
           "envelope_of", "runs", "text_to_codes", "window_frames"]
