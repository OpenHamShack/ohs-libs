"""The CW decoder: loopback, the on-air cases of the bench, and any rate.

The loopback and on-air tests moved from QO-100 NB with the decoder
(openhamshack-nb tests/test_modes_cw.py, main 47be138) and hold it to the same
numbers; NB's encoder tests stayed there with the encoder. The rate tests
are new: NB only ever fed 12 kHz, Icom feeds 12 kHz from the radio's LAN
stream and Flex 24 kHz.
"""
import unittest

import numpy as np

from shack_cw import Decoder, bench

FS = 12_000.0


def tone(text: str, wpm: float, freq: float = 700.0, rate: float = FS, tail: float = 0.5) -> np.ndarray:
    """Clean keyed tone for `text` (prosigns as <XX>), then `tail` s of silence."""
    env = bench.key_envelope(bench.runs_for(text, wpm) + [(0, tail)], rate=rate)
    return (env * np.sin(2 * np.pi * freq * np.arange(len(env)) / rate)).astype(np.float32)


def _decode(audio: np.ndarray, freq: float = 700.0, wpm_hint: float = 20.0, rate: float = FS,
            block_s: float = 0.1) -> str:
    events = []
    dec = Decoder(events.append, freq=freq, wpm_hint=wpm_hint)
    block = int(block_s * rate)  # 100 ms blocks: exercises feed() being called with a real block size
    for i in range(0, len(audio), block):
        dec.feed(audio[i:i + block], rate)
    return "".join(e["text"] for e in events)


class CwLoopbackTests(unittest.TestCase):
    MESSAGE = "CQ CQ DE GM4BRI GM4BRI <AR>"

    def test_clean_loopback_12_20_30_wpm(self):
        for wpm in (12, 20, 30):
            with self.subTest(wpm=wpm):
                text = _decode(tone(self.MESSAGE, wpm))
                self.assertIn("GM4BRI", text)
                self.assertIn("CQ", text)
                self.assertIn("DE", text)

    def test_noisy_loopback_20_wpm(self):
        rng = np.random.default_rng(42)
        audio = tone(self.MESSAGE, 20)
        noisy = audio + rng.normal(0, 0.1, len(audio)).astype(np.float32)
        text = _decode(noisy)
        # The callsign, sent twice, must survive this (roughly 20 dB) SNR intact.
        self.assertEqual(text.count("GM4BRI"), 2)
        self.assertIn("CQ", text)
        self.assertIn("DE", text)

    def test_prosign_round_trip(self):
        # A second of silence after it, not half: six elements are too few
        # to learn a speed from, so a lone prosign is decoded once the
        # silence after it is longer than any word space could be (four
        # dashes' worth, 0.8 s at 18 WPM) - see Decoder._check_gap.
        self.assertIn("<SK>", _decode(tone("<SK>", 18, tail=1.0)))

    def test_different_tone_offset(self):
        self.assertIn("GM4BRI", _decode(tone(self.MESSAGE, 20, freq=900.0), freq=900.0))

    def test_pure_noise_decodes_nothing_useful(self):
        # 2 s of noise must not produce as much text as 2 s of real CQing does.
        lengths = []
        for seed in range(5):
            rng = np.random.default_rng(seed)
            noise = rng.normal(0, 0.2, int(2.0 * FS)).astype(np.float32)
            lengths.append(len(_decode(noise, freq=700.0)))
        self.assertLess(sum(lengths) / len(lengths), 15)

    def test_reset_clears_state(self):
        dec = Decoder(freq=700.0)
        dec.feed(tone("E", 20), FS)
        dec.reset()
        self.assertEqual(dec.text, "")


class CwApiTests(unittest.TestCase):
    """What an app sees: events back from feed(), the speed, the pitch."""

    def test_feed_returns_the_events_it_emits_in_nbs_shape(self):
        emitted = []
        dec = Decoder(emitted.append, freq=600.0)
        audio = tone("CQ CQ DE GM4BRI GM4BRI K", 18, freq=600.0)
        returned = []
        for i in range(0, len(audio), 1200):
            returned += dec.feed(audio[i:i + 1200], 12_000)
        self.assertEqual(returned, emitted)
        self.assertIn("GM4BRI", "".join(e["text"] for e in returned))
        letter = next(e for e in returned if e["text"] != " ")
        self.assertEqual(set(letter), {"mode", "text", "freq", "wpm"})
        self.assertEqual((letter["mode"], letter["freq"]), ("cw", 600.0))
        space = next(e for e in returned if e["text"] == " ")
        self.assertNotIn("wpm", space)

    def test_without_emit_the_events_come_back_only(self):
        dec = Decoder(freq=700.0)
        out = []
        audio = tone("CQ CQ DE GM4BRI GM4BRI K", 20)
        for i in range(0, len(audio), 900):
            out += dec.feed(audio[i:i + 900], 12_000)
        self.assertIn("GM4BRI", "".join(e["text"] for e in out))

    def test_speed_pitch_and_lock_are_read_from_the_signal(self):
        dec = Decoder(freq=700.0)
        self.assertIsNone(dec.wpm)
        self.assertFalse(dec.locked)
        audio = tone("CQ CQ DE GM4BRI GM4BRI K", 15, freq=730.0)   # 30 Hz off the pitch
        for i in range(0, len(audio), 900):
            dec.feed(audio[i:i + 900], 12_000)
        self.assertTrue(dec.locked)
        self.assertAlmostEqual(dec.wpm, 15.0, delta=1.5)
        self.assertAlmostEqual(dec.pitch, 730.0, delta=5.0)

    def test_a_new_rate_starts_afresh(self):
        dec = Decoder(freq=700.0)
        dec.feed(tone("CQ CQ DE GM4BRI", 20), 12_000)
        self.assertTrue(dec.text)
        dec.feed(np.zeros(4800, np.float32), 48_000)
        self.assertEqual((dec.text, dec.rate, dec.frame_len), ("", 48_000.0, 240))


class CwRateTests(unittest.TestCase):
    """The decoder at the rates the apps have: nothing in it is 12 kHz."""

    def test_clean_and_noisy_cq_at_every_rate(self):
        rng = np.random.default_rng(7)
        for rate in (8_000.0, 12_000.0, 24_000.0, 48_000.0):
            with self.subTest(rate=rate):
                audio = tone("CQ CQ DE GM4BRI GM4BRI <AR>", 20, rate=rate)
                # Noise of the same density at each rate: the same SNR in
                # the decoder's narrow filter.
                noisy = audio + rng.normal(0, 0.1 * np.sqrt(rate / FS), len(audio)).astype(np.float32)
                text = _decode(noisy, rate=rate)
                self.assertEqual(text.count("GM4BRI"), 2, text)

    def test_the_beacon_through_a_receiver_at_flexs_rate(self):
        # The bench's beacon case, rendered and received at 24 kHz.
        for seed in range(2):
            with self.subTest(seed=seed):
                self.assertLess(bench.run_case("beacon", 6.0, seed, rate=24_000)[0], 0.05)

    def test_afc_bins_stay_fine_at_a_high_rate(self):
        # At 48 kHz a 2048-point spectrum has 23 Hz bins; AFC must still
        # find a tone 40 Hz off the pitch to within a few Hz.
        dec = Decoder(freq=700.0)
        audio = tone("TEST DE GM4BRI GM4BRI TEST", 18, freq=740.0, rate=48_000.0)
        for i in range(0, len(audio), 3600):
            dec.feed(audio[i:i + 3600], 48_000)
        self.assertAlmostEqual(dec.pitch, 740.0, delta=4.0)


class CwOnAirTests(unittest.TestCase):
    """The conditions QO-100's lower beacon and its CW stations are heard in."""

    def cer(self, case: str, snr: float, seed: int) -> float:
        return bench.run_case(case, snr, seed)[0]

    def test_the_lower_beacon_at_6_db_is_read_with_under_5_percent_errors(self):
        # The beacon on Brian's TV was 10.8 dB and read as strings of
        # unknowns; 6 dB in 2.5 kHz is a modest dish.
        for seed in range(3):
            with self.subTest(seed=seed):
                self.assertLess(self.cer("beacon", 6.0, seed), 0.05)

    def test_the_beacons_12_s_carrier_is_not_a_letter_and_spoils_none(self):
        # Every cycle ends in 12 s of key-down. It must print nothing, and
        # the "HI" straight after it must still be HI: the first version
        # learnt its speed from the carrier and garbled the next word.
        _, _, _, out = bench.run_case("beacon", 20.0, 0)
        self.assertEqual(" ".join(out.split()), " ".join([bench.BEACON_TEXT] * 2))

    def test_qsb_and_drift(self):
        # A 12 dB fade every 6-10 s, at 9 dB; and the tone wandering
        # +-25 Hz at 6 dB, which the decoder's AFC has to follow.
        self.assertLess(self.cer("beacon-qsb", 9.0, 0), 0.05)
        self.assertLess(self.cer("beacon-drift", 6.0, 0), 0.05)

    def test_a_human_fist_at_20_and_12_wpm(self):
        # Elements and gaps off by ~12 %, the sender's own dash ratio, letter
        # gaps of 2.2-4 dots: the thresholds follow the sender.
        for case in ("cq20-human", "cq12-human"):
            with self.subTest(case=case):
                self.assertLess(self.cer(case, 6.0, 1), 0.05)

    def test_a_tone_off_the_pitch_is_found(self):
        # A station tuned 80 Hz off by ear: AFC moves the narrow filter onto it.
        rng = np.random.default_rng(3)
        runs = bench.runs_for("CQ CQ DE GM4BRI GM4BRI K", 20)
        z = bench.render(runs, rng, 9.0, mark_hz=bench.PITCH + 80.0)
        out, _, _ = bench.decode(z)
        self.assertIn("GM4BRI GM4BRI", out)

    def test_noise_and_unkeyed_carriers_print_nothing(self):
        # A minute of the receiver's noise, then the same with three steady
        # carriers in the filter (one inside AFC's reach): not a character.
        for seed in range(2):
            with self.subTest(seed=seed):
                self.assertEqual(bench.run_case("noise", 0.0, seed)[3].strip(), "")
                self.assertEqual(bench.run_case("carriers", 6.0, seed)[3].strip(), "")

    def test_the_beacon_at_10_db_and_off_the_pitch_beside_a_carrier(self):
        # On the pitch at 10 dB; then 60 Hz off it, at 6 dB, with as strong
        # a carrier 150 Hz the other side - the upper beacon's
        # neighbourhood on Brian's TV. (Stronger than about 10 dB, a
        # neighbour that close still costs letters: see the bench.)
        for case, snr in (("beacon", 10.0), ("beacon-beside", 6.0)):
            for seed in range(2):
                with self.subTest(case=case, seed=seed):
                    self.assertLess(self.cer(case, snr, seed), 0.05)

    def test_afc_is_not_pulled_onto_a_carrier_outside_its_reach(self):
        # A carrier 120 Hz below the pitch, nothing else: the edge of AFC's
        # +-100 Hz window is the carrier's skirt, not a line, and AFC moved
        # the filter 100 Hz towards it with no wanted tone there at all.
        rng = np.random.default_rng(5)
        z = bench.render([(0, 5.0)], rng, 0.0, lead=0.0, tail=0.0)
        z = z + bench.carrier(len(z), bench.PITCH - 120, 15.0)
        dec = Decoder(freq=bench.PITCH)
        for a in bench.demodulate(z):
            dec.feed(a, bench.FS)
        self.assertLess(abs(dec.pitch - bench.PITCH), 10.0)

    def test_a_beacon_keyed_inside_out_is_read_the_right_way_round(self):
        # The old bookmarks put the F1A beacons' key-up tone on the pitch:
        # the decoder heard tone in every gap and printed "<SK>UTEITEENEEET
        # EEEETEEEE..." (Brian's TV, 10/10/2026, letter for letter). It now
        # turns the keying over and reads the text, from the first line.
        for snr in (3.0, 10.0):
            with self.subTest(snr=snr):
                out = bench.run_case("beacon-bookmark", snr, 0)[3]
                self.assertLess(bench.cer(out, " ".join([bench.BEACON_TEXT] * 2)), 0.05)
                self.assertTrue(out.startswith("HI DE") or out.startswith("E HI DE"), out)

    def test_real_keying_is_never_turned_over(self):
        # Fades and a human fist at low SNR merge dashes into long marks
        # now and then; the word spaces of the same signal must outvote them.
        seen = []

        class Watched(Decoder):
            def _polarity(self, tone, dur):
                super()._polarity(tone, dur)
                seen.append(self._inverted)

        for case, snr in (("cq20-human-qsb-drift", 3.0), ("beacon-qsb", 3.0), ("cq12-human", 0.0)):
            with self.subTest(case=case):
                seen.clear()
                bench.run_case(case, snr, 0, Watched)
                self.assertTrue(seen)
                self.assertFalse(any(seen))

    def test_letters_run_together_are_split_at_the_longest_gap(self):
        # The C and Q of "CQ" sent with 1.8 dots between them instead of 3:
        # read as one letter, -.-.--.- is no letter at all; split where the
        # gap was longest, it is C and Q.
        dit = 1.2 / 20
        c, q = bench.runs_for("C", 20), bench.runs_for("Q", 20)
        word = c + [(0, 1.8 * dit)] + q
        runs = bench.runs_for("DE GM4BRI GM4BRI", 20) + [(0, 7 * dit)]
        for _ in range(3):
            runs += word + [(0, 7 * dit)]
        out, _, _ = bench.decode(bench.render(runs, np.random.default_rng(4), 20.0))
        self.assertEqual(out.split()[-3:], ["CQ", "CQ", "CQ"])


if __name__ == "__main__":
    unittest.main()
