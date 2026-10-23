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
use rustfft::num_complex::Complex32;
use shack_sdr::psk::tx::{psk_audio, psk_iq, Interferer, PskCase, PskTx};
use shack_sdr::psk::varicode::truth;
use shack_sdr::psk::{Psk, BAUDS};
use shack_sdr::testsig::{levenshtein, Rng, Watterson};
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

fn cer(points: &BTreeMap<String, Point>, group: &str, baud: &str, snr: f64) -> f64 {
    let (_, p) = points
        .iter()
        .find(|(k, _)| k.starts_with(group) && k.contains(&format!(" {baud}Bd ")) && k.ends_with(&format!("snr {snr:+5.1}")))
        .unwrap_or_else(|| panic!("no point {group} {baud} {snr}"));
    p.edits_rust as f64 / p.chars as f64
}

#[test]
fn awgn_meets_the_floor_and_never_worse() {
    let points = sweep(&["awgn"]);
    report(&points);
    never_worse(&points);
    // What it does now, with a little room: where psk.py copied 15 %, 19 %
    // and 20 % (SNR in 2.5 kHz).
    assert!(cer(&points, "awgn", "31.25", -13.0) <= 0.05);
    assert!(cer(&points, "awgn", "62.5", -10.0) <= 0.07);
    assert!(cer(&points, "awgn", "125", -7.0) <= 0.05);
    // And clean copy a few dB up: no junk at either end of an over.
    for (b, snr) in [("31.25", -8.0), ("62.5", -7.0), ("125", -5.0)] {
        assert!(cer(&points, "awgn", b, snr) <= 0.01, "{b} {snr}");
    }
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
    let moderate = points.iter().find(|(k, _)| k.contains("31.25Bd moderate") && k.ends_with("snr  +0.0")).unwrap().1;
    assert!(moderate.edits_rust as f64 / moderate.chars as f64 <= 0.03);
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
    // RADIO-SDR-PLAN §7.2's budget is one a minute; it prints none.
    assert!(rust <= 3, "{rust}");
}

// ------------------------------------------------- beyond psk.py's sweep

fn feed_iq(d: &mut Psk, iq: &[Complex32], block: usize) -> Sink {
    let mut sink = Sink::new();
    for (k, c) in iq.chunks(block).enumerate() {
        let input = Input::Iq { samples: c, rate: 12_000.0, t0: 100.0 + (k * block) as f64 / 12_000.0, index: (k * block) as u64 };
        d.feed(input, &mut sink).unwrap();
    }
    sink
}

const OVER: &str = "CQ CQ de GM4BRI GM4BRI pse k\n";

#[test]
fn iq_in_copies_with_its_offset_and_snr() {
    for baud in BAUDS {
        let case = PskCase {
            text: OVER.into(),
            tx: PskTx { baud: *baud, ..Default::default() },
            centre: 9.0, // off the channel centre: the AFC's to find
            snr_db: Some(-5.0),
            seed: 5,
            ..Default::default()
        };
        let mut d = Psk::new();
        d.configure(&Params::new().with("baud", Value::Num(*baud))).unwrap();
        let sink = feed_iq(&mut d, &psk_iq(&case), 1200);
        assert_eq!(text(&sink), truth(OVER), "{baud}");
        let (mut offsets, mut snrs): (Vec<f32>, Vec<f32>) = sink
            .items
            .iter()
            .filter_map(|(_, o)| match o {
                Output::Text { offset_hz, snr_db, .. } => Some((*offset_hz, snr_db.unwrap())),
                _ => None,
            })
            .unzip();
        offsets.sort_by(f32::total_cmp);
        snrs.sort_by(f32::total_cmp);
        let (offset, snr) = (offsets[offsets.len() / 2], snrs[snrs.len() / 2]);
        assert!((offset - 9.0).abs() < 0.5, "{baud}: AFC at {offset}");
        assert!((snr + 5.0).abs() < 2.0, "{baud}: SNR {snr}");
        let st = d.status();
        assert_eq!(st.baud, Some(*baud as f32));
    }
}

#[test]
fn every_character_has_the_time_of_its_first_symbol() {
    let case = PskCase { text: OVER.into(), centre: 0.0, snr_db: Some(5.0), seed: 3, ..Default::default() };
    let mut d = Psk::new();
    d.configure(&Params::new()).unwrap();
    let sink = feed_iq(&mut d, &psk_iq(&case), 1000);
    // Where the transmitter put each character's first bit: after the
    // preamble and every earlier character's code and "00". Symbol k is
    // centred (k + 1) symbols after the transmission starts (tx.rs).
    let t_sym = 1.0 / 31.25;
    let mut k = 30; // a second of preamble: 31 symbols, rounded down to even
    let mut want = Vec::new();
    for c in OVER.chars() {
        want.push(100.0 + case.lead_s + (k + 1) as f64 * t_sym);
        k += shack_sdr::psk::varicode::code(c).unwrap().len() + 2;
    }
    let got: Vec<f64> = sink.items.iter().filter(|(_, o)| matches!(o, Output::Text { .. })).map(|(t, _)| *t).collect();
    assert_eq!(got.len(), want.len());
    for (g, w) in got.iter().zip(&want) {
        assert!((g - w).abs() < 0.25 * t_sym, "{g} against {w}");
    }
}

/// Real audio carrying several stations, as a receiver's passband does.
fn passband(stations: &[(f64, &str, u64)], snr_db: f64, seconds: f64) -> Vec<f32> {
    let n = (seconds * 12_000.0) as usize;
    let mut x = vec![0f32; n];
    for (f, text, seed) in stations {
        let case = PskCase { text: text.to_string(), centre: *f, seed: *seed, ..Default::default() };
        for (a, b) in x.iter_mut().zip(psk_audio(&case)) {
            *a += b;
        }
    }
    // Noise for the SNR in 2.5 kHz of one station at idle's power.
    let sigma = (0.25 * 0.5 / 2.0 / 10f64.powf(snr_db / 10.0) * 6000.0 / 2500.0).sqrt();
    let mut rng = Rng::new(77);
    x.iter_mut().for_each(|v| *v += (sigma * rng.gauss()) as f32);
    x
}

fn per_channel(sink: &Sink, offsets: &[f64]) -> Vec<String> {
    let mut out = vec![String::new(); offsets.len()];
    for (_, o) in &sink.items {
        if let Output::Text { text, offset_hz, .. } = o {
            let i = (0..offsets.len()).min_by(|a, b| (offsets[*a] - *offset_hz as f64).abs().total_cmp(&(offsets[*b] - *offset_hz as f64).abs())).unwrap();
            out[i].push_str(text);
        }
    }
    out
}

#[test]
fn several_stations_in_one_passband() {
    // A waterfall's worth: three stations in a 3 kHz passband, one of them
    // 6 Hz off where it was clicked, decoded by one session fed the audio
    // through the host's adapter centred on the passband.
    let texts = ["CQ CQ de GM4BRI pse k\n", "DL1ABC de G4XYZ 599 tu\n", "test de EI2XX qrz?\n"];
    let audio = passband(&[(700.0, texts[0], 1), (1206.0, texts[1], 2), (2140.0, texts[2], 3)], -5.0, 12.0);
    let offsets = [700.0 - 1500.0, 1200.0 - 1500.0, 2140.0 - 1500.0];
    let params = Params::new().with("channels", Value::Text("-800, -300, 640".into()));
    let sink = decode_audio(&audio, params, 1500.0, 1200);
    let got = per_channel(&sink, &offsets);
    for (g, t) in got.iter().zip(texts) {
        assert_eq!(g, &truth(t));
    }
    // The meta lists every channel, with where it is tracked to.
    let meta = sink.items.iter().rev().find_map(|(_, o)| match o {
        Output::Meta { fields } => Some(fields.clone()),
        _ => None,
    });
    let Some(Value::List(chs)) = meta.and_then(|f| f.into_iter().find(|(k, _)| k == "channels").map(|(_, v)| v)) else {
        panic!("no channel list");
    };
    assert_eq!(chs.len(), 3);
    let Value::Map(second) = &chs[1] else { panic!() };
    let get = |k: &str| second.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone()).unwrap();
    assert_eq!(get("offsetHz"), Value::Num(-300.0));
    let Value::Num(f) = get("freqHz") else { panic!() };
    assert!((f + 294.0).abs() < 1.0, "{f}");
}

#[test]
fn a_channel_added_live_leaves_the_others_alone() {
    let texts = ["CQ CQ de GM4BRI GM4BRI GM4BRI pse k\n", "DL1ABC de G4XYZ 599 tu\n"];
    let audio = passband(&[(1000.0, texts[0], 1), (1600.0, texts[1], 2)], -5.0, 16.0);
    let mut adapter = AudioToIq::new(1300.0, 12_000.0);
    let mut d = Psk::new();
    d.configure(&Params::new().with("channels", Value::Text("-300".into()))).unwrap();
    let mut sink = Sink::new();
    for (k, c) in audio.chunks(1200).enumerate() {
        if k == 5 {
            // Half a second in, before the second station starts sending.
            d.configure(&Params::new().with("channels", Value::Text("-300,300".into()))).unwrap();
        }
        let mut iq = Vec::new();
        adapter.process(c, &mut iq);
        d.feed(Input::Iq { samples: &iq, rate: 12_000.0, t0: k as f64 * 0.1, index: (k * 1200) as u64 }, &mut sink).unwrap();
    }
    let got = per_channel(&sink, &[-300.0, 300.0]);
    assert_eq!(got[0], truth(texts[0]));
    assert_eq!(got[1], truth(texts[1]));
    assert_eq!(d.status().counters.iter().find(|c| c.0 == "channels").unwrap().1, 2);
    // A list that is not one is refused, and changes nothing.
    assert!(d.configure(&Params::new().with("channels", Value::Text("300,abc".into()))).is_err());
    assert!(d.configure(&Params::new().with("channels", Value::Text(String::new()))).is_err());
}

#[test]
fn an_over_that_starts_off_tune_keeps_its_first_characters() {
    // 13 Hz off and a preamble of a third of a second: the coarse AFC needs
    // a second of signal to find it, by when the first characters have gone
    // by, decoded off-tune. The look-back decodes them again.
    let case = PskCase {
        text: OVER.into(),
        tx: PskTx { preamble_s: 0.33, ..Default::default() },
        centre: 13.0,
        snr_db: Some(-8.0),
        seed: 11,
        ..Default::default()
    };
    let mut d = Psk::new();
    d.configure(&Params::new()).unwrap();
    let sink = feed_iq(&mut d, &psk_iq(&case), 1200);
    assert_eq!(text(&sink), truth(OVER));
    let replays = d.status().counters.iter().find(|c| c.0 == "replays").unwrap().1;
    assert!(replays >= 1, "{replays}");
}

#[test]
fn noise_after_strong_overs_prints_almost_nothing() {
    // RTTY's test (tests/rtty.rs), for PSK31: forty strong overs (+5 dB in
    // 2.5 kHz, 24 dB in the symbol bandwidth), each sent with half a second
    // of idle after it and then eight seconds of the same noise. A squelch
    // that judged noise only against "a signal of the squelch's strength"
    // stayed open through a second of it after one over in ten.
    let mut junk = 0;
    let mut missed = 0;
    for seed in 100..140 {
        let case = PskCase { text: OVER.into(), centre: 0.0, snr_db: Some(5.0), tail_s: 8.0, seed, ..Default::default() };
        let mut d = Psk::new();
        d.configure(&Params::new()).unwrap();
        let got = text(&feed_iq(&mut d, &psk_iq(&case), 1200));
        match got.strip_prefix(truth(OVER).as_str()) {
            Some(tail) => junk += tail.chars().count(),
            None => {
                missed += 1;
                junk += levenshtein(&got, &truth(OVER));
            }
        }
    }
    println!("noise after 40 strong overs: {junk} false characters ({missed} overs not copied exactly)");
    assert!(missed == 0 && junk <= 4, "{junk} {missed}");
}

fn imd_of(sink: &Sink) -> Option<f64> {
    sink.items.iter().rev().find_map(|(_, o)| match o {
        Output::Meta { fields } => match &fields[0].1 {
            Value::List(chs) => match &chs[0] {
                Value::Map(m) => m.iter().find(|(k, _)| k == "imdDb").map(|(_, v)| match v {
                    Value::Num(x) => Some(*x),
                    _ => None,
                }),
                _ => None,
            },
            _ => None,
        },
        _ => None,
    })?
}

#[test]
fn an_overdriven_transmitter_shows_its_imd() {
    // fldigi's IMD: on idle, the products at 1.5 baud against the tones at
    // 0.5 baud. A clean PSK31 signal is -30 dB or better; -20 is a station
    // to tell about its audio drive.
    for want in [-28.0, -20.0, -12.0] {
        let case = PskCase {
            text: String::new(),
            tx: PskTx { preamble_s: 6.0, postamble_s: 0.0, imd_db: Some(want), ..Default::default() },
            centre: 0.0,
            snr_db: Some(15.0),
            tail_s: 0.0,
            seed: 4,
            ..Default::default()
        };
        // Idle alone: the transmitter sends nothing for no text, so send one
        // space after six seconds of idle and read the IMD before it.
        let case = PskCase { text: " ".into(), ..case };
        let mut d = Psk::new();
        d.configure(&Params::new()).unwrap();
        let iq = psk_iq(&case);
        let sink = feed_iq(&mut d, &iq[..(6.5 * 12_000.0) as usize], 1200);
        let got = imd_of(&sink).unwrap_or_else(|| panic!("no IMD at {want}"));
        assert!((got - want).abs() < 1.5, "IMD {got} for {want}");
    }
    // Clean: below anything the noise lets it measure, so none is claimed.
    let case = PskCase { text: " ".into(), tx: PskTx { preamble_s: 6.0, ..Default::default() }, centre: 0.0, snr_db: Some(15.0), ..Default::default() };
    let mut d = Psk::new();
    d.configure(&Params::new()).unwrap();
    let iq = psk_iq(&case);
    let sink = feed_iq(&mut d, &iq[..(6.5 * 12_000.0) as usize], 1200);
    assert!(imd_of(&sink).is_none_or(|x| x < -30.0), "{:?}", imd_of(&sink));
}

#[test]
fn cpu_per_channel_on_this_host() {
    // Eight stations in one passband, a session of eight channels fed as
    // the engine feeds it (12 kHz audio through the host's adapter, which
    // all the channels share). Printed every run; the assert is a regression
    // catcher (wfm_scan.rs's rule: 3x the figure measured here unless
    // SHACK_SDR_CPU_BUDGET_SCALE says otherwise).
    let scale = std::env::var("SHACK_SDR_CPU_BUDGET_SCALE").ok().and_then(|v| v.trim().parse::<f64>().ok()).filter(|s| *s > 0.0).unwrap_or(3.0);
    for (baud, spacing, measured) in [(31.25, 270.0, PSK31_SHARE), (62.5, 270.0, PSK63_SHARE), (125.0, 270.0, PSK125_SHARE)] {
        let freqs: Vec<f64> = (0..8).map(|k| 500.0 + spacing * k as f64).collect();
        let texts: Vec<String> = (0..8).map(|k| format!("station {k} de GM4BRI pse k\n")).collect();
        let n = 12_000 * 20;
        let mut audio = vec![0f32; n];
        for (k, (f, t)) in freqs.iter().zip(&texts).enumerate() {
            // Each starting when it likes, as stations do (all at once, each
            // one's first symbols' key click reaches the next channel's
            // first bits at 29 dB).
            let lead_s = 1.0 + 0.37 * k as f64;
            let case = PskCase { text: t.clone(), tx: PskTx { baud, ..Default::default() }, centre: *f, seed: k as u64 + 1, lead_s, ..Default::default() };
            for (a, b) in audio.iter_mut().zip(psk_audio(&case)) {
                *a += b;
            }
        }
        let mut rng = Rng::new(5);
        audio.iter_mut().for_each(|v| *v += (0.02 * rng.gauss()) as f32);
        let list: Vec<String> = freqs.iter().map(|f| format!("{}", f - 1500.0)).collect();
        let params = Params::new().with("baud", Value::Num(baud)).with("channels", Value::Text(list.join(",")));
        let t = shack_sdr::cpu::thread_time();
        let sink = decode_audio(&audio, params, 1500.0, 1200);
        let cpu = (shack_sdr::cpu::thread_time() - t).as_secs_f64();
        let got = per_channel(&sink, &freqs.iter().map(|f| f - 1500.0).collect::<Vec<_>>());
        // Every station copied whole. At 29 dB a carrier's first symbol,
        // easing up from nothing, reads as one more symbol of the same
        // phase - a 1 before the idle's 0s, a space - and the squelch,
        // rightly, cannot place the over's start closer than that: allow it.
        for (g, t) in got.iter().zip(&texts) {
            let at = g.find(&truth(t)).unwrap_or_else(|| panic!("{baud}: {g:?}"));
            assert!(g.chars().count() - truth(t).chars().count() <= 1 && g[..at].trim().is_empty(), "{baud}: {g:?}");
        }
        let share = cpu / (n as f64 / 12_000.0) / 8.0;
        println!("{baud} Bd: {:.3} % of a core per channel", 100.0 * share);
        assert!(share < measured * scale, "{baud}: {share}");
    }
}

/// A channel's share of one core, measured on shack (AMD Ryzen 9 6900HX),
/// with the host's audio adapter shared by eight channels.
const PSK31_SHARE: f64 = 0.00035;
const PSK63_SHARE: f64 = 0.00045;
const PSK125_SHARE: f64 = 0.00075;

// ------------------------------------------------------ the end of an over

/// Feed in 0.1 s blocks, noting after which input sample each character
/// came out (not its time stamp, which is its first symbol's).
fn feed_timed(d: &mut Psk, iq: &[Complex32]) -> Vec<(char, usize)> {
    let mut out = Vec::new();
    for (k, c) in iq.chunks(1200).enumerate() {
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: c, rate: 12_000.0, t0: k as f64 * 0.1, index: (k * 1200) as u64 }, &mut sink).unwrap();
        for (_, o) in sink.items {
            if let Output::Text { text, .. } = o {
                out.extend(text.chars().map(|ch| (ch, k * 1200 + c.len())));
            }
        }
    }
    out
}

/// An over of `syms` (the transmitter's symbols, cut wherever the caller
/// likes) after `lead_s` of noise and before three seconds more of it.
fn over_iq(syms: &[f64], baud: f64, offset_hz: f64, snr_db: f64, seed: u64) -> (Vec<Complex32>, usize) {
    let tx = PskTx { baud, ..Default::default() };
    let sig = shack_sdr::psk::tx::modulate(syms, &tx, 12_000.0, offset_hz, 0.0);
    let lead = 24_000;
    let end = lead + sig.len();
    let n = end + 36_000;
    // psk_iq's noise for the SNR in 2.5 kHz of idle's power.
    let sigma = (0.25 * 0.5 / 10f64.powf(snr_db / 10.0) * 12_000.0 / 2_500.0).sqrt();
    let mut rng = Rng::new(seed);
    let iq = (0..n)
        .map(|i| {
            let s = if (lead..end).contains(&i) { sig[i - lead] * 0.5 } else { Default::default() };
            let z = s + rng.cgauss() * sigma;
            Complex32::new(z.re as f32, z.im as f32)
        })
        .collect();
    (iq, end)
}

#[test]
fn an_over_that_stops_dead_prints_its_last_character_at_once() {
    // A transmitter that unkeys on the last character's "00", with no idle
    // after it (Radio's drain test, our own echo through NB's simulator):
    // psk.py printed the "k" as its "00" came in. The squelch here asks for
    // the over around a character, the four bits after it as well, and
    // refused it; and the trellis decides a second late. Now the squelch
    // closing settles what the trellis holds, and a character whose own
    // bits and "00" are plainly the over's prints without the bits after.
    for b in BAUDS {
        for (snr, seed) in [(5.0, 1), (0.0, 2), (-3.0, 3), (5.0, 4), (0.0, 5)] {
            let tx = PskTx { baud: *b, postamble_s: 0.0, ..Default::default() };
            let syms = shack_sdr::psk::tx::symbols(OVER, &tx);
            let (iq, end) = over_iq(&syms, *b, 3.0, snr, seed);
            let mut d = Psk::new();
            d.configure(&baud(*b)).unwrap();
            let got = feed_timed(&mut d, &iq);
            let text: String = got.iter().map(|(c, _)| *c).collect();
            assert_eq!(text, truth(OVER), "{b} Bd, {snr} dB, seed {seed}");
            // And promptly, not after the trellis's 32 bits of noise: within
            // the smoother's 0.2 s, the matched filter's and the timing's
            // look-ahead (7 symbols), the few bits of noise the squelch
            // needs to close, and the block it was fed in. Measured: 0.61 to
            // 0.71 s at PSK31, 0.38 to 0.48 at PSK125; without the early
            // release, 1.4 s and 0.6 s at the least.
            let late = (got.last().unwrap().1 as f64 - end as f64) / 12_000.0;
            println!("{b} Bd, {snr:+} dB: the last character {late:.2} s after the carrier");
            assert!(late < 0.4 + 16.0 / b, "{b} Bd, {snr} dB: the last character {late:.2} s after the carrier");
        }
    }
}

#[test]
fn an_over_cut_off_mid_character_prints_nothing_false() {
    // An emergency unkey stops the carrier wherever it is, a character
    // half sent. What printed must be the over's text up to a whole
    // character - never a character spelt from its first bits and the
    // noise after them.
    let text = "CQ de GM4BRI pse kn";
    let mut bad = Vec::new();
    for b in BAUDS {
        let tx = PskTx { baud: *b, postamble_s: 0.0, ..Default::default() };
        let full = shack_sdr::psk::tx::symbols(text, &tx);
        // Every cut in the last two characters and their separators.
        let tail = shack_sdr::psk::varicode::bits("kn").len();
        for cut in 1..=tail {
            for snr in [5.0, -3.0] {
                let (iq, _) = over_iq(&full[..full.len() - cut], *b, 0.0, snr, cut as u64);
                let mut d = Psk::new();
                d.configure(&baud(*b)).unwrap();
                let got: String = feed_timed(&mut d, &iq).iter().map(|(c, _)| *c).collect();
                // The whole characters sent before the cut.
                let sent = full.len() - cut - (full.len() - shack_sdr::psk::varicode::bits(text).len());
                let mut whole = String::new();
                let mut n = 0;
                for c in text.chars() {
                    n += shack_sdr::psk::varicode::bits(&c.to_string()).len();
                    if n > sent {
                        break;
                    }
                    whole.push(c);
                }
                // On a strong signal every whole character prints, the last
                // too; on a weaker one the last may wait for bits after it
                // that never come. Nothing else may print.
                let short = if snr > 0.0 { whole.as_str() } else { &whole[..whole.len() - 1] };
                if got != whole && got != short {
                    bad.push(format!("{b} Bd, {snr} dB, cut {cut}: {got:?} for {whole:?}"));
                }
            }
        }
    }
    assert!(bad.is_empty(), "{bad:#?}");
}
