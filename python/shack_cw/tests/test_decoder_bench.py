"""The decoder bench's own instruments (tools/decoder_bench): the port of
fldigi's receiver has to decode clean Morse, or every number it gives on
the bands is the port's fault, not fldigi's; and the beacon score has to
score the call wherever it sits in a slot's text."""
import sys
from pathlib import Path

import numpy as np

from shack_cw import envelope

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "decoder_bench"))
from fldigi_rx import FldigiCw  # noqa: E402
from score import infix_cer  # noqa: E402


def _tone(text: str, wpm: float, snr_db: float, seed: int = 0) -> np.ndarray:
    rate = 8000
    env = np.concatenate([np.zeros(rate), envelope(text, wpm, rate, tail=1.5)])
    t = np.arange(len(env)) / rate
    rng = np.random.default_rng(seed)
    # SNR in 2.5 kHz: noise power 1 over 4 kHz, the tone's power 10^(snr/10) * 2500/4000.
    a = np.sqrt(2 * 10 ** (snr_db / 10) * 2500 / 4000)
    return (a * env * np.sin(2 * np.pi * 700 * t) + rng.normal(0, 1, len(t))).astype(np.float32)


def test_the_fldigi_port_copies_clean_morse_at_its_speeds():
    # fldigi's tracking starts at 18 WPM and follows within +-10; its own
    # dot/dash pairs pull it there within the first word.
    for wpm in (15, 20, 25):
        x = _tone("CQ CQ DE GM4BRI GM4BRI K", wpm, 20.0)
        text = "".join(c for _, c in FldigiCw(700.0).decode(x))
        assert "GM4BRI" in text.replace(" ", ""), (wpm, text)


def test_the_fldigi_port_times_its_letters():
    x = _tone("PARIS", 20, 20.0)
    ev = FldigiCw(700.0).decode(x)
    first = next(t for t, c in ev if c.strip())
    # P ends 1 s (the leading silence) + 11 dots in, then the letter gap.
    assert 1.6 < first < 2.0, ev


def test_the_beacon_score_finds_the_call_among_the_dashes():
    assert infix_cer("4X6TU", "TT4X6TUTTTT") == 0.0
    assert infix_cer("4X6TU", "EE4X6TTTTT") == 0.2      # one letter missing
    assert infix_cer("OH2B", "") == 1.0
