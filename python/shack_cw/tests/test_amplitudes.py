"""The decoder's front end as a function (shack_cw.amplitudes): the step
from per-frame complex sums to what Decoder.feed_frames takes, shared by
Decoder.feed and the skimmer's filter bank."""
import numpy as np

import shack_cw
from shack_cw.decoder import Decoder, amplitudes, window_frames


def test_amplitudes_over_channels_is_each_channel_alone():
    # The skimmer runs amplitudes() once over every channel that shares a
    # window; that must be exactly the per-channel arithmetic feed() does.
    rng = np.random.default_rng(1)
    c = rng.standard_normal((30, 3, 7)) + 1j * rng.standard_normal((30, 3, 7))
    tone, quiet, loud, hist = amplitudes(c[:12], 3)
    tone2, quiet2, loud2, _ = amplitudes(c[12:], 3, hist)
    for k in range(3):
        a = amplitudes(c[:12, k], 3)
        b = amplitudes(c[12:, k], 3, a[3])
        np.testing.assert_allclose(tone[:, k], a[0])
        np.testing.assert_allclose(quiet2[:, k], b[1])
        np.testing.assert_allclose(loud2[:, k], b[2])
        np.testing.assert_allclose(tone2[:, k], b[0])
    assert np.all(quiet <= loud)


def test_window_frames_follows_the_speed():
    assert window_frames(1.2 / 12) == 5          # a quarter of a 100 ms dot
    assert window_frames(1.2 / 40) == 2
    assert window_frames(10.0) == 8 and window_frames(0.001) == 1
    d = Decoder()
    assert d.window_frames == window_frames(1.2 / 20)


def test_exports():
    assert shack_cw.amplitudes is amplitudes and shack_cw.window_frames is window_frames
