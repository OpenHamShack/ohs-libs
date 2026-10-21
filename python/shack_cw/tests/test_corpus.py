"""shack_cw.corpus, the scoring of recordings: the beacon schedule, the
slot annotation, the file formats and the scores, on IQ made here."""
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from shack_cw import bench, corpus

FS = 12_000


def keyed(text: str, wpm: float, seconds: float, at: float, snr_db: float, hz: float = 0.0, seed: int = 0,
          dashes: int = 0):
    """`seconds` of IQ: noise, and `text` keyed at `hz` from `at` s, `snr_db`
    over the noise in 2.5 kHz (the bench's convention); then `dashes` 1 s
    dashes, as an NCDXF beacon sends after its call."""
    rng = np.random.default_rng(seed)
    runs = [(0, at)] + bench.runs_for(text, wpm) + [(0, 0.3), (1, 1.0)] * dashes
    runs.append((0, max(0.0, seconds - sum(d for _, d in runs))))
    return bench.render(runs, rng, snr_db, mark_hz=hz, lead=0.0, tail=0.0)[:int(seconds * FS)]


class ScheduleTests(unittest.TestCase):
    def test_whose_turn_it_is(self):
        cycle = 1800.0 * 1000   # a multiple of the 3-minute cycle
        self.assertEqual(corpus.ncdxf_call(14100.0, cycle + 1), "4U1UN")
        self.assertEqual(corpus.ncdxf_call(14100.0, cycle + 131), "OH2B")
        # One slot later on each band up: 4U1UN is on 18110 at 00:10.
        self.assertEqual(corpus.ncdxf_call(18110.0, cycle + 11), "4U1UN")
        self.assertEqual(corpus.ncdxf_call(18110.0, cycle + 1), "YV5B")


class SlotTests(unittest.TestCase):
    def test_a_heard_beacon_and_empty_slots(self):
        # Four slots on 14100, the recording asked for 2 s before the top
        # of a cycle and its first sample 1.2 s late; W6WX (slot 2) heard
        # 10 dB over the noise, nothing else. The grid is found from the
        # keying: slot 2 starts 20.8 s into the file, not at the clock's 22.
        z = keyed("W6WX", 22, 38.0, at=20.8, snr_db=10.0, dashes=4)
        t = corpus.beacon_slots(z, FS, 14100.0, 1800.0 * 1000 - 2.0)
        self.assertAlmostEqual(t["t0"], 0.8, delta=0.15)
        self.assertEqual(t["slots"][:3], [None, None, "W6WX"])
        self.assertGreater(t["snr"]["2"], 3.0)


class FormatTests(unittest.TestCase):
    def test_kiwi_wav_and_fixture_round_trip(self):
        # A KiwiSDR IQ WAV (16-bit stereo, I left) with a tone 300 Hz up;
        # cut as a fixture round it, the tone comes back at 0 Hz.
        n = 2 * FS
        tone = 0.3 * np.exp(2j * np.pi * 300.0 * np.arange(n) / FS)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "iq").mkdir()
            with wave.open(str(base / "iq" / "t.wav"), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(FS)
                q = np.stack([tone.real, tone.imag], axis=1)
                w.writeframes((q * 32767).astype("<i2").tobytes())
            z, rate = corpus.load_iq(base / "iq" / "t.wav")
            self.assertEqual(rate, FS)
            self.assertAlmostEqual(float(np.abs(z[100:]).mean()), 0.3, places=3)
            import importlib.util
            tool = Path(__file__).resolve().parent.parent / "tools" / "cw_corpus.py"
            spec = importlib.util.spec_from_file_location("cw_corpus", tool)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.fixture(base, {"file": "iq/t.wav"}, 300.0, 0.0, 2.0, base / "f.npz")
            y, rate = corpus.load_iq(base / "f.npz")
        self.assertEqual(rate, FS)
        f = np.fft.fftfreq(len(y), 1 / FS)[np.argmax(np.abs(np.fft.fft(y)))]
        self.assertLess(abs(f), 2.0)


class ScoreTests(unittest.TestCase):
    def test_text_and_noise_targets(self):
        z = keyed("CQ DE GM4BRI GM4BRI K", 20, 14.0, at=1.0, snr_db=12.0, hz=500.0)
        rec = {"id": "t", "band": "20m", "kind": "activity"}
        text = corpus.score_target(z, FS, rec, {"offset": 500.0, "text": "CQ DE GM4BRI GM4BRI K", "wpm": 20})[0]
        self.assertEqual(text["kind"], "text")
        self.assertLess(text["cer"], 0.1)
        self.assertAlmostEqual(text["wpm"], 20.0, delta=2.0)
        self.assertLess(text["lock"], 5.0)
        quiet = corpus.score_target(z, FS, rec, {"offset": -2000.0, "noise": True})[0]
        self.assertEqual(quiet["kind"], "noise")
        self.assertEqual(quiet["cpm"], 0.0)

    def test_summary_pools_by_band_and_category(self):
        rows = [{"band": "20m", "category": "strong", "kind": "text", "expect": "AB", "cer": 0.5,
                 "wpm": 21.0, "wpm_true": 20, "lock": 2.0, "secs": 10.0},
                {"band": "20m", "category": "noise", "kind": "noise", "cpm": 6.0, "secs": 60.0,
                 "wpm_shown": 30.0}]
        s = corpus.summary(rows)
        self.assertAlmostEqual(s["band"]["20m"]["cer"], 0.5)
        self.assertAlmostEqual(s["band"]["20m"]["cpm"], 6.0)
        self.assertEqual(s["category"]["noise"]["wpm_shown_on_noise"], 1)
        self.assertAlmostEqual(s["category"]["strong"]["wpm_err"], 1.0)


if __name__ == "__main__":
    unittest.main()
