"""Helpers for the radio-link tests: a link that records what it does to a
pretend radio, waiting without sleeping blind, and audio that decodes."""

from __future__ import annotations

import time

from shack_packet import Frame, Receiver, ax25
from shack_packet.radios.base import AudioRadioLink


def until(cond, timeout: float = 5.0, step: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


class Sink:
    """Stands in for the engine: keeps what the link hands up, in order."""

    def __init__(self, station=None):
        self.station = station
        self.pcm = bytearray()
        self.busy: list[bool] = []
        self.events: list[tuple[str, str, dict]] = []

    def pcm_in(self, pcm) -> None:
        self.pcm += pcm

    def frame_in(self, frame) -> None:
        pass

    def busy_in(self, busy: bool) -> None:
        self.busy.append(busy)

    def link_event(self, kind, reason, detail=None) -> None:
        self.events.append((kind, reason, dict(detail or {})))

    def reasons(self) -> list[str]:
        return [r for _k, r, _d in self.events]


class Recorder(AudioRadioLink):
    """An AudioRadioLink whose radio is a list: every operation, timed."""

    rate = 12000

    def __init__(self, **kw):
        super().__init__(**kw)
        self.log: list[tuple[float, str, object]] = []
        self.fail_key = None
        self.fail_audio = False
        self.fail_unkey = 0
        self.alive = True
        self.busy_now = None
        self.drain_s = 0.05
        self.on_air = False

    def _note(self, what, arg=None):
        self.log.append((time.monotonic(), what, arg))

    def _open_radio(self):
        self._note("open")

    def _close_radio(self):
        self._note("close")

    def _radio_key(self):
        if self.fail_key is not None:
            raise self.fail_key
        self.on_air = True
        self._note("key")

    def _radio_unkey(self):
        if self.fail_unkey:
            self.fail_unkey -= 1
            raise OSError("the radio did not hear the unkey")
        self.on_air = False
        self._note("unkey")

    def _radio_audio(self, pcm):
        if self.fail_audio:
            raise OSError("the audio went nowhere")
        self._note("audio", len(pcm))
        return True

    def _radio_audio_end(self):
        self._note("end")

    def _radio_drain_s(self):
        return self.drain_s

    def _radio_alive(self):
        return self.alive

    def _radio_busy_now(self):
        return self.busy_now

    def what(self) -> list[str]:
        return [w for _t, w, _a in self.log]

    def when(self, what) -> list[float]:
        return [t for t, w, _a in self.log if w == what]


def heard_frames(pcm: bytes, rate: int) -> list[str]:
    """Every frame decoded from 16-bit audio, as monitor text."""
    rx = Receiver(rate)
    out = [h.frame for h in rx.feed(bytes(pcm))] + [h.frame for h in rx.flush()]
    return [ax25.parse(f).monitor() for f in out if ax25.parse(f) is not None]


def ui(text: str = "GM4BRI>APRS:>radio link test") -> Frame:
    return Frame.from_monitor(text)
