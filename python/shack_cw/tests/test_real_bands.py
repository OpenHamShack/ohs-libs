"""The decoder on the bands themselves, and on the bench cases they added.

The fixtures are short clips of real IQ cut from the corpus
(tools/cw_corpus.py fixture; shack_cw.corpus has the whole of it): 80 m
static at night, a strong 30 m station calling CQ and the pitch 600 Hz
beside it in a 1500 Hz filter, and two NCDXF beacons on 20 m. The decoder
as shipped printed Morse from the static and from beside the station -
Brian's 20 m report of 13/10/2026 - and these hold it to printing nothing
there while still reading the stations.
"""
import unittest
from pathlib import Path

import numpy as np

from shack_cw import Decoder, bench, corpus

FIX = Path(__file__).parent / "fixtures"


def decode_fixture(name: str, width: float = 500.0, **opts) -> tuple[str, list]:
    z, rate = corpus.load_iq(FIX / f"{name}.npz")
    events, _ = corpus.decode_timed(z, rate, 0.0, width=width,
                                    decoder=lambda emit, freq: Decoder(emit, freq=freq, **opts))
    return corpus._text(events), [e for _, e in events]


class RealBandsTests(unittest.TestCase):
    def test_80m_static_prints_nothing(self):
        # 16 s of 80 m at night (K1VL, 3548.8 kHz) with no station on it:
        # the decoder as shipped printed "E E E E" and showed a speed.
        text, events = decode_fixture("static-80m")
        self.assertEqual(text.strip(), "")
        self.assertFalse(any(e.get("wpm") for e in events))

    def test_beside_a_strong_station_in_a_wide_filter_prints_nothing(self):
        # The pitch 600 Hz below EW4DW (30 dB over the noise on 30 m)
        # inside a 1500 Hz filter: his key clicks were read as "EEE EEI".
        text, _ = decode_fixture("beside-30m", width=1500.0)
        self.assertEqual(text.strip(), "")

    def test_the_station_itself_is_read(self):
        text, events = decode_fixture("ew4dw-30m")
        self.assertIn("CQ CQ", text)
        self.assertIn("EW4DW", text.replace(" ", ""))
        # RBN's skimmers measured him at 21 WPM.
        self.assertAlmostEqual(np.median([e["wpm"] for e in events if e.get("wpm")]), 21.0, delta=3.0)

    def test_a_strong_beacon_is_read_from_its_first_letter(self):
        # OH2B, 20 dB over the noise; the clip starts half a second before
        # its slot, so a lock on the noise before it would print first.
        text, events = decode_fixture("oh2b-20m")
        self.assertEqual(text.strip(), "OH2B")
        self.assertAlmostEqual(events[0]["wpm"], 22.0, delta=2.5)   # NCDXF beacons send at 22

    def test_a_beacon_3_db_over_the_noise_is_read(self):
        # 4X6TU, 3 dB over the noise in 2.5 kHz: the squelch must open for
        # it. (Its 1 W dash, 20 dB down, is the "E" after it.)
        text, _ = decode_fixture("4x6tu-20m")
        self.assertIn("4X6TU", text)

    def test_a_higher_squelch_silences_a_weak_signal(self):
        text, _ = decode_fixture("4x6tu-20m", snr_db=30.0)
        self.assertEqual(text.strip(), "")


class BenchCasesTests(unittest.TestCase):
    """The bench cases the corpus added (shack_cw.bench), one seed each."""

    def test_static_prints_nothing(self):
        for snr in (0.0, 9.0, 15.0):
            with self.subTest(snr=snr):
                cpm, _, _, out = bench.run_case("qrn", snr, 0)
                self.assertEqual(out.strip(), "")

    def test_a_keyed_neighbour_200_hz_away_prints_nothing(self):
        # The decoder as shipped printed 100-160 characters a minute here,
        # and on some seeds recursed until Python stopped it.
        for snr in (0.0, 9.0):
            with self.subTest(snr=snr):
                cpm, _, _, out = bench.run_case("neighbour-200", snr, 0)
                self.assertEqual(out.strip(), "")

    def test_a_station_600_hz_away_in_a_wide_filter_prints_nothing(self):
        cpm, _, _, out = bench.run_case("neighbour-wide", 15.0, 0)
        self.assertEqual(out.strip(), "")

    def test_light_keying_in_noise_is_not_read_inside_out(self):
        # Seed 1 at 6 dB turned the polarity over mid-over and printed
        # "IO75R ITIAEITIAEITIMEAE5T..." for the rest of it.
        e, _, _, out = bench.run_case("cq20-light", 6.0, 1)
        self.assertLess(e, 0.1, out)

    def test_polarity_off_never_turns_keying_over(self):
        # QO-100's beacon at the old bookmark is heard inside out: turned
        # over by default (NB), left as heard with polarity=False (Radio,
        # where a beacon's fading dashes turned real keying over).
        _, _, _, as_nb = bench.run_case("beacon-bookmark", 12.0, 0)
        self.assertIn("QO100", as_nb.replace(" ", ""))
        _, _, _, as_radio = bench.run_case("beacon-bookmark", 12.0, 0,
                                           lambda emit, freq: Decoder(emit, freq=freq, polarity=False))
        self.assertNotIn("QO100", as_radio.replace(" ", ""))

    def test_noise_alone_shows_no_speed(self):
        dec = Decoder(freq=700.0)
        rng = np.random.default_rng(3)
        for _ in range(200):
            dec.feed(rng.normal(0, 0.1, 900).astype(np.float32), 12_000)
        self.assertEqual(dec.text, "")
        self.assertIsNone(dec.wpm)
        self.assertFalse(dec.hearing)


class FrameApiTests(unittest.TestCase):
    """Decoder.feed_frames: the decoder without its front end, one per
    channel of a skimmer that makes every channel's amplitudes itself."""

    @staticmethod
    def frames(text: str, wpm: float, level: float, seed: int = 1):
        rng = np.random.default_rng(seed)
        key = bench.key_envelope(bench.runs_for(text, wpm) + [(0, 1.5)], edge=0.015, rate=200)
        n = len(key)
        return level * key + np.abs(rng.normal(1, 0.3, n)), np.abs(rng.normal(1, 0.3, n))

    def test_amplitudes_in_text_speed_and_snr_out(self):
        tone, quiet = self.frames("CQ CQ DE GM4BRI GM4BRI K", 20, 4.0)
        dec = Decoder(freq=700.0)
        events = []
        for i in range(0, len(tone), 15):   # 75 ms blocks of 5 ms frames
            events += dec.feed_frames(tone[i:i + 15], quiet[i:i + 15])
        self.assertIn("GM4BRI GM4BRI", "".join(e["text"] for e in events))
        self.assertTrue(dec.locked)
        self.assertAlmostEqual(dec.wpm, 20.0, delta=2.0)

    def test_noise_frames_print_nothing_and_say_so(self):
        tone, quiet = self.frames("", 20, 0.0)
        tone, quiet = np.tile(tone, 40), np.tile(quiet, 40)
        dec = Decoder(freq=700.0)
        events = dec.feed_frames(tone, quiet)
        self.assertEqual(events, [])
        self.assertFalse(dec.hearing)
        self.assertLess(dec.snr, 6.0)


if __name__ == "__main__":
    unittest.main()
