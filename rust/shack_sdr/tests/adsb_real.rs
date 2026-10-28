//! ADS-B on real recordings. Ignored: the recordings are too big for the
//! repository (five minutes at 2.4 MS/s is 2.9 GB), and the aircraft in
//! them are real, so what they must show lives beside them, never here.
//!
//! SHACK_ADSB_DIR names a folder of SigMF captures (ci16_le or cf32_le, at
//! the device's rate, any centre within the band: the demodulator works on
//! the magnitude, so 1090 MHz need not be at DC). Each `<name>.sigmf-data`
//! is streamed in 100 ms blocks, as Radio feeds the decoder, with the
//! station from `<name>.expect`. That file holds, one per line (`#`
//! comments):
//!
//!     station <lat> <lon>       where the receiver was (for the range check)
//!     min_aircraft <n>          at least this many aircraft shown
//!     max_aircraft <n>          and no more (a guard against invented ones)
//!     near <lat> <lon> <km>     every aircraft shown is within <km> of here
//!     icao <hex>                this aircraft must be among them
//!     callsign <text>           and this callsign
//!
//! Every aircraft's last state is printed, with the decoder's counters and
//! the CPU it cost. SHACK_ADSB_SECONDS limits how much is read.
//!
//!     SHACK_ADSB_DIR=... cargo test --release --test adsb_real -- --ignored --nocapture
use rustfft::num_complex::Complex32;
use shack_sdr::adsb::cpr::distance_km;
use shack_sdr::adsb::Adsb;
use shack_sdr::decoder::{Decoder, Input, Output, Params, Sink, Value};
use std::collections::BTreeMap;
use std::io::Read;
use std::path::Path;
use std::time::Instant;

fn meta_value(meta: &str, key: &str) -> Option<String> {
    let rest = meta.split(&format!("\"{key}\"")).nth(1)?;
    let v = rest.trim_start().strip_prefix(':')?.trim_start();
    let end = v.find([',', '\n', '}']).unwrap_or(v.len());
    Some(v[..end].trim().trim_matches('"').to_string())
}

fn num(f: &[(String, Value)], k: &str) -> Option<f64> {
    match f.iter().find(|(n, _)| n == k).map(|(_, v)| v) {
        Some(Value::Num(x)) => Some(*x),
        _ => None,
    }
}

fn text(f: &[(String, Value)], k: &str) -> String {
    match f.iter().find(|(n, _)| n == k).map(|(_, v)| v) {
        Some(Value::Text(t)) => t.clone(),
        Some(Value::Num(x)) => format!("{x}"),
        Some(Value::Bool(b)) => format!("{b}"),
        _ => "-".into(),
    }
}

struct Expect {
    station: Option<(f64, f64)>,
    min: usize,
    max: Option<usize>,
    near: Option<(f64, f64, f64)>,
    icao: Vec<String>,
    callsign: Vec<String>,
}

fn expect(path: &Path) -> Expect {
    let mut e = Expect { station: None, min: 1, max: None, near: None, icao: vec![], callsign: vec![] };
    let Ok(s) = std::fs::read_to_string(path) else { return e };
    for line in s.lines() {
        let line = line.split('#').next().unwrap().trim();
        let w: Vec<&str> = line.split_whitespace().collect();
        match w.as_slice() {
            ["station", a, b] => e.station = Some((a.parse().unwrap(), b.parse().unwrap())),
            ["min_aircraft", n] => e.min = n.parse().unwrap(),
            ["max_aircraft", n] => e.max = Some(n.parse().unwrap()),
            ["near", a, b, k] => e.near = Some((a.parse().unwrap(), b.parse().unwrap(), k.parse().unwrap())),
            ["icao", h] => e.icao.push(h.to_uppercase()),
            ["callsign", c] => e.callsign.push(c.to_uppercase()),
            [] => {}
            _ => panic!("{}: cannot read {line:?}", path.display()),
        }
    }
    e
}

#[test]
#[ignore]
fn real_recordings() {
    let Ok(dir) = std::env::var("SHACK_ADSB_DIR") else {
        println!("SHACK_ADSB_DIR is not set: no real recordings read");
        return;
    };
    let limit: f64 = std::env::var("SHACK_ADSB_SECONDS").ok().and_then(|s| s.parse().ok()).unwrap_or(f64::INFINITY);
    let mut files: Vec<_> = std::fs::read_dir(&dir).unwrap().flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "sigmf-data")).collect();
    files.sort();
    assert!(!files.is_empty(), "no .sigmf-data in {dir}");
    for path in files {
        let meta = std::fs::read_to_string(path.with_extension("sigmf-meta")).expect("a .sigmf-meta beside it");
        let rate: f64 = meta_value(&meta, "core:sample_rate").unwrap().parse().unwrap();
        let cf32 = meta_value(&meta, "core:datatype").as_deref() == Some("cf32_le");
        let exp = expect(&path.with_extension("expect"));
        let mut d = Adsb::new();
        let mut p = Params::new();
        if let Some((la, lo)) = exp.station {
            p = p.with("refLat", Value::Num(la)).with("refLon", Value::Num(lo));
        }
        d.configure(&p).unwrap();
        let mut file = std::io::BufReader::with_capacity(1 << 22, std::fs::File::open(&path).unwrap());
        let block = (rate / 10.0) as usize;
        let size = if cf32 { 8 } else { 4 };
        let mut raw = vec![0u8; block * size];
        let mut index = 0u64;
        let mut last: BTreeMap<String, Vec<(String, Value)>> = BTreeMap::new();
        let mut cpu = 0.0;
        let mut sink = Sink::new();
        loop {
            if index as f64 / rate >= limit {
                break;
            }
            let mut got = 0;
            while got < raw.len() {
                match file.read(&mut raw[got..]) {
                    Ok(0) | Err(_) => break,
                    Ok(k) => got += k,
                }
            }
            if got < size {
                break;
            }
            let x: Vec<Complex32> = if cf32 {
                raw[..got - got % 8].chunks_exact(8).map(|c| Complex32::new(f32::from_le_bytes(c[..4].try_into().unwrap()), f32::from_le_bytes(c[4..].try_into().unwrap()))).collect()
            } else {
                raw[..got - got % 4].chunks_exact(4).map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0, i16::from_le_bytes([c[2], c[3]]) as f32 / 32768.0)).collect()
            };
            let start = Instant::now();
            d.feed(Input::Iq { samples: &x, rate, t0: index as f64 / rate, index }, &mut sink).unwrap();
            cpu += start.elapsed().as_secs_f64();
            index += x.len() as u64;
            for (_, o) in sink.take() {
                if let Output::Frame { fields, .. } = o {
                    last.insert(text(&fields, "icao"), fields);
                }
            }
        }
        let secs = index as f64 / rate;
        println!("{}: {:.0} s at {:.1} MS/s, {:.1} ms of CPU a second", path.display(), secs, rate / 1e6, cpu / secs * 1e3);
        println!("  {:?}", d.status().counters);
        for f in last.values() {
            println!(
                "  {:7} {:8} cat {:3} sq {:4} {:9.4} {:9.4} alt {:6} geo {:6} gs {:5} trk {:5} vr {:6} gnd {:5} msgs {:5} rssi {:6} {}",
                text(f, "icao"), text(f, "callsign"), text(f, "category"), text(f, "squawk"),
                num(f, "lat").unwrap_or(f64::NAN), num(f, "lon").unwrap_or(f64::NAN),
                text(f, "altBaro"), text(f, "altGeo"), text(f, "gs"), text(f, "track"), text(f, "vrate"),
                text(f, "onGround"), text(f, "messages"), text(f, "rssiDb"), text(f, "emergency"),
            );
        }
        let n = last.len();
        assert!(n >= exp.min, "{}: {n} aircraft, expected at least {}", path.display(), exp.min);
        if let Some(m) = exp.max {
            assert!(n <= m, "{}: {n} aircraft, expected no more than {m}", path.display());
        }
        for f in last.values() {
            let alt = num(f, "altBaro").or(num(f, "altGeo"));
            if let Some(a) = alt {
                assert!((-1000.0..=50000.0).contains(&a), "{}: altitude {a}", text(f, "icao"));
            }
            if let (Some((la, lo, km)), Some(a), Some(b)) = (exp.near, num(f, "lat"), num(f, "lon")) {
                let dist = distance_km(la, lo, a, b);
                assert!(dist <= km, "{} at {a},{b} is {dist:.0} km from {la},{lo}", text(f, "icao"));
            }
        }
        for i in &exp.icao {
            assert!(last.contains_key(i), "{}: no {i}", path.display());
        }
        for c in &exp.callsign {
            assert!(last.values().any(|f| text(f, "callsign") == *c), "{}: no callsign {c}", path.display());
        }
    }
}
