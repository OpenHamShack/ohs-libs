"""Stations on a simulated channel, for the engine tests."""

from __future__ import annotations

import random

from shack_packet.channel import ChannelConfig
from shack_packet.datalink import LinkConfig
from shack_packet.engine import Engine
from shack_packet.identity import Station
from shack_packet.sim import Medium, SimClock, Simulation


class World:
    def __init__(self, mode="frames", seed=1, **medium):
        self.clock = SimClock()
        self.medium = Medium(self.clock, mode, seed=seed, **medium)
        self.sim = Simulation(self.clock, self.medium)
        self.seed = seed
        self.stations: dict[str, Engine] = {}

    def station(self, call, *, channel=None, link=None, rise_s=0.0, tx=True, hears_itself=False, **kw) -> Engine:
        radio = self.medium.radio(call, rise_s=rise_s, hears_itself=hears_itself)
        engine = Engine(Station.of(call, tx_enabled=tx), radio, channel=channel or ChannelConfig(),
                        link=link or LinkConfig(), clock=self.clock,
                        rng=random.Random(f"{self.seed}-{call}"), **kw)
        self.sim.add(engine)
        self.stations[call] = engine
        return engine

    def run(self, seconds, until=None):
        return self.sim.run(seconds, until)


def collect(conn):
    """Give a connection an on_data that gathers what arrives, and a list
    of its events."""
    got = bytearray()
    events = []
    conn.on_data = got.extend
    conn.on_event = events.append
    return got, events


def accept_all(engine, call=None):
    """Make `engine` accept every incoming connection, gathering each one."""
    accepted = []

    def accept(request):
        def setup(conn):
            got, events = collect(conn)
            accepted.append((conn, got, events))
        return setup

    engine.listen(call or engine.station.address(), accept)
    return accepted
