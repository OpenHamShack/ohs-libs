"""The OHS-only wide modes (modem.py, 18/10/2026): GFSK at 76 800 and
153 600 baud and 4-FSK at 38 400 and 76 800 baud, chosen by name; 4-FSK's
bit stream both ways; the engine running a connected transfer through
4-FSK; and a Pluto link set up for a wide mode (the channel further from
the LO, the TX filter asked for wide enough) whose transmit IQ decodes
through a second link's receiver."""

from __future__ import annotations

import numpy as np
import pytest
from radiohelp import until
from stations import World, accept_all
from test_fsk_iq import FREQ, FloatSink, frames
from sdrhelp import FakePluto

from shack_packet import Station, fsk, fsk_bench, modem
from shack_packet.afsk import pcm16
from shack_packet.channel import ChannelConfig
from shack_packet.datalink import LinkConfig
from shack_packet.engine import Engine
from shack_packet.errors import PacketError
from shack_packet.radios import LinkRefused, PlutoLink
from shack_packet.radios.pluto import FSK_RATE_IQ, TX_BANDWIDTH, WIDE_IFO_HZ

ARMED = Station.of("GM4BRI", tx_enabled=True)
FOUR = ("4fsk38400", "4fsk76800")


def decode(baseband, rate, mode, pieces=None):
    m = modem.mode(mode)
    rx = modem.receiver(rate, mode=m, block_ms=20.0)
    out = []
    step = pieces or len(baseband)
    for i in range(0, len(baseband), step):
        out += rx.feed(baseband[i:i + step])
    return [h.frame for h in out + rx.flush()]


def test_modes_by_name_and_the_old_numbers_still_work():
    assert modem.mode("4fsk76800").bitrate == 153_600 and modem.mode("4fsk76800").baud == 76_800
    assert modem.mode(9600).name == "g3ruh9600" and modem.mode("153600").name == "gfsk153600"
    assert modem.check(76_800) == 76_800 and modem.by_name("4fsk38400") == 76_800
    # 4-FSK has no number of its own: 76 800 is the two-level mode.
    assert modem.mode(76_800).kind == "gfsk"
    with pytest.raises(PacketError):
        modem.mode("8fsk")
    cfg = ChannelConfig(mode="4fsk38400")
    assert (cfg.mode, cfg.baud) == ("4fsk38400", 76_800)
    assert ChannelConfig(baud=9600).mode == "g3ruh9600"
    with pytest.raises(PacketError):
        ChannelConfig(mode="4fsk38400", baud=38_400)       # 38 400 symbols, but 76 800 bits a second


@pytest.mark.parametrize("mode", FOUR)
def test_4fsk_frames_go_round_built_and_heard_in_odd_pieces(mode):
    m = modem.mode(mode)
    rate = m.baud * 8
    sent = frames(5, mode)
    # Built bit by bit in odd-sized pieces, so a pair of bits is split
    # across calls; the result must be the same signal as built at once.
    mod = fsk.Modulator(rate, m.baud, 0.5, m.shape, bits=2)
    bits = fsk.transmission_bits(sent, txdelay_ms=20, tail_ms=5, baud=m.bitrate)
    parts = [mod.bits(bits[i:i + 37]) for i in range(0, len(bits), 37)] + [mod.flush()]
    pieced = np.concatenate(parts)
    whole = modem.modulate(sent, rate, mode=m, txdelay_ms=20, tail_ms=5)
    assert np.allclose(pieced, whole)
    assert decode(whole, rate, m, pieces=4_999) == sent


def test_4fsk_levels_are_gray_coded_and_symbols_carry_two_bits():
    # Neighbouring levels differ in one bit of their pair, so the commonest
    # error costs one bit, not two.
    order = np.argsort(fsk.FOUR_LEVELS)
    pairs = [(v >> 1, v & 1) for v in order]
    assert all(sum(a != b for a, b in zip(pairs[i], pairs[i + 1])) == 1 for i in range(3))
    # A long transmission takes half the symbols of the same bits in 2-FSK.
    sent = frames(4)
    two = fsk.modulate(sent, 307_200, baud=38_400, txdelay_ms=0, tail_ms=0)
    four = fsk.modulate(sent, 307_200, baud=38_400, bits=2, shape="rc", txdelay_ms=0, tail_ms=0)
    assert len(four) / len(two) == pytest.approx(0.5, rel=0.03)


def test_4fsk_is_not_polarity_free():
    # modem.py: an inverted discriminator flips the first bit of every pair,
    # which NRZI cannot undo (2-FSK's inversion it can).
    m = modem.mode("4fsk38400")
    bb = modem.modulate(frames(3), m.baud * 8, mode=m)
    assert decode(-bb, m.baud * 8, m) == []
    g = modem.mode("gfsk76800")
    assert len(decode(-modem.modulate(frames(3), g.baud * 8, mode=g), g.baud * 8, g)) == 3


@pytest.mark.parametrize("mode", FOUR)
def test_4fsk_frame_success_rises_with_cnr_through_the_iq_path(mode):
    """fsk_bench's loopback: 30 frames a cell, 18/10/2026, decoded 0-3 at
    12 dB and 29-30 at 20 dB across both modes and +-3 kHz (README table).
    Held at the ends with a margin."""
    low, n, _ = fsk_bench.loopback(mode, 10, n=12, seed=2)
    high, _, _ = fsk_bench.loopback(mode, 22, n=12, seed=2)
    off, _, _ = fsk_bench.loopback(mode, 22, 3_000, n=12, seed=3)
    assert low <= 2 and high == n and off >= n - 1, (low, high, off)


def test_a_transfer_through_4fsk_with_noise():
    """The engine on a 4-FSK channel (the simulated medium carries the
    baseband): connected mode, 6 KB, at better than half the bit rate."""
    world = World(mode="audio", snr_db=30.0, seed=5, rate=38_400 * 8, modem_mode="4fsk38400")
    cfg = ChannelConfig(txdelay_s=0.03, txtail_s=0.005)
    link = LinkConfig(k=7)
    a = world.station("G0ABC", channel=cfg, link=link)
    b = world.station("GM4BRI", channel=cfg, link=link)
    assert a.channel.mode.name == "4fsk38400" and a.channel.baud == 76_800
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(30, lambda: conn.connected)
    data = bytes((i * 7 + i // 256) % 256 for i in range(6000))
    start = world.clock()
    conn.send(data)
    _, got, _ = accepted[0]
    assert world.run(60, lambda: len(got) >= len(data))
    assert bytes(got) == data
    assert b.receiver.bits_per_symbol == 2 and b.receiver.frames > 0
    assert len(data) * 8 / (world.clock() - start) > 76_800 / 4


def test_a_link_and_a_channel_on_different_modes_are_refused():
    world = World(mode="audio", rate=38_400 * 8, modem_mode="4fsk38400")
    radio = world.medium.radio("A")
    with pytest.raises(PacketError):
        Engine(ARMED, radio, channel=ChannelConfig(baud=38_400), clock=world.clock)


def make(mode, radio=None, *, realtime=True, **kw):
    radio = radio or FakePluto()
    radio.rx_realtime = realtime
    sink = FloatSink(ARMED)
    link = PlutoLink("ip:192.168.2.1", FREQ, iio_module=radio.module(), mode=mode, allow_unguarded=True, **kw)
    link.attach(sink)
    return radio, link, sink


def test_a_wide_mode_link_moves_the_channel_from_the_lo_and_widens_the_tx_filter():
    for mode, rate, ifo in (("gfsk76800", 614_400, 100_000), ("gfsk153600", 1_228_800, WIDE_IFO_HZ),
                            ("4fsk38400", 307_200, 100_000), ("4fsk76800", 614_400, WIDE_IFO_HZ)):
        radio, link, _ = make(mode)
        assert (link.rate, link.ifo_hz, link.mode) == (rate, ifo, mode)
        assert link.baud == modem.mode(mode).bitrate
        assert Engine(ARMED, link).channel.mode.name == mode
        with link:
            assert radio.attr("tx", "sampling_frequency") == str(FSK_RATE_IQ)
            assert int(radio.attr("tx", "rf_bandwidth")) == link.tx_bandwidth >= TX_BANDWIDTH
            assert link.receiver.rate == rate
    # Wide enough for the far edge: 200 kHz out, 83 kHz either side of it.
    assert make("gfsk153600")[1].tx_bandwidth == 750_000
    # A wide mode cannot be put where the LO would sit inside it.
    with pytest.raises(LinkRefused):
        make("gfsk153600", ifo_hz=100_000)


@pytest.mark.parametrize("mode,offset", [("gfsk153600", 3_000), ("4fsk76800", -3_000), ("4fsk38400", 3_000)])
def test_wide_mode_frames_go_out_and_come_back_through_a_second_link(mode, offset):
    m = modem.mode(mode)
    radio, link, _ = make(mode)
    sent = frames(3, mode)
    with link:
        link.key()
        link.send_pcm(pcm16(modem.modulate(sent, link.rate, mode=m, txdelay_ms=50, tail_ms=5)))
        link.unkey()
        assert until(lambda: not link.keyed, 10)
        assert radio.tx_quiet()
    live = [i for i, (_d, on) in enumerate(radio.pushed) if on]
    keep = FakePluto()
    keep.pushed = radio.pushed[live[0]:live[-1] + 1]
    iq = keep.tx_iq()
    spec = np.abs(np.fft.fft(iq * np.hanning(len(iq)))) ** 2
    f = np.fft.fftfreq(len(iq), 1 / FSK_RATE_IQ)
    assert spec[np.abs(f - link.ifo_hz) < m.occupied_hz].sum() > 0.999 * spec.sum()
    rng = np.random.default_rng(7)
    n = np.arange(len(iq))
    lead = FSK_RATE_IQ // 2
    noise = lambda k: rng.normal(0, 0.01, k) + 1j * rng.normal(0, 0.01, k)  # noqa: E731
    air = np.concatenate([noise(lead), iq * np.exp(2j * np.pi * offset * n / FSK_RATE_IQ) + noise(len(iq)),
                          noise(lead)])
    rx = FakePluto()
    rx.iq = np.round(np.column_stack([air.real, air.imag]).ravel() * 2047).astype(np.int16)
    _, link2, sink2 = make(mode, rx, realtime=False)
    with link2:
        assert until(lambda: sink2.samples() >= link2.rate * len(air) / FSK_RATE_IQ * 0.95, 30)
    assert decode(np.concatenate(sink2.chunks), link2.rate, m) == sent
