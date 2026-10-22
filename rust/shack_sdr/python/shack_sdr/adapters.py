"""Session runners for the Python-backed decoders (catalogue.py), in the
shape the host drives: feed(samples, rate, t0, index), poll() -> [result],
status() -> dict, reset(), close().

Only CW has one in M1: shack_cw's Decoder, the one CW decoder every app
shares, fed the session's audio. Its events become `text` results in the
same shape the Rust decoders give, so a client's Decoders view treats them
alike.
"""

from __future__ import annotations

import numpy as np


class PythonSession:
    """The protocol, for an app writing its own runner (catalogue.register)."""

    def feed(self, samples: np.ndarray, rate: float, t0: float, index: int) -> None:
        raise NotImplementedError

    def poll(self) -> list[dict]:
        return []

    def status(self) -> dict:
        return {}

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class CwSession(PythonSession):
    def __init__(self, pitch_hz: float):
        import shack_cw
        self._make = lambda: shack_cw.Decoder(freq=pitch_hz)
        self.pitch = pitch_hz
        self.dec = self._make()
        self._out: list[dict] = []

    def feed(self, samples, rate, t0, index):
        events = self.dec.feed(np.asarray(samples, dtype=np.float32), rate)
        for ev in events:
            freq = ev.get("freq", self.pitch)
            self._out.append({"t": t0, "type": "text", "text": ev["text"], "offsetHz": float(freq) - self.pitch,
                              "snrDb": None, "wpm": ev.get("wpm")})

    def poll(self):
        out, self._out = self._out, []
        return out

    def status(self):
        return {"snrDb": self.dec.snr, "offsetHz": float(self.dec.pitch) - self.pitch, "wpm": self.dec.wpm,
                "sync": bool(self.dec.locked)}

    def reset(self):
        self.dec = self._make()


def cw_session(params: dict, rate: float, center_hz: float) -> CwSession:
    unknown = set(params) - {"pitchHz"}
    if unknown:
        raise ValueError("unknown_param")
    pitch = float(params.get("pitchHz", center_hz or 700.0))
    if not 200.0 <= pitch <= 3000.0:
        raise ValueError("bad_param")
    return CwSession(pitch)
