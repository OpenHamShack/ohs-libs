//! DMR on real recordings, and what a channel costs. Both ignored: the
//! recordings are too big for the repository, and timing is a measurement,
//! not a check.
//!
//! Recordings of local repeaters (SigMF, ci16_le or cf32_le, centred on the
//! channel at any rate from 16 kS/s) go in SHACK_DMR_DIR (default
//! ~/build/dmr-real). Each file is decoded whole and what it held printed.
//! A `<name>.expect` beside it holds the calls it must find, one
//! `slot src dst` a line (`#` comments); without one a file need only lock.
//!
//!     SHACK_DMR_DIR=... cargo test --release --test dmr_real -- --ignored --nocapture
use rustfft::num_complex::Complex32;
use shack_sdr::decoder::{Decoder, Input, Output, Sink, Value};
use shack_sdr::dmr::tx::{dmr_iq, showcase, DmrCase};
use shack_sdr::dmr::Dmr;
use std::path::{Path, PathBuf};

fn meta_value(meta: &str, key: &str) -> Option<String> {
    let rest = meta.split(&format!("\"{key}\"")).nth(1)?;
    let v = rest.trim_start().strip_prefix(':')?.trim_start();
    let end = v.find([',', '\n', '}']).unwrap_or(v.len());
    Some(v[..end].trim().trim_matches('"').to_string())
}

fn read(path: &Path) -> (Vec<Complex32>, f64) {
    let meta = std::fs::read_to_string(path.with_extension("sigmf-meta")).expect("a .sigmf-meta beside it");
    let rate: f64 = meta_value(&meta, "core:sample_rate").expect("core:sample_rate").parse().unwrap();
    let dt = meta_value(&meta, "core:datatype").unwrap_or_else(|| "ci16_le".into());
    let raw = std::fs::read(path).unwrap();
    let x = match dt.as_str() {
        "cf32_le" => raw.chunks_exact(8).map(|c| Complex32::new(f32::from_le_bytes(c[..4].try_into().unwrap()), f32::from_le_bytes(c[4..].try_into().unwrap()))).collect(),
        "ci16_le" => raw
            .chunks_exact(4)
            .map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0, i16::from_le_bytes([c[2], c[3]]) as f32 / 32768.0))
            .collect(),
        other => panic!("{}: datatype {other} not read here", path.display()),
    };
    (x, rate)
}

fn field(f: &[(String, Value)], k: &str) -> String {
    match f.iter().find(|(n, _)| n == k).map(|(_, v)| v) {
        Some(Value::Num(x)) => format!("{x}"),
        Some(Value::Text(t)) => t.clone(),
        Some(Value::Bool(b)) => format!("{b}"),
        _ => "-".into(),
    }
}

#[test]
#[ignore]
fn recordings_of_local_repeaters() {
    let dir = std::env::var_os("SHACK_DMR_DIR").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(std::env::var("HOME").unwrap()).join("build/dmr-real"));
    let Ok(entries) = std::fs::read_dir(&dir) else {
        eprintln!("skipping: no folder {}", dir.display());
        return;
    };
    let mut files: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|e| e == "sigmf-data")).collect();
    files.sort();
    if files.is_empty() {
        eprintln!("skipping: no .sigmf-data in {}", dir.display());
    }
    for path in files {
        let (x, rate) = read(&path);
        let mut d = Dmr::new();
        let mut sink = Sink::new();
        let block = (rate / 10.0) as usize;
        for (k, b) in x.chunks(block).enumerate() {
            let index = (k * block) as u64;
            let _ = d.feed(Input::Iq { samples: b, rate, t0: index as f64 / rate, index }, &mut sink);
        }
        let mut calls = Vec::new();
        println!("== {} ({:.1} s at {rate} S/s)", path.display(), x.len() as f64 / rate);
        for (t, o) in sink.take() {
            if let Output::Frame { kind, fields, .. } = o {
                let line = format!(
                    "{t:7.2} {kind:12} slot {} src {} dst {} {} cc {} alias {} enc {} ber {} text {}",
                    field(&fields, "slot"),
                    field(&fields, "src"),
                    field(&fields, "dst"),
                    field(&fields, "callType"),
                    field(&fields, "cc"),
                    field(&fields, "alias"),
                    field(&fields, "encrypted"),
                    field(&fields, "ber"),
                    field(&fields, "text")
                );
                println!("{line}");
                if kind == "call_end" {
                    calls.push(format!("{} {} {}", field(&fields, "slot"), field(&fields, "src"), field(&fields, "dst")));
                }
            }
        }
        let st = d.status();
        println!("   {:?}", st);
        let syncs = st.counters.iter().find(|(k, _)| *k == "syncs").map(|c| c.1).unwrap_or(0);
        assert!(syncs > 0, "{}: no DMR sync at all", path.display());
        if let Ok(want) = std::fs::read_to_string(path.with_extension("expect")) {
            for w in want.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')) {
                assert!(calls.iter().any(|c| c == w), "{}: no call `{w}` in {calls:?}", path.display());
            }
        }
    }
}

/// CPU per channel: the showcase (both slots busy) decoded on one thread,
/// as a share of real time. On shack (28/10/2026, release): see the
/// handover; the wheel is built the same way (LTO, one codegen unit).
#[test]
#[ignore]
fn cpu_per_channel() {
    // A busy repeater at three rates, then noise alone (the framer searching
    // every sample, its dearest state).
    for (rate, busy) in [(48_000.0, true), (24_000.0, true), (240_000.0, true), (48_000.0, false)] {
        let case = if busy {
            DmrCase { slots: showcase(), snr_db: Some(20.0), rate, ..Default::default() }
        } else {
            DmrCase { mode: shack_sdr::dmr::tx::Mode::Ms, lead: 230, snr_db: Some(0.0), rate, ..Default::default() }
        };
        let mut x = Vec::new();
        for _ in 0..4 {
            x.extend(dmr_iq(&case).iq);
        }
        let secs = x.len() as f64 / rate;
        let mut d = Dmr::new();
        let mut sink = Sink::new();
        let block = (rate / 10.0) as usize;
        let t = std::time::Instant::now();
        for (k, b) in x.chunks(block).enumerate() {
            let index = (k * block) as u64;
            let _ = d.feed(Input::Iq { samples: b, rate, t0: index as f64 / rate, index }, &mut sink);
            sink.take();
        }
        let spent = t.elapsed().as_secs_f64();
        let what = if busy { "busy repeater" } else { "noise only" };
        println!("dmr at {rate} S/s, {what}: {:.2} % of a core ({spent:.3} s for {secs:.1} s of IQ)", 100.0 * spent / secs);
    }
}
