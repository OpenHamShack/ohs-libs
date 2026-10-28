//! DMR on real recordings, and what a channel costs. Both ignored: the
//! recordings are too big for the repository, and timing is a measurement,
//! not a check.
//!
//! Recordings of local repeaters (SigMF, ci16_le or cf32_le, at the
//! device's rate) go in SHACK_DMR_DIR (default ~/build/dmr-real). The
//! channel is `<name>.channel` beside the file (its frequency in Hz), else
//! SHACK_DMR_CHANNEL_HZ, else the capture's own centre (`core:frequency`):
//! it is mixed down from the capture's centre and cut to 48 kS/s, as
//! Radio's bank does, streamed in 100 ms blocks so a ten-minute capture at
//! 2.4 MS/s (5.8 GB) never sits in memory. SHACK_DMR_SECONDS limits how
//! much is read. Each file's results are printed. A `<name>.expect` beside
//! it holds the calls it must find, one `slot src dst` a line (`#`
//! comments); without one a file need only lock.
//!
//!     SHACK_DMR_DIR=... cargo test --release --test dmr_real -- --ignored --nocapture
use rustfft::num_complex::Complex32;
use shack_sdr::decoder::{Decoder, Input, Output, Sink, Value};
use shack_sdr::dmr::tx::{dmr_iq, showcase, DmrCase};
use shack_sdr::dmr::Dmr;
use shack_sdr::dsp::{lowpass, Decimator};
use std::path::{Path, PathBuf};

fn meta_value(meta: &str, key: &str) -> Option<String> {
    let rest = meta.split(&format!("\"{key}\"")).nth(1)?;
    let v = rest.trim_start().strip_prefix(':')?.trim_start();
    let end = v.find([',', '\n', '}']).unwrap_or(v.len());
    Some(v[..end].trim().trim_matches('"').to_string())
}

/// A capture, read block by block: its rate, centre, and the next block.
struct Capture {
    file: std::io::BufReader<std::fs::File>,
    rate: f64,
    centre: Option<f64>,
    cf32: bool,
}

impl Capture {
    fn open(path: &Path) -> Capture {
        let meta = std::fs::read_to_string(path.with_extension("sigmf-meta")).expect("a .sigmf-meta beside it");
        let rate: f64 = meta_value(&meta, "core:sample_rate").expect("core:sample_rate").parse().unwrap();
        let centre = meta_value(&meta, "core:frequency").and_then(|v| v.parse().ok());
        let dt = meta_value(&meta, "core:datatype").unwrap_or_else(|| "ci16_le".into());
        assert!(dt == "ci16_le" || dt == "cf32_le", "{}: datatype {dt} not read here", path.display());
        Capture { file: std::io::BufReader::new(std::fs::File::open(path).unwrap()), rate, centre, cf32: dt == "cf32_le" }
    }

    fn block(&mut self, n: usize) -> Vec<Complex32> {
        use std::io::Read;
        let size = if self.cf32 { 8 } else { 4 };
        let mut raw = vec![0u8; n * size];
        let mut got = 0;
        while got < raw.len() {
            match self.file.read(&mut raw[got..]) {
                Ok(0) | Err(_) => break,
                Ok(k) => got += k,
            }
        }
        raw.truncate(got - got % size);
        if self.cf32 {
            raw.chunks_exact(8).map(|c| Complex32::new(f32::from_le_bytes(c[..4].try_into().unwrap()), f32::from_le_bytes(c[4..].try_into().unwrap()))).collect()
        } else {
            raw.chunks_exact(4)
                .map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0, i16::from_le_bytes([c[2], c[3]]) as f32 / 32768.0))
                .collect()
        }
    }
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
    let limit: f64 = std::env::var("SHACK_DMR_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(f64::INFINITY);
    for path in files {
        let mut cap = Capture::open(&path);
        let rate = cap.rate;
        let channel = std::fs::read_to_string(path.with_extension("channel"))
            .ok()
            .and_then(|t| t.trim().parse::<f64>().ok())
            .or_else(|| std::env::var("SHACK_DMR_CHANNEL_HZ").ok().and_then(|v| v.parse().ok()))
            .or(cap.centre);
        let shift = match (channel, cap.centre) {
            (Some(ch), Some(c)) => ch - c,
            _ => 0.0,
        };
        // Down to the channel, then to about 48 kS/s by a whole decimation,
        // as Radio's bank cuts a channel (the decoder resamples the rest).
        let d = ((rate / 48_000.0).floor() as usize).max(1);
        let mut dec = Decimator::new(lowpass(8 * d + 1, 7_000.0, rate), d);
        let mut phase = 0f64;
        let step = -2.0 * std::f64::consts::PI * shift / rate;
        let mut d_dmr = Dmr::new();
        let mut sink = Sink::new();
        let block = (rate / 10.0) as usize;
        let (mut index, mut read) = (0u64, 0usize);
        let mut out = Vec::new();
        loop {
            let x = cap.block(block);
            if x.is_empty() || read as f64 / rate >= limit {
                break;
            }
            read += x.len();
            let mixed: Vec<Complex32> = x
                .iter()
                .map(|z| {
                    let r = Complex32::new(phase.cos() as f32, phase.sin() as f32);
                    phase = (phase + step) % (2.0 * std::f64::consts::PI);
                    z * r
                })
                .collect();
            out.clear();
            dec.process(&mixed, &mut out);
            let r = rate / d as f64;
            let _ = d_dmr.feed(Input::Iq { samples: &out, rate: r, t0: index as f64 / r, index }, &mut sink);
            index += out.len() as u64;
        }
        let d = d_dmr;
        let x_len = read;
        let mut calls = Vec::new();
        println!("== {} ({:.1} s at {rate} S/s, channel {channel:?}, {shift:+.0} Hz from the centre)", path.display(), x_len as f64 / rate);
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
