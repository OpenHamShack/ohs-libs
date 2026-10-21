"""The SDR monitor: receive-only, many packet channels at once from one
wide IQ capture (step 4 of OpenHamShack Packet Radio). GUI-free; the Qt
app (step 5) draws `Monitor`.

    from shack_packet import sdr
    plan = sdr.default_plan()                       # 144.800, the node channels, the ISS
    source = sdr.RtlSdrSource(plan.centre_for(2.4e6), 2.4e6)
    pipeline = sdr.Pipeline(source.rate, source.centre, plan, ppm=1.5)
    runner = sdr.Runner(source, pipeline)
    runner.start()
    ...  pipeline.monitor.snapshot()                # frames, heard, nodes, links, activity, map, waterfall
    runner.stop()

The layers: `source` (the IqSource interface; `SdrError` and its REASONS),
the sources `rtlsdr` (librtlsdr over ctypes), `rtltcp` (the network
protocol), `sdrplay` (SDRplay's API, the user's own install, never bundled), `pluto` (libiio, receive only, transmitter held quiet), `files`
(SigMF and raw recordings, and `record`) and `synth` (a made-up band and a
fake rtl_tcp server); `plan` (which channels: data, not constants);
`channeliser` (one FFT filter bank, FM discriminators); `spectrum`
(waterfall rows); `pipeline` (channeliser, a receiver a channel, levels);
`monitor` (the tables); `runner` (threads, the queue, drops); `bench`
(the CPU figures).

Receive only by construction: nothing in this package has a transmit
method, the Pluto backend refuses every transmitter write but "quiet",
and the package imports neither the radio links nor shack_rig (a test
checks).
"""

from __future__ import annotations

from .channeliser import Channeliser, pick_decimation
from .files import FileSource, record
from .monitor import ROLES, Decoded, Monitor
from .pipeline import Pipeline
from .plan import KINDS, Channel, ChannelPlan, default_plan, from_dict, from_spots
from .pluto import PlutoSource
from .rtlsdr import RtlSdrSource
from .rtltcp import RtlTcpSource
from .sdrplay import SdrplaySource
from .runner import Runner
from .source import REASONS, IqBlock, IqSource, SdrError
from .spectrum import Spectrum
from .synth import Neighbour, RtlTcpServer, Station, SyntheticSource

__all__ = ["KINDS", "REASONS", "ROLES", "Channel", "ChannelPlan", "Channeliser", "Decoded", "FileSource", "IqBlock",
           "IqSource", "Monitor", "Neighbour", "Pipeline", "PlutoSource", "RtlSdrSource", "RtlTcpServer",
           "RtlTcpSource", "Runner", "SdrError", "SdrplaySource", "Spectrum", "Station", "SyntheticSource", "default_plan",
           "from_dict", "from_spots", "pick_decimation", "record"]
