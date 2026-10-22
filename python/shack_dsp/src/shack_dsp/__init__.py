"""shack_dsp: signal processing every OpenHamShack app with receive audio
shares (GUIDE: shared components, one of each).

Today one thing, NR2, the spectral noise reduction the rig apps run on the
audio people hear (`shack_dsp.nr2`):

    nr = shack_dsp.Nr2Stream(rate=12_000, channels=1, level=50, on=True)
    out = nr.feed(pcm)         # s16le in, s16le out, any packet size
    nr.on = False              # fades out: no click, no lost sample

`shack_dsp.Nr2` is the float core. `python3 -m shack_dsp.bench` measures
the SNR it gains on speech and CW, its block edges and its CPU cost.
"""

from __future__ import annotations

from .nr2 import Nr2, Nr2Stream, frame_for

__version__ = "0.2.0"

__all__ = ["Nr2", "Nr2Stream", "frame_for"]
