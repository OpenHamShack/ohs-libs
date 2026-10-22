//! The RTTY decoder against the golden numbers captured from
//! openhamshack-nb's Python decoder (testdata/rtty, tools/rtty_golden.py),
//! and against RADIO-SDR-PLAN §7.2's targets.
//!
//! Every fixture is regenerated from its recipe and checked byte for byte
//! (sha256) before it is decoded, so the numbers compared are on exactly
//! the samples rtty.py saw. Each is decoded as Radio's engine feeds a mode
//! decoder - 12 kHz audio in 0.1 s blocks, the centre at 1000 Hz - through
//! the same audio-to-IQ adapter the host uses.
//!
//! "Never worse" is judged per point of the sweep (group, speed, shift,
//! offset, fading, SNR), summing edits over its seeds: one fixture's luck
//! either way is not a regression.

use serde_json::Value as J;
use sha2::{Digest, Sha256};
use shack_sdr::decoder::{Decoder, Input, Output, Params, Sink, Value};
use shack_sdr::dsp::AudioToIq;
use shack_sdr::rtty::Rtty;
use shack_sdr::testsig::{levenshtein, rtty_audio, rtty_iq, rtty_truth, RttyCase, RttyTx, Watterson};
use std::collections::BTreeMap;

const DATA: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/rtty");

fn load(name: &str) -> J {
    serde_json::from_str(&std::fs::read_to_string(format!("{DATA}/{name}")).unwrap()).unwrap()
}

fn case_of(c: &J, rate: f64, centre: f64) -> RttyCase {
    let f = |k: &str| c[k].as_f64().unwrap();
    RttyCase {
        text: c["text"].as_str().unwrap().to_string(),
        tx: RttyTx { baud: f("baud"), shift: f("shift"), reverse: c["reverse"].as_bool().unwrap(), ..Default::default() },
        rate,
        centre: centre + f("offset_hz"),
        drift: f("drift"),
        snr_db: c["snr_db"].as_f64(),
        fading: match c["fading"].as_str() {
            Some("moderate") => Some(Watterson::MODERATE),
            Some("poor") => Some(Watterson::POOR),
            Some("good") => Some(Watterson::GOOD),
            _ => None,
        },
        lead_s: f("lead_s"),
        tail_s: f("tail_s"),
        seed: c["seed"].as_u64().unwrap(),
    }
}

fn params(case: &RttyCase) -> Params {
    Params::new()
        .with("baud", Value::Num(case.tx.baud))
        .with("shift", Value::Num(case.tx.shift))
        .with("reverse", Value::Bool(case.tx.reverse))
}

fn text(sink: Sink) -> String {
    sink.items
        .into_iter()
        .filter_map(|(_, o)| match o {
            Output::Text { text, .. } => Some(text),
            _ => None,
        })
        .collect()
}

/// As the engine feeds the mode decoder: audio in 0.1 s blocks.
fn decode_audio(audio: &[f32], case: &RttyCase, centre: f64, block: usize) -> String {
    let mut adapter = AudioToIq::new(centre, case.rate);
    let mut d = Rtty::new();
    d.configure(&params(case)).unwrap();
    let mut sink = Sink::new();
    for (k, c) in audio.chunks(block).enumerate() {
        let mut iq = Vec::with_capacity(c.len());
        adapter.process(c, &mut iq);
        let input = Input::Iq { samples: &iq, rate: case.rate, t0: k as f64 * 0.1, index: (k * block) as u64 };
        d.feed(input, &mut sink).unwrap();
    }
    text(sink)
}

struct Point {
    edits_rust: usize,
    edits_python: usize,
    chars: usize,
}

/// Decode every fixture in `groups` and gather the edits per sweep point.
fn sweep(groups: &[&str]) -> BTreeMap<String, Point> {
    let cases = load("cases.json");
    let golden = load("golden.json");
    let rate = cases["rate"].as_f64().unwrap();
    let centre = cases["centre"].as_f64().unwrap();
    let block = cases["block"].as_u64().unwrap() as usize;
    let python: BTreeMap<&str, usize> = golden["results"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| (r["id"].as_str().unwrap(), r["edits"].as_u64().unwrap() as usize))
        .collect();
    let mut points: BTreeMap<String, Point> = BTreeMap::new();
    for c in cases["cases"].as_array().unwrap() {
        if !groups.contains(&c["group"].as_str().unwrap()) {
            continue;
        }
        let case = case_of(c, rate, centre);
        let audio = rtty_audio(&case);
        let bytes: Vec<u8> = audio.iter().flat_map(|x| x.to_le_bytes()).collect();
        let digest: String = Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(digest, c["sha256"].as_str().unwrap(), "fixture {} is not the one rtty.py saw", c["id"]);
        let truth = rtty_truth(&case.text);
        let got = decode_audio(&audio, &case, centre, block);
        let key = format!(
            "{} {}Bd {}Hz{} {} off {:+} snr {:+}",
            c["group"].as_str().unwrap(),
            case.tx.baud,
            case.tx.shift,
            if case.tx.reverse { " rev" } else { "" },
            c["fading"].as_str().unwrap_or("-"),
            c["offset_hz"].as_f64().unwrap(),
            case.snr_db.unwrap_or(f64::NAN)
        );
        let p = points.entry(key).or_insert(Point { edits_rust: 0, edits_python: 0, chars: 0 });
        p.edits_rust += levenshtein(&got, &truth);
        p.edits_python += python[c["id"].as_str().unwrap()];
        p.chars += truth.chars().count();
    }
    points
}

fn report(points: &BTreeMap<String, Point>) {
    for (k, p) in points {
        let n = p.chars.max(1) as f64;
        println!(
            "{k:<44} rust {:>6.3}  python {:>6.3}  ({} / {} of {})",
            p.edits_rust as f64 / n,
            p.edits_python as f64 / n,
            p.edits_rust,
            p.edits_python,
            p.chars
        );
    }
}

fn never_worse(points: &BTreeMap<String, Point>) {
    let worse: Vec<&String> = points.iter().filter(|(_, p)| p.edits_rust > p.edits_python).map(|(k, _)| k).collect();
    assert!(worse.is_empty(), "worse than rtty.py at {worse:?}");
}

fn cer(points: &BTreeMap<String, Point>, key: &str) -> f64 {
    let p = &points[key];
    p.edits_rust as f64 / p.chars as f64
}

#[test]
fn awgn_45_bd_170_hz_meets_the_target_and_never_worse() {
    let all = sweep(&["awgn"]);
    let points: BTreeMap<String, Point> = all.into_iter().filter(|(k, _)| k.contains(" 45.45Bd 170Hz ")).collect();
    report(&points);
    never_worse(&points);
    // §7.2: at most 1 % at -5 dB in 2.5 kHz.
    assert!(cer(&points, "awgn 45.45Bd 170Hz - off +0 snr -5") <= 0.01);
}

/// The whole AWGN grid: every speed and shift (a few seconds).
#[test]
fn awgn_every_speed_and_shift_never_worse() {
    let points = sweep(&["awgn"]);
    report(&points);
    never_worse(&points);
}

#[test]
fn off_tune_reversed_and_drifting_never_worse() {
    let points = sweep(&["offset", "reverse", "drift"]);
    report(&points);
    never_worse(&points);
}

#[test]
fn fading_meets_the_target_and_never_worse() {
    let points = sweep(&["fading"]);
    report(&points);
    never_worse(&points);
    // §7.2: Watterson "moderate", at most 5 % at +5 dB.
    assert!(cer(&points, "fading 45.45Bd 170Hz moderate off +0 snr +5") <= 0.05);
}

#[test]
fn noise_prints_almost_nothing() {
    // Ten minutes of noise: §7.2 allows one false character a minute.
    let p = sweep(&["noise"]);
    report(&p);
    let rust: usize = p.values().map(|p| p.edits_rust).sum();
    let python: usize = p.values().map(|p| p.edits_python).sum();
    println!("noise: rust {rust} false characters in ten minutes, python {python}");
    assert!(rust <= 10, "{rust}");
}

#[test]
fn iq_in_copies_as_audio_does() {
    // The same signal as complex baseband: the decoder's native input.
    for (baud, shift, reverse) in [(45.45, 170.0, false), (50.0, 850.0, true), (75.0, 425.0, false)] {
        let case = RttyCase {
            text: "CQ CQ DE GM4BRI GM4BRI 599 0123 K\n".into(),
            tx: RttyTx { baud, shift, reverse, ..Default::default() },
            centre: 12.0, // a little off the channel centre, for the AFC
            snr_db: Some(0.0),
            seed: 5,
            ..Default::default()
        };
        let iq = rtty_iq(&case);
        let mut d = Rtty::new();
        d.configure(&params(&case)).unwrap();
        let mut sink = Sink::new();
        for (k, c) in iq.chunks(1200).enumerate() {
            d.feed(Input::Iq { samples: c, rate: 12_000.0, t0: 0.0, index: (k * 1200) as u64 }, &mut sink).unwrap();
        }
        // The SNR and offset each result was printed with: the estimate
        // during the over, not after it (the status, read at the end, has
        // the tail's second of noise in its average).
        let median = |f: &dyn Fn(&Output) -> Option<f32>| {
            let mut v: Vec<f32> = sink.items.iter().filter_map(|(_, o)| f(o)).collect();
            v.sort_by(f32::total_cmp);
            v[v.len() / 2]
        };
        let snr = median(&|o| match o {
            Output::Text { snr_db, .. } => *snr_db,
            _ => None,
        });
        let offset = median(&|o| match o {
            Output::Text { offset_hz, .. } => Some(*offset_hz),
            _ => None,
        });
        assert_eq!(text(sink), rtty_truth(&case.text), "{baud} {shift} {reverse}");
        assert!((offset - 12.0).abs() < 3.0, "AFC at {offset}");
        assert!((-2.0..2.0).contains(&snr), "SNR estimate {snr} at {baud} {shift}");
    }
}

#[test]
fn unshift_on_space_can_be_turned_off() {
    // FIGS 1 SPACE 2: with USOS the 2 after the space is read as a letter (W).
    let case = RttyCase { text: "1 2\n".into(), centre: 0.0, snr_db: Some(10.0), seed: 9, ..Default::default() };
    let iq = rtty_iq(&case);
    let decode = |usos: bool| {
        let mut d = Rtty::new();
        d.configure(&Params::new().with("usos", Value::Bool(usos))).unwrap();
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: &iq, rate: 12_000.0, t0: 0.0, index: 0 }, &mut sink).unwrap();
        text(sink)
    };
    // The encoder sent FIGS again after the space, so both read it right...
    assert_eq!(decode(true), "1 2\n");
    assert_eq!(decode(false), "1 2\n");
    // ...but codes sent without that FIGS show the difference.
    let tx = RttyTx::default();
    let codes = [0b11111, 0b11011, 0b11101, 0b00100, 0b11001, 0b00010, 0b01000];
    let segs = shack_sdr::testsig::rtty_segments(&codes, &tx);
    let x = shack_sdr::testsig::fsk(&segs, 12_000.0, 0.0, 0.0);
    let mut pad = vec![rustfft::num_complex::Complex32::new(0.0, 0.0); 6000];
    pad.extend(x.iter().map(|z| rustfft::num_complex::Complex32::new(0.5 * z.re as f32, 0.5 * z.im as f32)));
    pad.extend(vec![rustfft::num_complex::Complex32::new(0.0, 0.0); 6000]);
    let run = |usos: bool| {
        let mut d = Rtty::new();
        d.configure(&Params::new().with("usos", Value::Bool(usos)).with("squelch", Value::Num(0.0))).unwrap();
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: &pad, rate: 12_000.0, t0: 0.0, index: 0 }, &mut sink).unwrap();
        text(sink)
    };
    assert_eq!(run(true), "1 W\n");
    assert_eq!(run(false), "1 2\n");
}


// ------------------------------------------------- after the carrier drops

/// The noise after an over, as a receiver with AGC delivers it. Captured
/// from openhamshack-nb's own-echo test (Radio M1, 22/10/2026): a strong
/// over (+15 dB in 2.5 kHz) holds the AGC down, so the noise under it is
/// about a third of its level before; when the carrier drops the noise
/// comes back up over half a second or so. The decoder's averaged noise
/// estimate lags that rise, and its squelch, held open by the averaged
/// tones, printed one to three noise characters after the over in three
/// runs of five ("PSE KSWI"). rtty.py printed none on the same audio.
///
/// `before` and `under` are the noise's level, as a fraction of the
/// signal's amplitude, outside the over and under it; `recover_s` is the
/// AGC's time constant after the over.
fn over_then_noise(text: &str, seed: u64, before: f64, under: f64, recover_s: f64, tail_s: f64) -> Vec<f32> {
    let rate = 12_000.0;
    let tx = RttyTx::default();
    let segs = shack_sdr::testsig::rtty_segments(&shack_sdr::testsig::rtty_codes(text, &tx), &tx);
    let over = shack_sdr::testsig::fsk(&segs, rate, 1000.0, 0.0);
    let lead = (4.0 * rate) as usize;
    let end = lead + over.len();
    let total = end + (tail_s * rate) as usize;
    let mut noise = shack_sdr::testsig::Rng::new(seed);
    let amp = 0.5;
    (0..total)
        .map(|i| {
            let (sig, gain) = if i < lead {
                (0.0, before)
            } else if i < end {
                (over[i - lead].re, under)
            } else {
                let t = (i - end) as f64 / rate;
                // The AGC letting go: back to the old level, exponentially.
                (0.0, before - (before - under) * (-t / recover_s).exp())
            };
            (amp * sig + amp * gain * noise.gauss()) as f32
        })
        .collect()
}

/// What the decoder prints on `audio`, fed as the engine feeds it.
fn decode_plain(audio: &[f32]) -> String {
    let case = RttyCase::default();
    decode_audio(audio, &case, 1000.0, 1200)
}

#[test]
fn nothing_after_the_carrier_drops_under_agc() {
    // The echo test's shape: noise at 0.5 of the signal's amplitude, a
    // third of that under the over, back within about a second. Every seed
    // must copy the over exactly and print nothing after it: the
    // transmitter's idle mark ends the last frame cleanly, so there is no
    // partial character to allow for.
    let text = "CQ CQ CQ DE GM4BRI GM4BRI GM4BRI PSE K";
    let truth = rtty_truth(text);
    let mut junk = 0;
    let mut bad = Vec::new();
    for seed in 1..=20 {
        let got = decode_plain(&over_then_noise(text, seed, 0.5, 0.17, 0.25, 4.0));
        if got != truth {
            junk += got.strip_prefix(truth.as_str()).map_or(usize::MAX, |t| t.chars().count());
            bad.push(format!("seed {seed}: {got:?}"));
        }
    }
    println!("after the carrier dropped: {junk} characters ({bad:?})");
    assert!(bad.is_empty(), "{bad:?}");
}

#[test]
fn noise_after_strong_overs_prints_almost_nothing() {
    // Plain white noise, no AGC: a strong over, then eight seconds of the
    // same noise. The averaged spectrum remembers the over for seconds; the
    // squelch must not print the noise in that time. §7.2's budget for noise
    // is one false character a minute: here 40 overs and 320 s of noise
    // after them.
    let text = "TEST DE GM4BRI K";
    let truth = rtty_truth(text);
    let mut junk = 0;
    let mut missed = 0;
    for seed in 100..140 {
        let got = decode_plain(&over_then_noise(text, seed, 0.18, 0.18, 0.01, 8.0));
        match got.strip_prefix(truth.as_str()) {
            Some(tail) => junk += tail.chars().count(),
            None => {
                missed += 1;
                junk += levenshtein(&got, &truth);
            }
        }
    }
    println!("noise after 40 strong overs: {junk} false characters ({missed} overs not copied exactly)");
    assert!(junk <= 5, "{junk}");
}
