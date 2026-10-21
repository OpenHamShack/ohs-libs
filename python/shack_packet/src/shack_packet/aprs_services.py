"""The scheduled APRS transmissions beyond the station's own beacon:
objects and items it has placed, its weather, and its telemetry. One
engine service sends them all, so the etiquette is kept in one place.

    sched = AprsSchedule(engine, ScheduleConfig())
    sched.place(PlacedObject("NET", 55.9, -3.2, "/r", "Net 2000 145.500"))
    sched.weather_source = lambda: (lat, lon, readings, when)     # or None
    sched.telemetry_source = lambda: [rx, tx, digi, igated, 0]

**Etiquette** (APRS 1.1, and the UK's "be kind to 1200 baud"):

- *Objects and items* every `object_every_s` (30 min, never under 10),
  each one a separate beacon; a killed one is sent three times, a minute
  apart, then forgotten, so maps take it away without it being repeated
  for ever.
- *Weather* every `weather_every_s` (10 min, never under 5), only when
  there is a reading from the last `weather_stale_s` (a dead station
  sends nothing rather than stale numbers).
- *Telemetry* every `telemetry_every_s` (20 min, never under 10); the
  PARM/UNIT/EQNS/BITS messages that say what the numbers are go with the
  first report and then every `meta_every` reports.
- Nothing within `min_gap_s` (30 s) of the last of these, so a station
  with five objects does not send five frames in a row; the path is the
  station's beacon path (WIDE1-1,WIDE2-1 by default).
- The first of each goes 30-90 s after start, at random, like beacons.

Transmitting goes through engine.send_ui, so the app's transmit arming
decides, as for every other frame.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from . import aprs
from .aprs import TOCALL
from .engine import Event
from .errors import PacketError

KILL_REPEATS = 3
KILL_GAP_S = 60.0


@dataclass
class PlacedObject:
    name: str
    lat: float
    lon: float
    symbol: str = "/."
    comment: str = ""
    item: bool = False        # an item (no timestamp) rather than an object
    live: bool = True
    #: Kills still to send, once killed.
    kills_left: int = KILL_REPEATS

    def text(self, when: float | None = None) -> str:
        if self.item:
            return aprs.format_item(self.name, self.lat, self.lon, self.symbol, self.comment, live=self.live)
        return aprs.format_object(self.name, self.lat, self.lon, self.symbol, self.comment, live=self.live,
                                  when=when)


@dataclass
class ScheduleConfig:
    via: list[str] = field(default_factory=lambda: ["WIDE1-1", "WIDE2-1"])
    object_every_s: float = 1800.0
    weather_every_s: float = 600.0
    weather_stale_s: float = 900.0
    weather_comment: str = ""
    telemetry_every_s: float = 1200.0
    meta_every: int = 6
    telemetry_names: list[str] = field(default_factory=lambda: ["RxPkts", "TxPkts", "Digi", "IGated", "Spare"])
    telemetry_units: list[str] = field(default_factory=lambda: ["pkts", "pkts", "pkts", "pkts", ""])
    telemetry_title: str = "OpenHamShack"
    min_gap_s: float = 30.0
    tocall: str = TOCALL

    def floors(self) -> None:
        self.object_every_s = max(600.0, self.object_every_s)
        self.weather_every_s = max(300.0, self.weather_every_s)
        self.telemetry_every_s = max(600.0, self.telemetry_every_s)


class AprsSchedule:
    def __init__(self, engine, config: ScheduleConfig | None = None, *, rng: random.Random | None = None,
                 wall_clock: Callable[[], float] = time.time):
        self.engine = engine
        self.config = config or ScheduleConfig()
        self.config.floors()
        self.rng = rng or engine.rng
        self.wall_clock = wall_clock
        self.objects: dict[str, PlacedObject] = {}
        self._due: dict[str, float] = {}
        self.weather_source: Callable[[], tuple | None] | None = None
        self.telemetry_source: Callable[[], list] | None = None
        self.sequence = 0
        self._last_sent: float | None = None
        now = engine.clock()
        self._weather_due = now + self.rng.uniform(30.0, 90.0)
        self._telemetry_due = now + self.rng.uniform(30.0, 90.0)
        self.sent: dict[str, int] = {"objects": 0, "weather": 0, "telemetry": 0}
        engine.services.append(self)

    # ------------------------------------------------------------------ objects
    def place(self, obj: PlacedObject) -> None:
        """Add or move an object; it is sent within a minute or so, then on
        the schedule. Raises PacketError when it cannot be formatted."""
        obj.text()                         # refuse a bad name or symbol now, not at sending time
        key = obj.name.strip().upper()
        with self.engine.lock:
            obj.live, obj.kills_left = True, KILL_REPEATS
            self.objects[key] = obj
            self._due[key] = self.engine.clock() + self.rng.uniform(5.0, 15.0)
        self.engine._wake()

    def kill(self, name: str) -> bool:
        key = name.strip().upper()
        with self.engine.lock:
            obj = self.objects.get(key)
            if obj is None:
                return False
            obj.live = False
            obj.kills_left = KILL_REPEATS
            self._due[key] = self.engine.clock()
        self.engine._wake()
        return True

    def forget(self, name: str) -> None:
        """Gone without a kill (an object never sent)."""
        key = name.strip().upper()
        with self.engine.lock:
            self.objects.pop(key, None)
            self._due.pop(key, None)

    # ------------------------------------------------------------------ the clock
    def _send(self, text: str, what: str) -> None:
        self.engine.send_ui(self.config.tocall, text, via=list(self.config.via))
        self._last_sent = self.engine.clock()
        self.sent[what] += 1
        self.engine._emit(Event("aprs-sent", what, detail={"text": text}))

    def _gap_ok(self, now: float) -> bool:
        return self._last_sent is None or now - self._last_sent >= self.config.min_gap_s

    def poll(self, now: float) -> None:
        cfg = self.config
        for key, due in sorted(self._due.items(), key=lambda kv: kv[1]):
            if now < due or not self._gap_ok(now):
                continue
            obj = self.objects.get(key)
            if obj is None:
                self._due.pop(key, None)
                continue
            try:
                self._send(obj.text(self.wall_clock()), "objects")
            except PacketError:
                self.forget(key)
                continue
            if obj.live:
                self._due[key] = now + cfg.object_every_s
            else:
                obj.kills_left -= 1
                if obj.kills_left <= 0:
                    self.objects.pop(key, None)
                    self._due.pop(key, None)
                else:
                    self._due[key] = now + KILL_GAP_S
        if now >= self._weather_due and self._gap_ok(now):
            self._weather_due = now + cfg.weather_every_s
            got = self.weather_source() if self.weather_source is not None else None
            if got is not None:
                lat, lon, readings, when = got
                if readings and (when is None or self.wall_clock() - when <= cfg.weather_stale_s):
                    self._send(aprs.format_weather(lat, lon, readings, cfg.weather_comment), "weather")
        if now >= self._telemetry_due and self._gap_ok(now):
            self._telemetry_due = now + cfg.telemetry_every_s
            if self.telemetry_source is not None:
                values = self.telemetry_source()
                if values is not None:
                    self._send_telemetry(values)

    def _send_telemetry(self, values: list) -> None:
        cfg = self.config
        call = str(self.engine.station.address())
        if self.sequence % max(1, cfg.meta_every) == 0:
            for kind, vals in (("PARM", cfg.telemetry_names), ("UNIT", cfg.telemetry_units),
                               ("EQNS", [0, 1, 0] * 5), ("BITS", ["11111111", cfg.telemetry_title])):
                self.engine.send_ui(cfg.tocall, aprs.format_telemetry_meta(call, kind, vals), via=list(cfg.via))
        self._send(aprs.format_telemetry(self.sequence, values), "telemetry")
        self.sequence = (self.sequence + 1) % 1000

    def next_deadline(self) -> float | None:
        due = list(self._due.values())
        if self.weather_source is not None:
            due.append(self._weather_due)
        if self.telemetry_source is not None:
            due.append(self._telemetry_due)
        if not due:
            return None
        soonest = min(due)
        if self._last_sent is not None:
            soonest = max(soonest, self._last_sent + self.config.min_gap_s)
        return soonest
