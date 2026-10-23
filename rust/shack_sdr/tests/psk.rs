//! The PSK decoder against the golden numbers captured from
//! openhamshack-nb's Python decoder (testdata/psk, tools/psk_golden.py).
//!
//! Every fixture is regenerated from its recipe and checked byte for byte
//! (sha256) before it is decoded, so the numbers compared are on exactly
//! the samples psk.py saw. Each is decoded as Radio's engine feeds a mode
//! decoder - 12 kHz audio in 0.1 s blocks, the carrier near 1000 Hz -
//! through the same audio-to-IQ adapter the host uses.
//!
//! "Never worse" is judged per point of the sweep (group, speed, channel,
//! SNR), summing edits over its seeds: one fixture's luck either way is not
//! a regression. `PSK_SHOW=1` prints every fixture's text and the decoder's.

use serde_json::Value as J;
use sha2::{Digest, Sha256};
use shack_sdr::decoder::{Decoder, Input, Output, Params, Sink, Value};
use shack_sdr::dsp::AudioToIq;
use shack_sdr::psk::tx::{psk_audio, psk_iq, Interferer, PskCase, PskTx};
use shack_sdr::psk::varicode::truth;
use shack_sdr::psk::Psk;
use shack_sdr::testsig::{levenshtein, Watterson};
use std::collections::BTreeMap;

const DATA: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/psk");

fn load(name: &str) -> J {
    serde_json::from_str(&std::fs::read_to_string(format!("{DATA}/{name}")).unwrap()).unwrap()
}

fn case_of(c: &J, rate: f64, centre: f64) -> PskCase {
    let f = |k: &str| c[k].as_f64().unwrap();
    PskCase {
        text: c["text"].as_str().unwrap().to_string(),
        tx: PskTx { baud: f("baud"), clock_ppm: f("clock_ppm"), ..Default::default() },
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
        interferer: c["interferer"].as_array().map(|i| Interferer {
            offset_hz: i[0].as_f64().unwrap(),
            level_db: i[1].as_f64().unwrap(),
            baud: i[2].as_f64().unwrap(),
            text: i[3].as_str().unwrap().to_string(),
        }),
        lead_s: f("lead_s"),
        tail_s: f("tail_s"),
        seed: c["seed"].as_u64().unwrap(),
    }
}

fn text(sink: &Sink) -> String {
    sink.items
        .iter()
        .filter_map(|(_, o)| match o {
            Output::Text { text, .. } => Some(text.as_str()),
            _ => None,
        })
        .collect()
}

/// As the engine feeds the mode decoder: audio in 0.1 s blocks.
fn decode_audio(audio: &[f32], params: Params, centre: f64, block: usize) -> Sink {
    let rate = 12_000.0;
    let mut adapter = AudioToIq::new(centre, rate);
    let mut d = Psk::new();
    d.configure(&params).unwrap();
    let mut sink = Sink::new();
    for (k, c) in audio.chunks(block).enumerate() {
        let mut iq = Vec::with_capacity(c.len());
        adapter.process(c, &mut iq);
        let input = Input::Iq { samples: &iq, rate, t0: (k * block) as f64 / rate, index: (k * block) as u64 };
        d.feed(input, &mut sink).unwrap();
    }
    sink
}

fn baud(b: f64) -> Params {
    Params::new().with("baud", Value::Num(b))
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
    let mine: Vec<&J> =
        cases["cases"].as_array().unwrap().iter().filter(|c| groups.contains(&c["group"].as_str().unwrap())).collect();
    // The fixtures are independent: decode them on every core.
    let results: Vec<(String, usize, usize, usize)> = std::thread::scope(|s| {
        let chunks: Vec<_> = mine.chunks(mine.len().div_ceil(8).max(1)).collect();
        let handles: Vec<_> = chunks
            .into_iter()
            .map(|chunk| {
                let python = &python;
                s.spawn(move || {
                    chunk
                        .iter()
                        .map(|c| {
                            let case = case_of(c, rate, centre);
                            let audio = psk_audio(&case);
                            let bytes: Vec<u8> = audio.iter().flat_map(|x| x.to_le_bytes()).collect();
                            let digest: String = Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
                            assert_eq!(digest, c["sha256"].as_str().unwrap(), "fixture {} is not the one psk.py saw", c["id"]);
                            let want = truth(&case.text);
                            let got = text(&decode_audio(&audio, baud(case.tx.baud), centre, block));
                            let i = &c["interferer"];
                            let key = format!(
                                "{:<10} {:>6}Bd {:<8} off {:+5.1} {:<13} snr {:+5.1}",
                                c["group"].as_str().unwrap(),
                                case.tx.baud,
                                c["fading"].as_str().unwrap_or("-"),
                                c["offset_hz"].as_f64().unwrap(),
                                if i.is_array() { format!("qrm {:+.0}@{:+.0}dB", i[0].as_f64().unwrap(), i[1].as_f64().unwrap()) } else { String::new() },
                                case.snr_db.unwrap_or(f64::NAN)
                            );
                            if std::env::var("PSK_SHOW").is_ok() {
                                eprintln!("{} {key}\n   want {want:?}\n   got  {got:?}", c["id"]);
                            }
                            (key, levenshtein(&got, &want), python[c["id"].as_str().unwrap()], want.chars().count())
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        handles.into_iter().flat_map(|h| h.join().unwrap()).collect()
    });
    for (key, r, p, n) in results {
        let e = points.entry(key).or_insert(Point { edits_rust: 0, edits_python: 0, chars: 0 });
        e.edits_rust += r;
        e.edits_python += p;
        e.chars += n;
    }
    points
}

fn report(points: &BTreeMap<String, Point>) {
    for (k, p) in points {
        let n = p.chars.max(1) as f64;
        println!(
            "{k}  rust {:>6.3}  python {:>6.3}  ({} / {} of {})",
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
    assert!(worse.is_empty(), "worse than psk.py at {worse:?}");
}

#[test]
fn awgn_never_worse() {
    let points = sweep(&["awgn"]);
    report(&points);
    never_worse(&points);
}

#[test]
fn off_tune_drifting_and_clock_never_worse() {
    let points = sweep(&["offset", "drift", "clock"]);
    report(&points);
    never_worse(&points);
}

#[test]
fn fading_never_worse() {
    let points = sweep(&["fading"]);
    report(&points);
    never_worse(&points);
}

#[test]
fn a_station_alongside_never_worse() {
    let points = sweep(&["interferer"]);
    report(&points);
    never_worse(&points);
}

#[test]
fn noise_prints_almost_nothing() {
    let p = sweep(&["noise"]);
    report(&p);
    let rust: usize = p.values().map(|p| p.edits_rust).sum();
    let python: usize = p.values().map(|p| p.edits_python).sum();
    println!("noise: rust {rust} false characters in fifteen minutes, python {python}");
    assert!(rust <= 15, "{rust}");
}
