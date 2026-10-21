"""The receive-only SDR link (radios/sdrrx.py): frames on the air reach the
engine through the same receiver as PlutoLink's, busy follows the channel,
keying is refused before any radio call, and a receiver that fails is said
once. Against sdr.SyntheticSource, and an rtl_tcp server speaking the
protocol, which is how an RTL-SDR on another computer arrives."""

from __future__ import annotations

import pytest
from radiohelp import Sink, heard_frames, until

from shack_packet import Station
from shack_packet.ax25 import Frame
from shack_packet.radios import REASONS, LinkRefused, SdrRxLink
from shack_packet.sdr import RtlTcpServer, RtlTcpSource, SyntheticSource
from shack_packet.sdr import Station as OnAir
from shack_packet.sdr.source import IqSource, SdrError

FREQ = 144_800_000
ARMED = Station.of("GM4BRI", tx_enabled=True)


def frames(tag, n=3):
    return [Frame.from_monitor(f"M0ABC>APZOHS,WIDE1-1:>{tag} {i}").encode() for i in range(n)]


def band(centre, rate=2_400_000, seconds=4.0, tag="rtl"):
    return SyntheticSource(centre, rate, [OnAir(FREQ, frames(tag), level_db=-30, start_s=0.3, every_s=1.0)],
                           seconds=seconds, noise_dbfs=-50)


def test_frames_on_the_channel_reach_the_engine():
    src = band(0.0)
    link = SdrRxLink(src, FREQ, kind="synthetic")
    sink = Sink(ARMED)
    link.attach(sink)
    with link:
        assert src.centre == FREQ - 100_000                 # tuned below the channel: DC outside it
        assert until(lambda: len(set(heard_frames(sink.pcm, 12_000))) == 3, 15)
    assert set(heard_frames(sink.pcm, 12_000)) == {f"M0ABC>APZOHS,WIDE1-1:>rtl {i}" for i in range(3)}
    assert True in sink.busy                                # each transmission made the channel busy


def test_it_never_keys_whatever_the_station_says():
    link = SdrRxLink(band(0.0), FREQ)
    link.attach(Sink(ARMED))
    with link:
        with pytest.raises(LinkRefused) as e:
            link.key()
        assert e.value.reason == "receive-only" and not link.keyed
        link.send_pcm(b"\0\0" * 1200)
        assert link.stats["audio_refused"] == 1
    assert link.RECEIVE_ONLY and "receive-only" in REASONS


def test_an_rtl_tcp_receiver_is_a_station_radio():
    with RtlTcpServer(lambda c, r: band(c, r, tag="tcp"), realtime=False) as server:
        link = SdrRxLink(RtlTcpSource("127.0.0.1", server.port, rate=2_400_000), FREQ, kind="rtltcp")
        sink = Sink(ARMED)
        link.attach(sink)
        with link:
            assert until(lambda: len(set(heard_frames(sink.pcm, 12_000))) == 3, 15)


class Failing(IqSource):
    kind = "failing"

    def __init__(self):
        super().__init__(0.0, 2_400_000)
        self.reads = 0

    def _open(self):
        pass

    def _read(self):
        self.reads += 1
        if self.reads > 2:
            raise SdrError("disconnected", "unplugged")
        import numpy as np
        return np.zeros(120_000, np.complex64)


def test_a_receiver_that_fails_is_said_once_and_the_link_stays_open():
    link = SdrRxLink(Failing(), FREQ, kind="failing")
    sink = Sink(ARMED)
    link.attach(sink)
    with link:
        assert until(lambda: ("radio", "radio-error") in [(k, r) for k, r, _d in sink.events], 5)
        assert link.is_open
    assert [r for r in sink.reasons() if r == "radio-error"] == ["radio-error"]


def test_a_receiver_that_will_not_open_refuses_the_link():
    class NoDevice(Failing):
        def _open(self):
            raise SdrError("no-device", "nothing on USB")
    with pytest.raises(LinkRefused) as e:
        SdrRxLink(NoDevice(), FREQ).open()
    assert e.value.reason == "radio-error" and "no-device" in e.value.detail
