"""The fast modes on a Pluto (radios/pluto.py with `baud`), against the
fake libiio: the link's own transmit IQ - pushed through the same writer,
guard rules and quiet-after-every-over as 1200 baud - goes through a second
fast-mode link's receiver, off frequency and in noise, and decodes; the
loopback bench's frame success against CNR; the engine picking the G3RUH
modem from the link."""

from __future__ import annotations

import numpy as np
import pytest
from radiohelp import until
from sdrhelp import FakePluto

from shack_packet import Station, afsk, fsk, fsk_bench, modem
from shack_packet.afsk import pcm16
from shack_packet.ax25 import Frame
from shack_packet.engine import Engine
from shack_packet.radios import LinkRefused, PlutoLink
from shack_packet.radios.pluto import FSK_RATE_IQ
from shack_packet.sdr.pluto import TX_ATTEN_MAX

FREQ = 432_650_000
IFO = 100_000
ARMED = Station.of("GM4BRI", tx_enabled=True)
QUIET = [("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", str(TX_ATTEN_MAX))]


class FloatSink:
    """The engine's place: keeps the discriminator arrays a fast link hands up."""

    def __init__(self, station=None):
        self.station = station
        self.chunks: list[np.ndarray] = []
        self.events: list = []

    def pcm_in(self, pcm) -> None:
        self.chunks.append(np.asarray(pcm, dtype=np.float64))

    def frame_in(self, frame) -> None:
        pass

    def busy_in(self, busy) -> None:
        pass

    def link_event(self, kind, reason, detail=None) -> None:
        self.events.append((kind, reason))

    def samples(self) -> int:
        return sum(len(c) for c in self.chunks)


def make(baud, radio=None, *, realtime=True, station=ARMED, **kw):
    radio = radio or FakePluto()
    radio.rx_realtime = realtime
    sink = FloatSink(station)
    link = PlutoLink("ip:192.168.2.1", FREQ, iio_module=radio.module(), baud=baud, allow_unguarded=True, **kw)
    link.attach(sink)
    return radio, link, sink


def frames(n=3, tag="fast"):
    return [Frame.from_monitor(f"GM4BRI>APZOHS:>{tag} {i} " + "y" * (30 * i)).encode() for i in range(n)]


def test_a_fast_link_runs_at_a_rate_every_speed_divides():
    for baud, rate in ((9600, 76_800), (19_200, 153_600), (38_400, 307_200)):
        radio, link, _ = make(baud)
        assert link.rate == rate and link.rate_iq == FSK_RATE_IQ == 256 * 9600
        assert link.deviation_hz == fsk.deviation_hz(baud)
        with link:
            assert radio.attr("rx", "sampling_frequency") == radio.attr("tx", "sampling_frequency") == str(FSK_RATE_IQ)
            assert link.receiver.rate == rate
    # 1200 is unchanged.
    _, slow, _ = make(1200)
    assert slow.rate == 12_000 and slow.rate_iq == 2_400_000
    with pytest.raises(LinkRefused):
        make(4800)
    with pytest.raises(LinkRefused):
        make(9600, rate_iq=2_400_000)                # 31.25 IQ samples a modem sample


def test_the_engine_takes_the_g3ruh_modem_from_the_link():
    _, link, _ = make(9600)
    eng = Engine(ARMED, link)
    assert eng.channel.baud == 9600 and isinstance(eng.receiver, fsk.Receiver)
    assert eng.receiver.rate == 76_800


@pytest.mark.parametrize("baud,offset", [(9600, 3_000), (9600, -3_000), (19_200, 3_000), (38_400, -3_000)])
def test_frames_go_out_as_fsk_beside_the_lo_and_come_back_off_frequency(baud, offset):
    radio, link, _ = make(baud)
    sent = frames(3, f"b{baud}")
    over = fsk.modulate(sent, link.rate, baud=baud, amplitude=0.5, txdelay_ms=100, tail_ms=10)
    with link:
        link.key()
        link.send_pcm(pcm16(over))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        assert radio.tx_quiet()
    assert radio.tx_writes()[-2:] == QUIET
    live = [i for i, (_d, on) in enumerate(radio.pushed) if on]
    keep = FakePluto()
    keep.pushed = radio.pushed[live[0]:live[-1] + 1]
    iq = keep.tx_iq()
    # Constant envelope, never starved; the signal sits at +ifo inside its
    # occupied bandwidth, and nothing to speak of at the LO.
    assert np.abs(iq).min() > 0.65
    spec = np.abs(np.fft.fft(iq * np.hanning(len(iq)))) ** 2
    f = np.fft.fftfreq(len(iq), 1 / FSK_RATE_IQ)
    occupied = fsk.occupied_hz(baud)
    assert spec[np.abs(f - IFO) < occupied].sum() > 0.999 * spec.sum()
    assert spec[np.abs(f) < 12_500].sum() < 1e-6 * spec.sum()

    # Into a second fast link's receiver, `offset` Hz off (the nano's -19
    # ppm before correction is far more; this is what is left after), with
    # noise well above the threshold.
    rng = np.random.default_rng(baud + offset)
    n = np.arange(len(iq))
    lead = FSK_RATE_IQ // 2
    noise = lambda k: rng.normal(0, 0.02, k) + 1j * rng.normal(0, 0.02, k)  # noqa: E731
    air = np.concatenate([noise(lead), iq * np.exp(2j * np.pi * offset * n / FSK_RATE_IQ) + noise(len(iq)),
                          noise(lead)])
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([air.real, air.imag]).ravel() * 2047).astype(np.int16)
    _, link2, sink2 = make(baud, rx, realtime=False)
    with link2:
        assert until(lambda: sink2.samples() >= link2.rate * len(air) / FSK_RATE_IQ * 0.95, 30)
    receiver = fsk.Receiver(link2.rate, baud)
    heard = receiver.feed(np.concatenate(sink2.chunks)) + receiver.flush()
    assert [h.frame for h in heard] == sent


@pytest.mark.parametrize("baud", fsk.BAUDS)
def test_frame_success_rises_with_cnr_through_the_iq_path(baud):
    """The bench's loopback (fsk_bench: each frame its own over, acquired
    from cold). Measured 18/10/2026, 30 frames at 8/10/12/14 dB decoded
    0-1/1-16/24-30/29-30 across the bauds and offsets (README table); held
    here at the two ends, with a margin, so a receiver that got worse fails."""
    low, n, _ = fsk_bench.loopback(baud, 6, n=12, seed=2)
    high, _, _ = fsk_bench.loopback(baud, 15, n=12, seed=2)
    off, _, _ = fsk_bench.loopback(baud, 15, 3_000, n=12, seed=3)
    assert low <= 2 and high == n and off >= n - 1, (low, high, off)


def test_the_receiver_reports_how_far_off_a_station_is():
    """The keyed test prints it: two Plutos' crystals, seen from each end."""
    from shack_packet.radios.sdrrx import FskChannelReceiver
    iq = fsk_bench.over_iq(frames(4), 9600, offset_hz=-2_400.0, lead_s=1.0)
    chan = FskChannelReceiver(FSK_RATE_IQ, FREQ - IFO, FREQ, baud=9600)
    rng = np.random.default_rng(4)
    iq = fsk_bench.add_noise(iq, 9600, 20, rng)
    for at in range(0, len(iq), 122_880):
        chan.hear(iq[at:at + 122_880])
    assert chan.freq_hz == pytest.approx(-2_400, abs=150)


class _Relay:
    """The relay client's place for linktest.Telemetry: keeps what it sends."""
    env: dict = {}
    call, role = "G0ABC-1", "rf-receiver"

    def __init__(self):
        self.on_tele = None
        self.sent: list[dict] = []

    def send(self, msg):
        self.sent.append(msg)


@pytest.mark.parametrize("baud", [1200, 9600])
def test_a_frame_a_fixed_mode_pluto_hears_carries_its_c_n0_and_snr(baud):
    # A Pluto on one mode (not an adaptive ladder) handed the engine its
    # audio with no level, so every rx-frame line of the link test read
    # "C/N0 -" and "SNR -" (19/10/2026, with GM0OTB). The receiver's level
    # now travels with the audio it was measured on, and the C/N0 it gives
    # is the carrier's in the noise put on the air here, to a few dB.
    from shack_packet.linktest import Telemetry
    from shack_packet.radios.pluto import IQ_AMPLITUDE, FmModulator
    rng = np.random.default_rng(baud)
    sent = frames(1, f"level {baud}")
    if baud == 1200:
        rate_iq, sigma = 2_400_000, 0.3                # about 27 dB over the noise in 13 kHz
        audio = afsk.modulate(sent, 12_000, amplitude=0.5, txdelay_ms=200)
        lead = np.zeros(int(1.5 * rate_iq), complex)
        iq = np.concatenate([lead, FmModulator(rate_iq, 3_000, IFO)(audio), lead[:rate_iq // 2]])
        iq = iq + rng.normal(0, sigma, len(iq)) + 1j * rng.normal(0, sigma, len(iq))
        cn0 = 10 * np.log10(IQ_AMPLITUDE ** 2 * rate_iq / (2 * sigma ** 2))
    else:
        iq = np.concatenate([fsk_bench.over_iq(sent, baud, lead_s=1.5), np.zeros(FSK_RATE_IQ // 2)])
        iq = fsk_bench.add_noise(iq, baud, 20.0, rng)
        cn0 = 20.0 + 10 * np.log10(fsk.occupied_hz(baud))
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([iq.real, iq.imag]).ravel() * 2047).astype(np.int16)
    _, link, _ = make(baud, rx, realtime=False)
    eng = Engine(Station.of("G0ABC", tx_enabled=False), link)
    relay = _Relay()
    tele = Telemetry(eng, relay, link)
    eng.taps.append(tele._tap)
    lines = lambda: [m for m in relay.sent if m["kind"] == "rx-frame"]  # noqa: E731
    with link:
        assert until(lambda: lines(), 30)
    line = lines()[0]
    assert line["via"] == "radio" and line["src"] == "GM4BRI" and line["type"] == "UI"
    assert abs(line["cn0_dbhz"] - cn0) < 3.0, (line["cn0_dbhz"], cn0)
    occupied = modem.mode(line["mode"]).occupied_hz
    if occupied:
        assert abs(line["snr_db"] - (line["cn0_dbhz"] - 10 * np.log10(occupied))) < 0.1
    else:
        # afsk1200 had no occupied bandwidth when this was written (one
        # was on its way, on another branch): no SNR rather than a wrong one.
        assert line["snr_db"] is None
