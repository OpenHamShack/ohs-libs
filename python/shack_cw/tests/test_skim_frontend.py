"""The skimmer's filter bank (shack_cw.skim.frontend): one STFT over the
segment that drives a Decoder per signal through feed_frames."""
import numpy as np

from shack_cw import bench
from shack_cw.decoder import REF_OFFSETS, Decoder, amplitudes
from shack_cw.skim.frontend import Gather, Stft


def test_gather_is_coherent_off_bin_and_when_nudged():
    # A steady tone between bins: after de-rotation every frame's sum has
    # the same phase, so eight frames add to eight times one - and keep
    # doing so when the tracker nudges the channel's frequency.
    rate = 24000.0
    st = Stft(rate)
    f0 = 1234.5
    n = int(rate * 2)
    z = np.exp(2j * np.pi * f0 * np.arange(n) / rate).astype(np.complex64)
    _, X, _ = st.feed(z)
    g = Gather(st)
    c = g.take(X[10:60], [1], [f0])[:, 0, 0]
    assert abs(c[:8].sum()) > 0.99 * 8 * abs(c[0])
    c2 = g.take(X[60:120], [1], [f0 + 0.5])[:, 0, 0]
    assert abs(c2[:8].sum()) > 0.97 * 8 * abs(c2[0])
    # Per frame the 10 ms window is wide (the decoder's own: its main lobe
    # reaches +-200 Hz, and the coherent window narrows it); the outer
    # references, 225 Hz off, are clear of the tone.
    allc = g.take(X[120:], [2], [f0])[0, 0]
    assert abs(allc[1]) < 0.05 * abs(allc[0]) and abs(allc[6]) < 0.05 * abs(allc[0])
    assert len(REF_OFFSETS) == 7


def test_a_bank_channel_prints_what_the_decoder_prints():
    # The same keyed signal, decoded by a Decoder on receiver audio (its
    # own front end) and by one driven from the filter bank through
    # feed_frames: both print the message.
    rng = np.random.default_rng(3)
    text = "CQ CQ DE GM4BRI GM4BRI K"
    runs = bench.runs_for(text, 22.0)
    rate = 24000.0
    z = bench.render(runs, rng, 15.0, mark_hz=-2500.0, rate=rate)
    st = Stft(rate)
    g = Gather(st)
    dec = Decoder(None, freq=0.0, afc=False, polarity=False)
    hist = None
    out = []
    for i in range(0, len(z), int(0.075 * rate)):
        _, X, _ = st.feed(z[i:i + int(0.075 * rate)])
        c = g.take(X, [1], [-2500.0])[:, 0]
        tone, quiet, loud, hist = amplitudes(c, dec.window_frames, hist)
        out += dec.feed_frames(tone, quiet, loud)
    got = "".join(e["text"] for e in out)
    assert bench.cer(got, text) < 0.1, got
    ref, _, _ = bench.decode(bench.render(runs, np.random.default_rng(3), 15.0))
    assert bench.cer(ref, text) < 0.1, ref


def test_every_rate_a_bank_channel_can_have():
    # Radio's segment channel comes at whatever rate holds the segment:
    # 20 m at 96 kS/s, 40 m at 60 or 48, the 10 m fallback at 120, the
    # box recording at 200, Kiwi clips at 12. Each must give whole frames
    # and room for the detection window, with bins no coarser than ~24 Hz.
    for rate in (12_000.0, 24_000.0, 48_000.0, 60_000.0, 96_000.0, 120_000.0, 200_000.0, 240_000.0):
        st = Stft(rate)
        assert st.df <= 24.5, (rate, st.df)
        _, X, P = st.feed(np.zeros(int(0.2 * rate), np.complex64))
        assert len(X) == 40 and len(P) >= 8, (rate, len(X), len(P))
