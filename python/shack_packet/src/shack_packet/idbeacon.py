"""Identification for a station that answers on its own (the controller's
BBS): a plain-text UI frame to `ID`, sent only when the station has
transmitted since its last identification and that was at least
`every_s` ago, so a silent station stays silent (BPQ's IDINTERVAL
behaviour).

Every AX.25 frame already carries the station's callsign in its address,
but shifted a bit left, not as text a listener reads off a screen; the ID
frame says it in the clear, with what the station is ("GM4BRI-1 BBS,
GM4BRI-2 conference"). The default 15 minutes is the longest gap between
identifications a UK licence allows (confirmed by GM4BRI 20/10/2026;
BPQ's default is 10).

    IdBeacon(engine, "GM4BRI-1 BBS, GM4BRI-2 conference, 144.950 MHz")

An optional information beacon (`info_text`, `info_every_s`, at least 10
minutes; off by default) goes to `MAIL` on its own schedule, for "N new
bulletins" style notices. Neither is sent unless the station may
transmit: the engine's own refusal drops it otherwise, like any frame.
"""
from __future__ import annotations

from typing import Callable

from . import ax25

MIN_INFO_S = 600.0


class IdBeacon:
    def __init__(self, engine, text: str, *, every_s: float = 900.0, info_text: Callable[[], str] | str | None = None,
                 info_every_s: float = 0.0, via=()):
        self.engine = engine
        self.text = text
        self.every_s = every_s
        self.info_text = info_text
        self.info_every_s = max(info_every_s, MIN_INFO_S) if info_every_s else 0.0
        self.via = list(via)
        self.last_id: float | None = None
        self.last_info: float | None = None
        #: Something other than our own ID has gone out since the last ID.
        self.owed = False
        self.sent = 0
        engine.taps.append(self._tap)
        engine.services.append(self)

    def _tap(self, kind: str, detail: dict) -> None:
        if kind != "transmitted" or self.owed:
            return
        for raw in detail.get("frames") or ():
            frame = ax25.parse(raw)
            if frame is None or frame.destination.call != "ID":
                self.owed = True
                return

    def poll(self, now: float) -> None:
        if self.owed and (self.last_id is None or now - self.last_id >= self.every_s):
            self.engine.send_ui("ID", self.text, via=self.via)
            self.last_id = now
            self.owed = False
            self.sent += 1
        if self.info_every_s and (self.last_info is None or now - self.last_info >= self.info_every_s):
            text = self.info_text() if callable(self.info_text) else self.info_text
            self.last_info = now
            if text:
                self.engine.send_ui("MAIL", text, via=self.via)

    def next_deadline(self) -> float | None:
        due = []
        if self.owed:
            due.append(now_or(self.last_id, self.every_s))
        if self.info_every_s:
            due.append(now_or(self.last_info, self.info_every_s))
        return min(due) if due else None


def now_or(last: float | None, every: float) -> float:
    """When a schedule is next due: at once if it never ran."""
    return 0.0 if last is None else last + every
