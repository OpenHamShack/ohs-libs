"""The Morse tables and keying envelopes."""
import numpy as np

from shack_cw import CODE_TO_CHAR, CODE_TO_PROSIGN, MORSE, envelope, envelope_of, runs, text_to_codes


def test_paris_is_fifty_dits_with_its_word_space():
    # PARIS is 43 dits without the trailing word gap; dit = 60 ms at 20 wpm
    # (from openhamshack-nb tests/test_dsp.py, which this module came from).
    env = envelope("PARIS", 20, 1000)
    assert abs(len(env) / 1000 - 43 * 0.06) < 0.01
    assert abs(sum(d for _, d in runs("PARIS PARIS", 20)) - (2 * 43 + 7) * 0.06) < 1e-9


def test_the_envelope_follows_the_rate_and_never_clicks():
    for rate in (8000, 12000, 48000):
        env = envelope("E", 20, rate, tail=0.1)
        assert len(env) == round(0.06 * rate) + round(0.1 * rate)
        assert env.dtype == np.float32
        # Edges spread over 5 ms (40 samples even at 8 kHz): no step a click.
        assert float(np.abs(np.diff(env)).max()) < 0.1 and float(env.max()) > 0.99


def test_prosigns_are_one_code_and_typos_are_dropped():
    assert text_to_codes("CQ <AR>") == ["-.-.", "--.-", " ", ".-.-."]
    assert text_to_codes("k~") == ["-.-"]
    assert text_to_codes("<XX>") == ["-..-", "-..-"]   # not a prosign: its letters


def test_every_code_reads_back_as_its_character():
    for ch, code in MORSE.items():
        assert CODE_TO_PROSIGN.get(code) or CODE_TO_CHAR[code] == ch


def test_an_empty_run_list_is_an_empty_envelope():
    assert len(envelope_of([], 12000)) == 0
