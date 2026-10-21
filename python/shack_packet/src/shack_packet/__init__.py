"""shack_packet: the OpenHamShack packet radio modem, one of each (GUIDE:
shared components) - for the Packet Radio app, the Icom app's APRS window and
the SDR monitor.

Receive, at any sample rate from 8 kHz up:

    rx = shack_packet.Receiver(48_000)
    for heard in rx.feed(pcm):                   # s16le bytes or float samples
        frame = shack_packet.ax25.parse(heard.frame)
        print(frame.monitor(), heard.fx25)        # heard.fx25: tag if FX.25 fixed it

Transmit (fills an array; keying the radio is the app's, under its lease):

    frame = shack_packet.Frame.from_monitor("G0ABC-9>APRS,WIDE1-1:>hello")
    audio = shack_packet.modulate([frame.encode()], 48_000, fx25_mode="auto")

The layers, each usable alone: `afsk` (Bell 202 transmit), `demod` (Bell 202
receive, with the slicers and FX.25 and the de-duplication), `hdlc` (flags,
stuffing, FCS), `fx25` (Reed-Solomon and the correlation tags), `ax25` (the
frame codec, every frame type), `aprs` (APRS payloads). `python3 -m
shack_packet.bench` measures the receiver against direwolf.

The packet engine, on top (no GUI, no radio: the app supplies a
channel.RadioLink): `engine` (Engine, Connection, Runner), `channel`
(p-persistent CSMA and the radio-link interface), `datalink` (AX.25 2.2
connected mode), `xid`, `digi` (digipeater, off by default), `aprs_station`
(beacons, messaging, heard), `mailbox` (personal mailbox, off by default),
`ip` and `guard` (IP over AX.25 and the encryption guard), `identity`
(callsign and the transmit switch), `sim` (a simulated channel),
`netrom` (NODES
broadcasts and the node table), `terminal` (reading what nodes and BBSs
print) and `simnode` (a pretend BPQ node, BBS and chat for tests).

    station = shack_packet.Station.of("GM4BRI", tx_enabled=True)
    engine = shack_packet.Engine(station, radio)
    conn = engine.connect("GB7BBS", via=["MB7UC"])
    shack_packet.Runner(engine).start()
"""

from __future__ import annotations

from . import afsk, aprs, ax25, demod, fx25, hdlc
from .afsk import Modulator, modulate, pcm16, transmission_bits
from .ax25 import Address, Control, Frame, FrameError, FrameType, parse
from .channel import ChannelConfig, RadioLink
from .datalink import LinkConfig, State
from .demod import Received, Receiver
from .engine import Connection, Engine, Event, IncomingCall, Runner
from .errors import PacketError
from .identity import Station

__version__ = "0.1.0"

__all__ = ["Address", "ChannelConfig", "Connection", "Control", "Engine", "Event", "Frame", "FrameError",
           "FrameType", "IncomingCall", "LinkConfig", "Modulator", "PacketError", "RadioLink", "Received",
           "Receiver", "Runner", "State", "Station", "afsk", "aprs", "ax25", "demod", "fx25", "hdlc", "modulate",
           "parse", "pcm16", "transmission_bits"]
