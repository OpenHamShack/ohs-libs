//! The AIS decoder: messages read and written bit for bit against an
//! implementation that is not ours (testdata/ais/golden.json, from pyais
//! by tools/ais_golden.py), then over the air through the test
//! transmitter: every type, both channels at once, a damaged burst, bit
//! stuffing, an SNR and a frequency-error sweep, collisions, an empty
//! channel, the receiver rates Radio feeds, and the CPU it costs.
//!
//! Real recordings: `SHACK_AIS_DIR` names a folder of `<name>.cf32` (or
//! `.cs16`, interleaved little-endian) captures, each with `<name>.json`
//! beside it: {"rate": S/s, "offsetHz": where 162.000 MHz is from the
//! capture's centre, "minFrames": how many good frames it must give}.
//! Unset, that test says so and passes. `AIS_SHOW=1` prints the sweeps.

use rustfft::num_complex::Complex32;
use serde_json::Value as J;
use shack_sdr::ais::hdlc;
use shack_sdr::ais::msg::{self, Raw, RawV};
use shack_sdr::ais::tx::{scene_iq, Burst, Channel, Scene};
use shack_sdr::ais::Ais;
use shack_sdr::decoder::{Decoder, Input, Output, Params, Sink, Value};
use std::time::Instant;

const DATA: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/ais");

fn golden() -> Vec<J> {
    let j: J = serde_json::from_str(&std::fs::read_to_string(format!("{DATA}/golden.json")).unwrap()).unwrap();
    j["cases"].as_array().unwrap().clone()
}

fn show() -> bool {
    std::env::var("AIS_SHOW").is_ok()
}

/// A case's sentences back to the payload's bits.
fn bits_of(sentences: &[J]) -> Vec<u8> {
    let mut payload = String::new();
    let mut fill = 0;
    for s in sentences {
        let s = s.as_str().unwrap();
        let (body, cs) = s[1..].split_once('*').unwrap();
        assert_eq!(msg::checksum(body), u8::from_str_radix(cs, 16).unwrap(), "{s}");
        let parts: Vec<&str> = body.split(',').collect();
        payload.push_str(parts[5]);
        fill = parts[6].parse().unwrap();
    }
    msg::dearmour(&payload, fill).unwrap()
}

fn field<'a>(f: &'a [(String, Value)], k: &str) -> Option<&'a Value> {
    f.iter().find(|(n, _)| n == k).map(|(_, v)| v)
}

/// Every field the golden case gives, as ours reports it.
fn assert_fields(name: &str, got: &[(String, Value)], want: &J) {
    for (k, w) in want.as_object().unwrap() {
        let g = field(got, k).unwrap_or_else(|| panic!("{name}: no field {k} in {got:?}"));
        let ok = match (g, w) {
            (Value::Null, J::Null) => true,
            (Value::Num(a), J::Number(b)) => (a - b.as_f64().unwrap()).abs() < 1e-6,
            (Value::Bool(a), J::Bool(b)) => a == b,
            (Value::Text(a), J::String(b)) => a == b,
            _ => false,
        };
        assert!(ok, "{name}: {k} is {g:?}, pyais read {w}");
    }
}

fn raw_of(j: &J) -> Raw {
    j.as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| {
            let v = match v {
                J::String(s) => RawV::Text(s.clone()),
                J::Bool(b) => RawV::Int(*b as i64),
                n => RawV::Int(n.as_i64().unwrap()),
            };
            (k.clone(), v)
        })
        .collect()
}

#[test]
fn messages_heard_by_real_receivers_read_as_pyais_reads_them() {
    let mut n = 0;
    for c in golden().iter().filter(|c| c["kind"] == "heard") {
        let sentences = c["sentences"].as_array().unwrap();
        let bits = bits_of(sentences);
        let m = msg::parse(&bits).unwrap_or_else(|| panic!("{sentences:?} not read"));
        assert_fields(sentences[0].as_str().unwrap(), &msg::fields(&m), &c["fields"]);
        // Our sentences for the same bits are the same sentences, checksum
        // and all: the armour is bit-exact.
        let s0 = sentences[0].as_str().unwrap();
        let parts: Vec<&str> = s0.split(',').collect();
        let ch = parts[4].chars().next().unwrap();
        let seq: u8 = parts[3].parse().unwrap_or(0);
        let ours = msg::nmea(&bits, ch, seq);
        let theirs: Vec<&str> = sentences.iter().map(|s| s.as_str().unwrap()).collect();
        assert_eq!(ours, theirs);
        n += 1;
    }
    assert_eq!(n, 8);
}

#[test]
fn every_type_is_written_bit_for_bit_as_pyais_writes_it_and_read_back() {
    let mut kinds = std::collections::BTreeSet::new();
    for c in golden().iter().filter(|c| c["kind"] == "made") {
        let name = c["name"].as_str().unwrap();
        let theirs = bits_of(c["sentences"].as_array().unwrap());
        let ours = msg::encode(&raw_of(&c["raw"])).unwrap();
        // pyais pads some types further (24 A to 168 bits, 21 to its
        // longest name); the standard's bits are the same and the rest zeros.
        let n = ours.len().min(theirs.len());
        assert_eq!(ours[..n], theirs[..n], "{name}: payloads differ");
        assert!(ours[n..].iter().chain(&theirs[n..]).all(|&b| b == 0), "{name}: more than padding differs");
        let m = msg::parse(&ours).unwrap();
        assert_fields(name, &msg::fields(&m), &c["fields"]);
        // ...and the raw values come back as written.
        for (k, v) in raw_of(&c["raw"]) {
            assert_eq!(m.raw.get(&k), Some(&v), "{name}: {k}");
        }
        kinds.insert(m.kind);
    }
    assert_eq!(kinds.into_iter().collect::<Vec<_>>(), vec![1, 2, 3, 4, 5, 18, 19, 21, 24, 27]);
}

/// What an HDLC receiver that is not ours makes of a burst's on-air bits,
/// written as Direwolf does it (hdlc_rec.c, ais.c): skip to the opening
/// flag, drop each 0 after five 1s, build octets LSB first (`oacc >>= 1;
/// oacc |= 0x80`), stop at the closing flag, check the FCS, then read the
/// message's bits MSB first from each octet (`get_field`, mask 0x80
/// first). Nothing here calls hdlc.rs's pack, unpack or deframer.
fn as_direwolf_reads(on_air: &[u8]) -> Vec<u8> {
    let flag = [0u8, 1, 1, 1, 1, 1, 1, 0];
    let start = on_air.windows(8).position(|w| w == flag).unwrap() + 8;
    let (mut octets, mut acc, mut n, mut ones) = (Vec::new(), 0u8, 0, 0);
    let mut i = start;
    while i < on_air.len() {
        if on_air[i..].starts_with(&flag) {
            break;
        }
        let b = on_air[i];
        i += 1;
        if ones == 5 {
            assert_eq!(b, 0, "a stuffed bit is a 0");
            ones = 0;
            continue;
        }
        ones = if b == 1 { ones + 1 } else { 0 };
        acc >>= 1;
        if b == 1 {
            acc |= 0x80;
        }
        n += 1;
        if n == 8 {
            octets.push(acc);
            n = 0;
        }
    }
    assert_eq!(n, 0, "whole octets");
    assert_eq!(hdlc::crc16(&octets), hdlc::GOOD_RESIDUE, "the FCS");
    octets[..octets.len() - 2].iter().flat_map(|&v| [0x80u8, 0x40, 0x20, 0x10, 8, 4, 2, 1].map(|m| (v & m != 0) as u8)).collect()
}

#[test]
fn on_air_the_bits_are_in_the_order_real_transponders_send() {
    // Every golden message as our transmitter keys it, read back the way a
    // decoder that has heard real ships reads it, must be the message.
    for c in golden() {
        let bits = bits_of(c["sentences"].as_array().unwrap());
        let on_air = hdlc::burst_bits(&hdlc::pack(&bits), 8, 8);
        let got = as_direwolf_reads(&on_air);
        let mut want = bits.clone();
        while want.len() % 8 != 0 {
            want.push(0);
        }
        assert_eq!(got, want, "{:?}", c["sentences"]);
    }
}

#[test]
fn implausible_messages_are_marked() {
    let base = |mmsi: i64, kind: i64| -> Raw {
        [("type", kind), ("mmsi", mmsi), ("lat", 33_270_000), ("lon", -2_940_000), ("speed", 100), ("course", 900),
         ("heading", 90)]
            .into_iter()
            .map(|(k, v)| (k.to_string(), RawV::Int(v)))
            .collect()
    };
    let check = |raw: &Raw| msg::plausible(&msg::parse(&msg::encode(raw).unwrap()).unwrap());
    assert_eq!(check(&base(235000001, 1)), Ok(()));
    assert_eq!(check(&base(972000001, 1)), Ok(()));             // a MOB beacon
    assert_eq!(check(&base(2906440, 1)), Err("mmsi"));          // seven digits: a ship has nine
    assert_eq!(check(&base(120863493, 1)), Err("mmsi"));        // MID 120 is no country
    assert_eq!(check(&base(993167817, 1)), Err("mmsi"));        // an aid's MMSI on a ship's report
    assert_eq!(check(&base(2320771, 4)), Ok(()));               // 002320771: a UK base station
    assert_eq!(check(&base(232077100, 4)), Err("mmsi"));
    assert_eq!(check(&base(992351234, 21)), Ok(()));
    let mut r = base(235000001, 1);
    r.insert("lat".into(), RawV::Int(95 * 600_000));
    assert_eq!(check(&r), Err("position"));
    r.insert("lat".into(), RawV::Int(91 * 600_000));
    r.insert("lon".into(), RawV::Int(181 * 600_000));
    assert_eq!(check(&r), Ok(()));                              // "not available" is no lie
    let mut r = base(235000001, 1);
    r.insert("course".into(), RawV::Int(3700));
    assert_eq!(check(&r), Err("course"));
    let mut r = base(235000001, 1);
    r.insert("heading".into(), RawV::Int(400));
    assert_eq!(check(&r), Err("heading"));
}

/// Decode `iq` (at `rate`, in 0.1 s blocks, as Radio's engine feeds it).
fn decode(iq: &[Complex32], rate: f64, params: Params) -> (Vec<(f64, Vec<(String, Value)>, Vec<u8>)>, Ais) {
    let mut d = Ais::new();
    d.configure(&params).unwrap();
    let mut sink = Sink::new();
    let block = (rate / 10.0) as usize;
    for (i, c) in iq.chunks(block).enumerate() {
        let index = (i * block) as u64;
        d.feed(Input::Iq { samples: c, rate, t0: 1000.0 + index as f64 / rate, index }, &mut sink).unwrap();
    }
    let frames = sink
        .take()
        .into_iter()
        .filter_map(|(t, o)| match o {
            Output::Frame { kind: "ais", fields, raw } => Some((t, fields, raw.unwrap())),
            _ => None,
        })
        .collect();
    (frames, d)
}

fn position(mmsi: i64, lat: f64, lon: f64) -> Vec<u8> {
    let raw: Raw = [
        ("type", 1),
        ("mmsi", mmsi),
        ("status", 0),
        ("speed", 123),
        ("lat", (lat * 600_000.0).round() as i64),
        ("lon", (lon * 600_000.0).round() as i64),
        ("course", 2715),
        ("heading", 271),
        ("second", 30),
        ("turn", -128),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), RawV::Int(v)))
    .collect();
    msg::encode(&raw).unwrap()
}

fn mmsi_of(f: &[(String, Value)]) -> i64 {
    match field(f, "mmsi") {
        Some(Value::Num(m)) => *m as i64,
        _ => panic!("no mmsi"),
    }
}

fn text_of(f: &[(String, Value)], k: &str) -> String {
    match field(f, k) {
        Some(Value::Text(s)) => s.clone(),
        other => panic!("{k}: {other:?}"),
    }
}

fn num_of(f: &[(String, Value)], k: &str) -> f64 {
    match field(f, k) {
        Some(Value::Num(x)) => *x,
        other => panic!("{k}: {other:?}"),
    }
}

#[test]
fn every_type_comes_through_the_air_on_its_channel_with_its_sentences() {
    let cases: Vec<J> = golden();
    let mut bursts = Vec::new();
    for (i, c) in cases.iter().enumerate() {
        let ch = if i % 2 == 0 { Channel::A } else { Channel::B };
        bursts.push(Burst::new(0.05 + 0.15 * i as f64, ch, bits_of(c["sentences"].as_array().unwrap())));
    }
    let n = bursts.len();
    let scene = Scene { rate: 96_000.0, duration_s: 0.2 + 0.15 * n as f64, centre_hz: 0.0, snr_db: Some(30.0), seed: 7, bursts: bursts.clone() };
    let (frames, d) = decode(&scene_iq(&scene), 96_000.0, Params::new());
    assert_eq!(frames.len(), n, "{frames:?}");
    for ((t, f, raw), (b, c)) in frames.iter().zip(bursts.iter().zip(&cases)) {
        // Whole octets on the air: the payload as sent, padded.
        let mut want = b.bits.clone();
        while want.len() % 8 != 0 {
            want.push(0);
        }
        assert_eq!(hdlc::unpack(&hdlc::pack(&want)), want);
        let got: Vec<u8> = raw.iter().flat_map(|&v| (0..8).rev().map(move |i| (v >> i) & 1)).collect();
        assert_eq!(got, want);
        assert_eq!(text_of(f, "channel"), b.channel.code().to_string());
        assert_eq!(field(f, "plausible"), Some(&Value::Bool(true)), "{f:?}");
        assert_fields(&format!("{:?}", c["sentences"]), f, &c["fields"]);
        // The time is the burst's end, from the samples.
        let end = 1000.0 + b.t_s + b.duration_s();
        assert!((t - end).abs() < 0.01, "t {t} want about {end}");
        // A sentence for each, checksummed, on the right channel.
        let Some(Value::List(s)) = field(f, "nmea") else { panic!() };
        assert!(!s.is_empty());
        for v in s {
            let Value::Text(v) = v else { panic!() };
            assert!(v.starts_with("!AIVDM,"), "{v}");
            assert_eq!(v.split(',').nth(4), Some(b.channel.code().to_string().as_str()));
        }
    }
    let st = d.status();
    let count = |k: &str| st.counters.iter().find(|(n, _)| *n == k).unwrap().1;
    assert_eq!(count("channelA") + count("channelB"), n as u64);
    assert_eq!(count("channelA"), n.div_ceil(2) as u64);
}

#[test]
fn both_channels_at_the_same_moment_are_both_heard() {
    let a = position(235000001, 55.5, -4.9);
    let b = position(235000002, 55.6, -5.0);
    let bursts = vec![Burst::new(0.1, Channel::A, a), Burst::new(0.1, Channel::B, b)];
    let scene = Scene { rate: 96_000.0, duration_s: 0.3, centre_hz: 0.0, snr_db: Some(25.0), seed: 3, bursts };
    let (frames, _) = decode(&scene_iq(&scene), 96_000.0, Params::new());
    let mut got: Vec<(i64, String)> = frames.iter().map(|(_, f, _)| (mmsi_of(f), text_of(f, "channel"))).collect();
    got.sort();
    assert_eq!(got, vec![(235000001, "A".into()), (235000002, "B".into())]);
}

#[test]
fn a_damaged_burst_fails_its_crc_and_prints_nothing() {
    let mut b = Burst::new(0.1, Channel::A, position(235000003, 55.5, -4.9));
    // A bit in the middle of the payload (ramp 8 + training 24 + flag 8).
    b.flip_bits = vec![8 + 24 + 8 + 60];
    let scene = Scene { rate: 96_000.0, duration_s: 0.3, centre_hz: 0.0, snr_db: Some(30.0), seed: 4, bursts: vec![b] };
    let (frames, d) = decode(&scene_iq(&scene), 96_000.0, Params::new());
    assert!(frames.is_empty(), "{frames:?}");
    let bad = d.status().counters.iter().find(|(n, _)| *n == "damaged").unwrap().1;
    assert!(bad >= 1, "{bad}");
    // And the same burst undamaged is heard (the test's own control).
    let b = Burst::new(0.1, Channel::A, position(235000003, 55.5, -4.9));
    let scene = Scene { rate: 96_000.0, duration_s: 0.3, centre_hz: 0.0, snr_db: Some(30.0), seed: 4, bursts: vec![b] };
    assert_eq!(decode(&scene_iq(&scene), 96_000.0, Params::new()).0.len(), 1);
}

#[test]
fn long_runs_of_ones_are_stuffed_and_unstuffed() {
    // A binary broadcast (type 8) of all ones: a stuffed 0 after every five.
    let mut bits = Vec::new();
    for (v, n) in [(8u64, 6), (0, 2), (0x3FFF_FFFF, 30), (0, 2)] {
        bits.extend((0..n).rev().map(|i| ((v >> i) & 1) as u8));
    }
    bits.extend(std::iter::repeat_n(1u8, 160));
    let scene = Scene { rate: 96_000.0, duration_s: 0.3, centre_hz: 0.0, snr_db: Some(30.0), seed: 5,
                        bursts: vec![Burst::new(0.1, Channel::B, bits.clone())] };
    let (frames, _) = decode(&scene_iq(&scene), 96_000.0, Params::new());
    assert_eq!(frames.len(), 1);
    let (_, f, raw) = &frames[0];
    assert_eq!(num_of(f, "type"), 8.0);
    assert_eq!(num_of(f, "mmsi"), 0x3FFF_FFFF as f64);
    assert!(raw[5..].iter().all(|&v| v == 0xFF));
}

/// Bursts of type 1 at `snr`, each its own MMSI, alternate channels, 60 ms
/// apart: how many of `n` come through, and none that were not sent.
fn heard_of(n: usize, snr: f64, offset_hz: f64, seed: u64, rate: f64, centre_hz: f64) -> (usize, Vec<(String, Value)>) {
    let bursts: Vec<Burst> = (0..n)
        .map(|i| {
            let mut b = Burst::new(
                0.05 + 0.06 * i as f64,
                if i % 2 == 0 { Channel::A } else { Channel::B },
                position(235100000 + i as i64, 55.0 + i as f64 * 0.001, -5.0),
            );
            b.offset_hz = offset_hz;
            b
        })
        .collect();
    let scene = Scene { rate, duration_s: 0.1 + 0.06 * n as f64, centre_hz, snr_db: Some(snr), seed, bursts };
    let params = Params::new().with("offsetHz", Value::Num(centre_hz));
    let (frames, _) = decode(&scene_iq(&scene), rate, params);
    let mut seen = std::collections::BTreeSet::new();
    for (_, f, _) in &frames {
        let m = mmsi_of(f);
        assert!((235100000..235100000 + n as i64).contains(&m), "a frame never sent: {f:?}");
        assert!(seen.insert(m), "{m} twice");
    }
    (seen.len(), frames.last().map(|x| x.1.clone()).unwrap_or_default())
}

#[test]
fn the_snr_sweep() {
    // Eb/N0 (SNR in 9.6 kHz), 100 type-1 bursts a point. Measured on
    // 28/10/2026 with the on-air bit order fixed: 3 at 10 dB, 49 at 12, 88
    // at 14, 98 at 16, all at 20 (42/92/100 before the fix: the same
    // decoder on other bits, so one seed's luck either way) - a
    // non-coherent receiver's figures. Slicing the discriminator's samples
    // (the first version) needed about 1 dB more for each; a channel filter
    // of 6 kHz instead of 7.2 measured the same, and 5 kHz 2 dB worse.
    let floor = [(10.0, 0.0), (12.0, 0.35), (14.0, 0.82), (16.0, 0.95), (20.0, 1.0)];
    for (snr, least) in floor {
        let n = 100;
        let (got, last) = heard_of(n, snr, 0.0, 11 + snr as u64, 96_000.0, 0.0);
        if show() {
            println!("Eb/N0 {snr:5.1} dB: {got}/{n} {:?}", field(&last, "snrDb"));
        }
        assert!(got as f64 >= least * n as f64, "{snr} dB: {got}/{n}, want {least}");
    }
}

#[test]
fn the_snr_reported_is_the_bursts() {
    for snr in [12.0, 20.0, 30.0] {
        let (_, f) = heard_of(10, snr, 0.0, 21, 96_000.0, 0.0);
        let got = num_of(&f, "snrDb");
        assert!((got - snr).abs() < 3.0, "sent at {snr} dB, reported {got}");
    }
}

#[test]
fn the_frequency_error_sweep() {
    // M.1371 holds a transmitter to ±500 Hz; a cheap receiver adds its own
    // ppm (1 ppm at 162 MHz is 162 Hz). ±2 kHz is heard and measured.
    // Beyond ±1.5 kHz the burst begins to leave the ±7.2 kHz channel
    // filter: at ±2 kHz between three in five and three in four were
    // heard (18 dB).
    for off in [-2000.0, -1500.0, -1000.0, -500.0, 0.0, 500.0, 1000.0, 1500.0, 2000.0] {
        let (got, last) = heard_of(40, 18.0, off, 31, 96_000.0, 0.0);
        let meas = num_of(&last, "offsetHz");
        if show() {
            println!("offset {off:6.0} Hz: {got}/40, measured {meas}");
        }
        let least = if f64::abs(off) > 1500.0 { 20 } else { 39 };
        assert!(got >= least, "{off} Hz: {got}/40");
        assert!((meas - off).abs() < 200.0, "{off} Hz measured as {meas}");
    }
}

#[test]
fn the_rates_radio_feeds() {
    // A receiver's own rate with 162.000 MHz off its centre (the session's
    // offsetHz), and a bank channel's.
    for (rate, centre) in [(2_400_000.0, 300_000.0), (2_048_000.0, -400_000.0), (240_000.0, 0.0), (96_000.0, 0.0), (1_000_000.0, 100_000.0)] {
        let (got, _) = heard_of(10, 25.0, 0.0, 41, rate, centre);
        assert_eq!(got, 10, "{rate} S/s, 162 MHz at {centre} Hz");
    }
}

#[test]
fn collisions() {
    let strong = position(235200001, 55.1, -5.1);
    let weak = position(235200002, 55.2, -5.2);
    // The stronger by 12 dB, starting half a burst later: its training
    // finds the slicers over the weaker one, and it is captured (FM's
    // capture effect); the weaker, cut in half, is not.
    let mut a = Burst::new(0.10, Channel::A, weak.clone());
    a.level_db = -12.0;
    let b = Burst::new(0.1135, Channel::A, strong.clone());
    // Equal and fully overlapping on B: neither may be read as anything
    // else, whether or not either is read at all.
    let c = Burst::new(0.10, Channel::B, position(235200003, 55.3, -5.3));
    let mut d = Burst::new(0.1005, Channel::B, position(235200004, 55.4, -5.4));
    d.offset_hz = 700.0;
    // Back to back on A, a millisecond apart, later on.
    let e = Burst::new(0.30, Channel::A, position(235200005, 55.5, -5.5));
    let f = Burst::new(0.30 + e.duration_s() + 0.001, Channel::A, position(235200006, 55.6, -5.6));
    let scene = Scene { rate: 96_000.0, duration_s: 0.45, centre_hz: 0.0, snr_db: Some(25.0), seed: 9, bursts: vec![a, b, c, d, e, f] };
    let (frames, _) = decode(&scene_iq(&scene), 96_000.0, Params::new());
    let got: Vec<i64> = frames.iter().map(|(_, f, _)| mmsi_of(f)).collect();
    assert!(got.contains(&235200001), "{got:?}");
    assert!(!got.contains(&235200002), "{got:?}");
    assert!(got.contains(&235200005) && got.contains(&235200006), "{got:?}");
    for m in &got {
        assert!((235200001..=235200006).contains(m), "{got:?}");
    }
}

#[test]
fn an_empty_channel_prints_nothing() {
    // Three minutes of noise on both channels, ten slicers: no message.
    let rate = 96_000.0;
    let mut d = Ais::new();
    let mut sink = Sink::new();
    for k in 0..18 {
        let scene = Scene { rate, duration_s: 10.0, centre_hz: 0.0, snr_db: Some(10.0), seed: 100 + k, bursts: vec![] };
        let iq = scene_iq(&scene);
        let base = (k as usize * iq.len()) as u64;
        for (i, c) in iq.chunks(9600).enumerate() {
            let index = base + (i * 9600) as u64;
            d.feed(Input::Iq { samples: c, rate, t0: index as f64 / rate, index }, &mut sink).unwrap();
        }
    }
    assert!(sink.items.is_empty(), "{:?}", sink.items);
}

#[test]
fn a_receivers_noise_with_spurs_and_overload_prints_nothing() {
    // What a real RSP adds to white noise: carriers inside and beside both
    // channels (spurs), a DC spike between them, and the samples clipped
    // as an overloaded front end clips them. Three minutes, both channels.
    let rate = 96_000.0;
    let mut d = Ais::new();
    let mut sink = Sink::new();
    let spurs = [(-25_000.0, 0.5), (-21_300.0, 0.2), (25_000.0 + 1_800.0, 0.3), (0.0, 1.0), (30_000.0, 2.0)];
    let mut n0 = 0u64;
    for k in 0..18 {
        let scene = Scene { rate, duration_s: 10.0, centre_hz: 0.0, snr_db: Some(0.0), seed: 300 + k, bursts: vec![] };
        let mut iq = scene_iq(&scene);
        for (i, z) in iq.iter_mut().enumerate() {
            let t = (n0 + i as u64) as f64 / rate;
            for (f, a) in spurs {
                let p = 2.0 * std::f64::consts::PI * f * t;
                *z += Complex32::new((a * p.cos()) as f32, (a * p.sin()) as f32);
            }
            // Clipped, as an ADC at full scale: 1.5 x the noise's RMS.
            let lim = 1.5f32;
            *z = Complex32::new(z.re.clamp(-lim, lim), z.im.clamp(-lim, lim));
        }
        for (i, c) in iq.chunks(9600).enumerate() {
            let index = n0 + (i * 9600) as u64;
            d.feed(Input::Iq { samples: c, rate, t0: index as f64 / rate, index }, &mut sink).unwrap();
        }
        n0 += iq.len() as u64;
    }
    assert!(sink.items.is_empty(), "{:?}", sink.items);
}

#[test]
fn the_cpu_it_costs() {
    // Seconds of one core per second of input, release build.
    let mut worst: f64 = 0.0;
    for rate in [96_000.0, 240_000.0, 2_048_000.0, 2_400_000.0] {
        let scene = Scene { rate, duration_s: 2.0, centre_hz: 0.0, snr_db: Some(20.0), seed: 1,
                            bursts: (0..20).map(|i| Burst::new(0.05 + 0.09 * i as f64, if i % 2 == 0 { Channel::A } else { Channel::B }, position(235300000 + i, 55.0, -5.0))).collect() };
        let iq = scene_iq(&scene);
        let start = Instant::now();
        let (frames, _) = decode(&iq, rate, Params::new());
        let cpu = start.elapsed().as_secs_f64() / 2.0;
        println!("AIS at {:7.0} kS/s: {:5.1} ms of CPU a second, {} frames", rate / 1e3, cpu * 1e3, frames.len());
        assert!(frames.len() == 20, "{rate}: {}", frames.len());
        worst = worst.max(cpu);
    }
    // Generous for a loaded CI host; on shack it is far below.
    if cfg!(not(debug_assertions)) {
        assert!(worst < 0.25, "{worst}");
    }
}

#[test]
fn real_recordings() {
    let Ok(dir) = std::env::var("SHACK_AIS_DIR") else {
        println!("SHACK_AIS_DIR is not set: no real recordings decoded");
        return;
    };
    let mut any = false;
    for e in std::fs::read_dir(&dir).unwrap() {
        let p = e.unwrap().path();
        let ext = p.extension().and_then(|x| x.to_str()).unwrap_or("");
        if ext != "cf32" && ext != "cs16" {
            continue;
        }
        let meta: J = serde_json::from_str(&std::fs::read_to_string(p.with_extension("json")).unwrap()).unwrap();
        let rate = meta["rate"].as_f64().unwrap();
        let bytes = std::fs::read(&p).unwrap();
        let iq: Vec<Complex32> = if ext == "cf32" {
            bytes.chunks_exact(8).map(|c| Complex32::new(f32::from_le_bytes(c[..4].try_into().unwrap()), f32::from_le_bytes(c[4..].try_into().unwrap()))).collect()
        } else {
            bytes.chunks_exact(4).map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0, i16::from_le_bytes([c[2], c[3]]) as f32 / 32768.0)).collect()
        };
        let params = Params::new().with("offsetHz", Value::Num(meta["offsetHz"].as_f64().unwrap_or(0.0)));
        let (frames, _) = decode(&iq, rate, params);
        for (_, f, _) in &frames {
            if let Some(Value::List(s)) = field(f, "nmea") {
                for v in s {
                    println!("{v:?}");
                }
            }
        }
        let least = meta["minFrames"].as_u64().unwrap_or(1) as usize;
        println!("{}: {} frames", p.display(), frames.len());
        assert!(frames.len() >= least, "{}: {} frames, want {least}", p.display(), frames.len());
        any = true;
    }
    assert!(any, "no .cf32 or .cs16 captures in {dir}");
}
