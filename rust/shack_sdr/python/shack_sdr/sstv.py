"""What an app needs around the `sstv` decoder (RADIO-SDR-PLAN §8.5 M2):
its modes, where its IQ is centred, the codes its feed uses, and the test
transmitter and score the tests and a desk check use.

    from shack_sdr import Host, sstv
    host = Host(files_dir=pictures)                 # finished pictures land there
    sid = host.start("sstv", {}, "audio", 12_000)  # or "iq", centred on sstv.IQ_CENTRE_HZ
    host.feed(sid, audio, t0, index)
    host.poll()          # image start / lines / done, and meta (below)
    host.file(name)      # the PNG, for api/decoders/files?id=<name>

The decoder takes audio at any rate from 6 kHz (48 kHz is decimated to
12, never resampled) or IQ from 4 kHz centred on the 1900 Hz leader tone:
for an SSB channel, the session's frequency is the dial plus 1900 Hz.

A picture arrives as `image` results: "start" (`width`, `height`,
`format` "rgb"), "lines" (`from`, `rgb`: rows of 3 bytes a pixel, top
down, sent as they are read and again where the picture is straightened
when it ends) and "done" (`file`, the name `Host.file` serves, and `path`
when the host has a files folder). Its `meta` (also `status()["meta"]`)
says, while receiving (every sixteen lines) and at the end:

    {"state": "receiving" | "done" | "lost", "mode": "martin1", "vis": 44 | None,
     "partial": False, "width": 320, "height": 256, "rows": 256, "started": <UTC>,
     "slantPpm": -0.3, "snrDb": 21.4, "offsetHz": 12.5, "syncs": 255, "missed": 1,
     "corrected": True, "file": "ds_3f2a_sstv_20261023-101500Z_martin1.png" | None}

`partial` is a picture started from its line syncs with no header (joined
part way: its first row is wherever it was joined, and it is as tall as
what arrived); `lost` one that ended before its last line (its syncs
stopped, or a new header came); `slantPpm` the sender's sound card, parts
per million fast; `corrected` whether the final straightening moved any
row. A header for a mode not decoded is reported alone,
`{"state": "unsupported", "vis": 55, "mode": "wraase_sc2_180"}`, or
`{"state": "bad_vis", ...}` when its parity failed.

Codes (the app words them; these are suggested sentences):

| Code | Suggested sentence |
|---|---|
| `receiving` | Receiving a picture. |
| `done` | Picture received. |
| `lost` | The picture stopped before its end. |
| `unsupported` | A picture in a mode Radio cannot show yet. |
| `bad_vis` | A picture's header was too weak to read. |
| mode ids | `martin1` Martin M1, `martin2` Martin M2, `scottie1` Scottie S1, `scottie2` Scottie S2, `scottiedx` Scottie DX, `robot36` Robot 36, `robot72` Robot 72, `pd50`-`pd290` PD50-PD290 |
"""

from __future__ import annotations

import numpy as np

from . import _native

#: Where IQ for the decoder is centred, Hz above the SSB dial.
IQ_CENTRE_HZ = 1900.0


def modes() -> list[dict]:
    """[{"id", "vis", "width", "height", "lineMs", "seconds"}] for every
    mode decoded, in the order offered."""
    return _native.sstv_modes()


def testsig(mode: str, image: np.ndarray | None = None, *, rate: float = 12_000.0, snr_db: float | None = None,
            offset_hz: float = 0.0, clock_ppm: float = 0.0, fading: str | None = None, lead_s: float = 0.5,
            tail_s: float = 1.0, cut: float = 0.0, seed: int = 1, vis: bool = True,
            iq: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """A picture sent in `mode` through a channel, for tests and a desk
    check: (samples, the picture sent as height x width x 3 uint8).
    `image` defaults to the test card; samples are float32 audio, or
    complex64 IQ centred on IQ_CENTRE_HZ. `fading` is None, "good",
    "moderate" or "poor"; `cut` the share of the transmission lost before
    the receiver tuned in; SNR is in 2.5 kHz."""
    m = next(x for x in modes() if x["id"] == mode)
    raw = None if image is None else np.ascontiguousarray(image, dtype=np.uint8).tobytes()
    d = _native.testsig_sstv(mode, raw, rate=rate, snr_db=snr_db, offset_hz=offset_hz, clock_ppm=clock_ppm,
                             fading=fading, lead_s=lead_s, tail_s=tail_s, cut=cut, seed=seed, vis=vis, iq=iq)
    x = np.frombuffer(d["samples"], np.complex64 if iq else np.float32)
    return x, np.frombuffer(d["image"], np.uint8).reshape(m["height"], m["width"], 3)


def png_rgb(png: bytes) -> np.ndarray:
    """A PNG the decoder wrote, as height x width x 3 uint8."""
    w, h, rgb = _native.sstv_png_rgb(png)
    return np.frombuffer(rgb, np.uint8).reshape(h, w, 3)


def score(sent: np.ndarray, got: np.ndarray, partial: bool = False) -> dict:
    """How `got` compares with `sent`: {"psnr", "rows", "rowOffset",
    "inSync" (rows within a pixel of their place), "maxShift",
    "meanShift"}; a `partial` picture is compared where it fits best."""
    h, w, _ = sent.shape
    return _native.sstv_score(np.ascontiguousarray(sent, np.uint8).tobytes(), w, h,
                              np.ascontiguousarray(got, np.uint8).tobytes(), partial)
