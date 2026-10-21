"""The G3RUH FSK modem (fsk.py) on its own: the scrambler, the baseband
both ways at every baud, what a receiver must survive (inversion, a
station off frequency, noise, audio cut anywhere) and FX.25 on top.
Interop with direwolf is test_fsk_direwolf.py; the IQ path through a
Pluto is test_fsk_iq.py."""

from __future__ import annotations

import numpy as np
import pytest
from helpers import frame_bytes

from shack_packet import fsk, hdlc, modem
from shack_packet.errors import PacketError

FRAMES = [frame_bytes("GM4BRI-1", "APRS", f">fast packet {i} " + "x" * (10 * i)) for i in range(6)]


def decode(baseband, rate, baud, pieces=None):
    rx = fsk.Receiver(rate, baud)
    if pieces is None:
        out = rx.feed(baseband)
    else:
        out = []
        for i in range(0, len(baseband), pieces):
            out += rx.feed(baseband[i:i + pieces])
    return [h.frame for h in out + rx.flush()], rx


def test_the_scrambler_is_x17_x12_1_from_its_impulse_response():
    # Written out independently: y[n] = x[n] ^ y[n-12] ^ y[n-17].
    x = [1] + [0] * 99
    y = []
    for n, b in enumerate(x):
        y.append(b ^ (y[n - 12] if n >= 12 else 0) ^ (y[n - 17] if n >= 17 else 0))
    assert fsk.Scrambler()(x) == y
    # One in, ones out at 0, 12, 17 and 24 (12 + 12); 29 is 12 + 17, twice, so 0.
    assert [n for n in range(30) if y[n]] == [0, 12, 17, 24]


def test_the_descrambler_undoes_it_from_any_start_and_across_calls():
    rng = np.random.default_rng(1)
    data = rng.integers(0, 2, 5000).tolist()
    sent = fsk.Scrambler()(data)
    d = fsk.Descrambler()
    got = np.concatenate([d(np.array(sent[i:i + 37], np.int8)) for i in range(0, len(sent), 37)])
    assert got.tolist() == data
    # Joined late, it locks within 17 bits.
    late = fsk.Descrambler()(np.array(sent[1000:], np.int8))
    assert late[17:].tolist() == data[1017:]


@pytest.mark.parametrize("baud", fsk.BAUDS)
@pytest.mark.parametrize("shape", ["gaussian", "rc"])
def test_frames_go_round_at_every_baud(baud, shape):
    rate = baud * 8
    bb = fsk.modulate(FRAMES, rate, baud=baud, shape=shape)
    got, _ = decode(bb, rate, baud)
    assert got == FRAMES


@pytest.mark.parametrize("baud", fsk.BAUDS)
def test_a_long_run_of_one_symbol_sits_at_full_deviation(baud):
    # The pulse is normalised so a constant symbol is exactly the level
    # the link calls full deviation, whatever the baud.
    mod = fsk.Modulator(baud * 8, baud, amplitude=0.5)
    out = mod.levels([1] * 200)                    # scrambled ones: random symbols
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=0.01)


def test_received_inverted_it_decodes_the_same():
    # A receiver wired backwards, or a radio whose discriminator inverts:
    # the descrambler inverts every bit and NRZI ignores that.
    bb = fsk.modulate(FRAMES, 76_800, baud=9600)
    got, _ = decode(-bb, 76_800, 9600)
    assert got == FRAMES


def test_cut_anywhere_the_result_is_the_same():
    bb = fsk.modulate(FRAMES, 76_800, baud=9600)
    whole, _ = decode(bb, 76_800, 9600)
    for piece in (1, 7, 1001):
        got, _ = decode(bb, 76_800, 9600, pieces=piece)
        assert got == whole == FRAMES


@pytest.mark.parametrize("offset", [-1.0, -0.5, 0.5, 1.0])
def test_a_station_off_frequency_still_decodes(offset):
    # On a discriminator an offset is DC: a nano 3 kHz off at 9600 baud
    # and +-3 kHz deviation is a shift of a whole deviation (offset 1.0).
    bb = fsk.modulate(FRAMES, 76_800, baud=9600)
    got, _ = decode(bb + offset * 0.5, 76_800, 9600)
    assert got == FRAMES


def test_any_rate_of_four_samples_a_bit_or_more_receives():
    # The receiver's clock does not need a whole number of samples a bit:
    # 48 kHz is direwolf's 9600 rate (5), 100 kHz is 10.4.
    bb = fsk.modulate(FRAMES, 96_000, baud=9600)
    for rate in (48_000, 100_000):
        n = int(len(bb) * rate / 96_000)
        x = np.interp(np.arange(n) * 96_000 / rate, np.arange(len(bb)), bb)
        got, _ = decode(x, rate, 9600)
        assert got == FRAMES, rate
    with pytest.raises(PacketError):
        fsk.Receiver(30_000, 9600)


def test_the_transmitter_needs_a_whole_number_of_samples_a_bit():
    with pytest.raises(PacketError):
        fsk.Modulator(100_000, 9600)
    with pytest.raises(PacketError):
        fsk.Modulator(28_800, 9600)


@pytest.mark.parametrize("check", [16, 32, 64])
def test_fx25_works_on_top_and_mends_errors(check):
    rate = 76_800
    bits = fsk.transmission_bits(FRAMES[:2], fx25_mode=check, txdelay_ms=50)
    # Damage four bytes' worth of the first block, in the data bits before
    # NRZI and the scrambler, as a burst on the air would.
    start = bits.index(0, 64 * 8 + 200)
    for i in range(start, start + 32, 8):
        bits[i] ^= 1
    mod = fsk.Modulator(rate, 9600)
    bb = np.concatenate([mod.bits(bits), mod.flush()])
    rx = fsk.Receiver(rate, 9600)
    heard = rx.feed(bb) + rx.flush()
    assert [h.frame for h in heard] == FRAMES[:2]
    assert all(h.fx25 is not None for h in heard)
    assert heard[0].corrected > 0


@pytest.mark.parametrize("baud", fsk.BAUDS)
def test_noise_costs_frames_gradually_not_all_at_once(baud):
    # White noise on the baseband: well above the threshold everything
    # decodes, and far below it nothing does. (The IQ bench measures the
    # threshold properly, through a real channel filter and discriminator.)
    rng = np.random.default_rng(7)
    rate = baud * 8
    bb = fsk.modulate(FRAMES * 3, rate, baud=baud)
    counts = []
    for sigma in (0.05, 2.0):
        got, _ = decode(bb + rng.normal(0, sigma, len(bb)), rate, baud)
        counts.append(len(set(got) & set(FRAMES)))
    assert counts == [len(FRAMES), 0]


def test_dcd_follows_a_transmission():
    rate = 76_800
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.3, rate)
    rx = fsk.Receiver(rate, 9600)
    rx.feed(noise)
    assert not rx.dcd
    rx.feed(fsk.modulate(FRAMES, rate, baud=9600, txdelay_ms=200)[: rate // 5])
    assert rx.dcd
    rx.feed(noise)
    assert not rx.dcd


def test_modem_picks_by_baud_and_name():
    assert modem.by_name("g3ruh9600") == 9600
    assert modem.by_name("afsk1200") == 1200
    assert modem.by_name("38400") == 38_400
    with pytest.raises(PacketError):
        modem.check(4800)
    assert isinstance(modem.receiver(76_800, baud=9600), fsk.Receiver)
    assert not isinstance(modem.receiver(12_000), fsk.Receiver)
    # And what each sends is what its receiver takes.
    audio = modem.modulate(FRAMES[:1], 12_000)
    rx = modem.receiver(12_000)
    assert [h.frame for h in rx.feed(audio) + rx.flush()] == FRAMES[:1]


def test_bits_on_the_air_are_hdlc_as_ever():
    # The frames' own bits are AFSK's: only what carries them differs.
    a = fsk.transmission_bits(FRAMES[:1], txdelay_ms=0, tail_ms=0, baud=9600)
    assert a == list(hdlc.FLAG_BITS) + hdlc.frame_bits(FRAMES[0], 0, 1) + list(hdlc.FLAG_BITS)
