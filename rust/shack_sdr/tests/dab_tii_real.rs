//! TII on real recordings: nine Band III blocks recorded on ohs-box (an
//! RSPdx in Ayrshire, 23/10/2026, 2.4 MS/s ci16 SigMF, 600 MB each), too
//! big for the repository, so these are ignored and look for the files in
//! SHACK_DAB_DIR (default ~/build/dab11b; each block is skipped, and said
//! so, when its file is not there):
//!
//!     SHACK_DAB_DIR=... cargo test --test dab_tii_real -- --ignored --nocapture
//!
//! `every_block_names_what_is_there` holds the detector to what each block
//! carries: Ofcom's list for its ensemble, less what is too weak to read,
//! plus two codes the list does not have but the air plainly does (63/7
//! and 63/5 on Ayr's 11B, 0 and -11/-24 dB in all four quarters of their
//! combs at sure levels: by their delays and levels West Kilbride and
//! Darvel, which Ofcom lists as 64/7 and 64/10; Darvel's 63/5 is its code
//! on Central Scotland's 11D and Glasgow's 11C too). `probe` prints one
//! file's pairs, both kinds of frame, for working out why.
use rustfft::num_complex::Complex32;
use shack_sdr::dab::ofdm::{Demod, Event};
use shack_sdr::dab::tii;
use shack_sdr::dab::Dab;
use shack_sdr::decoder::{Decoder, Input, Output, Sink, Value};
use shack_sdr::dsp::Resampler;
use std::path::PathBuf;

fn read(path: &PathBuf, secs: f64) -> (Vec<Complex32>, f64) {
    let meta = std::fs::read_to_string(path.with_extension("sigmf-meta")).unwrap_or_else(|_| "\"core:sample_rate\": 2400000.0,".into());
    let rate: f64 = meta.split("\"core:sample_rate\":").nth(1).unwrap().trim().split(|c: char| c == ',' || c == '\n').next().unwrap().trim().parse().unwrap();
    let raw = std::fs::read(path).unwrap();
    let n = ((secs * rate) as usize).min(raw.len() / 4);
    let x = (0..n).map(|i| {
        let re = i16::from_le_bytes([raw[4 * i], raw[4 * i + 1]]) as f32 / 32768.0;
        let im = i16::from_le_bytes([raw[4 * i + 2], raw[4 * i + 3]]) as f32 / 32768.0;
        Complex32::new(re, im)
    }).collect();
    (x, rate)
}

fn demod_of(path: &PathBuf, secs: f64) -> Demod {
    let (x, rate) = read(path, secs);
    let mut r = Resampler::<Complex32>::new(rate, 2_048_000.0, 0.75, 16);
    let mut y = Vec::new();
    r.process(&x, &mut y);
    let mut d = Demod::new();
    let msc = [false; 18];
    let mut frames = 0;
    for chunk in y.chunks(204_800) {
        d.push(chunk);
        while let Some(ev) = d.next(&msc) {
            if let Event::Frame(f) = ev { frames += 1; if frames == 1 { eprintln!("offset {:.0} Hz snr {:.1}", f.offset_hz, f.snr_db); } }
        }
    }
    eprintln!("frames {frames}, resyncs {}", d.resyncs);
    d
}

#[test]
#[ignore]
fn probe() {
    let path = PathBuf::from(std::env::var("SHACK_DAB_FILE").unwrap());
    let secs: f64 = std::env::var("SHACK_DAB_SECS").ok().and_then(|s| s.parse().ok()).unwrap_or(10.0);
    let d = demod_of(&path, secs);
    let (on, off, signal) = d.tii.probe().unwrap();
    let db = |v: Complex32| format!("{:4.0}", 10.0 * (v.norm() / signal).max(1e-9).log10());
    for c in 0..24 {
        let row: Vec<String> = (0..8).map(|b| (0..4).map(|q| db(on[(q * 24 + c) * 8 + b])).collect::<Vec<_>>().join("")).collect();
        let row2: Vec<String> = (0..8).map(|b| (0..4).map(|q| db(off[(q * 24 + c) * 8 + b])).collect::<Vec<_>>().join("")).collect();
        let row3: Vec<String> = (0..8).map(|b| (0..4).map(|q| { let i = (q * 24 + c) * 8 + b; db(on[i] - off[i]) }).collect::<Vec<_>>().join("")).collect();
        eprintln!("c{c:2} on  {}\n    off {}\n    dif {}", row.join(" |"), row2.join(" |"), row3.join(" |"));
    }
    for (m, c) in [(64u8, 14usize), (63, 7), (63, 5), (64, 4), (64, 7), (64, 10)] {
        let mask = tii::pattern(m).unwrap();
        let mut acc = Complex32::new(0.0, 0.0);
        for q in 0..4 { for b in 0..8 { if mask >> b & 1 == 1 { acc += on[(q * 24 + c) * 8 + b]; } } }
        let tau = acc.arg() as f64 * 2048.0 / (2.0 * std::f64::consts::PI);
        eprintln!("{m}/{c}: {:.1} dB, delay {:.1} samples = {:.2} km (mod 300 km)", 10.0 * (acc.norm() / 16.0 / signal).log10(), tau, tau / 2.048e6 * 299_792.458);
    }
    eprintln!("heard: {:?}", d.tii.heard());
    let _ = tii::pattern(0);
}

/// What the decoder's feed says at the end of `secs` of a recording:
/// (EId, [(main, sub, levelDb, confidence)]).
fn heard(path: &PathBuf, secs: f64) -> (String, Vec<(u8, u8, f64, f64)>) {
    let (x, rate) = read(path, secs);
    let mut d = Dab::new();
    let block = (rate / 10.0) as usize;
    let mut last = Vec::new();
    for (i, chunk) in x.chunks(block).enumerate() {
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: chunk, rate, t0: 0.0, index: (i * block) as u64 }, &mut sink).unwrap();
        for (_, o) in sink.take() {
            if let Output::Meta { fields } = o {
                last = fields;
            }
        }
    }
    let get = |m: &[(String, Value)], k: &str| m.iter().find(|f| f.0 == k).map(|f| f.1.clone()).unwrap_or(Value::Null);
    let eid = match get(&last, "ensemble") { Value::Map(m) => match get(&m, "id") { Value::Text(t) => t, _ => String::new() }, _ => String::new() };
    let num = |v: Value| if let Value::Num(x) = v { x } else { f64::NAN };
    let tii = match get(&last, "tii") {
        Value::List(l) => l.into_iter().filter_map(|v| if let Value::Map(m) = v { Some((num(get(&m, "main")) as u8, num(get(&m, "sub")) as u8, num(get(&m, "levelDb")), num(get(&m, "confidence")))) } else { None }).collect(),
        _ => vec![],
    };
    (eid, tii)
}

#[test]
#[ignore]
fn list_every_file() {
    let dir = PathBuf::from(std::env::var("SHACK_DAB_DIR").unwrap_or_else(|_| format!("{}/build/dab11b", std::env::var("HOME").unwrap())));
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir).unwrap().filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|e| e == "sigmf-data")).collect();
    files.sort();
    for f in files {
        let (eid, tii) = heard(&f, 30.0);
        let list: Vec<String> = tii.iter().map(|t| format!("{}/{} {:+.1} c{:.1}", t.0, t.1, t.2, t.3)).collect();
        let d = demod_of(&f, 30.0);
        eprintln!("{} {eid}: {}  [nearest miss {:.2} at {}/{}]", f.file_name().unwrap().to_string_lossy(), list.join(", "), d.tii.nearest_miss().0, d.tii.nearest_miss().1, d.tii.nearest_miss().2);
    }
}

/// Each block: a part of its file's name, its EId, the codes it must list
/// at confidence 0.5 or more, and the codes it may list (too weak or too
/// near a louder one to insist on). Anything else may be listed only at
/// SUSPECT (0.3) or less.
const BLOCKS: &[(&str, &str, &[(u8, u8)], &[(u8, u8)])] = &[
    // 9A: Ayr small-scale, not in Ofcom's list under this EId.
    ("233340Z", "C251", &[(64, 11)], &[]),
    // 9B: North Ayrshire small-scale, Baidlandhill Farm.
    ("233449Z", "C23F", &[(64, 12)], &[]),
    // 11A: Sound Digital: Darvel, Black Hill; Divis (Belfast) across the sea.
    ("233559Z", "C1CE", &[(64, 10), (63, 4)], &[(25, 1)]),
    // 11B, the RSP overloaded: Brown Carrick Hill, the two above, Girvan.
    ("233708Z", "C19B", &[(64, 14), (63, 7), (63, 5), (64, 4)], &[]),
    // 11B clean (Antenna C): the same; Girvan is at the edge.
    ("11b.", "C19B", &[(64, 14), (63, 7), (63, 5)], &[(64, 4)]),
    // 11C: Darvel; Rothesay, Rosneath; Black Hill, Millburn Muir weaker.
    // (Sergeant Law sends Central Scotland's 63/3 here too, at the edge.)
    ("233817Z", "C182", &[(63, 5), (57, 13), (57, 12)], &[(57, 4), (57, 15), (63, 3)]),
    // 11D: Central Scotland, Darvel.
    ("233926Z", "C191", &[(63, 5)], &[(63, 3)]),
    // 12A: Digital One: Brown Carrick Hill, Darvel.
    ("234036Z", "C181", &[(64, 14), (64, 10)], &[]),
    // 12B: BBC: Brown Carrick Hill, Darvel, South Knapdale, Girvan,
    // Kirkconnel; Rothesay (30 dB under its neighbour 64/14) and Kirk
    // O'Shotts at the edge.
    ("234145Z", "CE15", &[(64, 14), (64, 10), (61, 8), (64, 4), (66, 2)], &[(57, 13), (63, 1)]),
];

#[test]
#[ignore]
fn every_block_names_what_is_there() {
    let dir = PathBuf::from(std::env::var("SHACK_DAB_DIR").unwrap_or_else(|_| format!("{}/build/dab11b", std::env::var("HOME").unwrap())));
    let files: Vec<PathBuf> = std::fs::read_dir(&dir).map(|r| r.filter_map(|e| e.ok().map(|e| e.path())).collect()).unwrap_or_default();
    let mut bad = Vec::new();
    let mut ran = 0;
    for &(part, eid, must, may) in BLOCKS {
        let Some(f) = files.iter().find(|p| p.to_string_lossy().contains(part) && p.extension().is_some_and(|e| e == "sigmf-data") && p.exists()) else {
            eprintln!("{part}: no recording in {}, skipped", dir.display());
            continue;
        };
        ran += 1;
        let c0 = shack_sdr::cpu::thread_time();
        let (got_eid, tii) = heard(f, 30.0);
        let cpu = (shack_sdr::cpu::thread_time() - c0).as_secs_f64();
        eprintln!("{part}: {:.2} ms CPU per 96 ms frame, reading and resampling included", cpu * 1000.0 / (30.0 / 0.096));
        let list: Vec<String> = tii.iter().map(|t| format!("{}/{} {:+.1} c{:.1}", t.0, t.1, t.2, t.3)).collect();
        eprintln!("{part} {got_eid}: {}", list.join(", "));
        if got_eid != eid {
            bad.push(format!("{part}: ensemble {got_eid}, not {eid}"));
        }
        for code in must {
            if !tii.iter().any(|t| (t.0, t.1) == *code && t.3 >= 0.5) {
                bad.push(format!("{part}: {}/{} not listed at 0.5 or more", code.0, code.1));
            }
        }
        for t in &tii {
            if !must.contains(&(t.0, t.1)) && !may.contains(&(t.0, t.1)) && t.3 > 0.3 + 1e-6 {
                bad.push(format!("{part}: {}/{} at {:.1} is not there", t.0, t.1, t.3));
            }
        }
    }
    assert!(ran > 0, "no recordings in {}", dir.display());
    assert!(bad.is_empty(), "{bad:#?}");
}
