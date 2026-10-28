//! The ADS-B decoder: messages read bit for bit as an implementation that
//! is not ours reads them (testdata/adsb/golden.json, from pyModeS by
//! tools/adsb_golden.py: "The 1090MHz Riddle"'s worked examples, real
//! traffic, CPR pairs, our own writer's output), the tracker on a real
//! two-thousand-message stream, then over the air through the test
//! transmitter: aircraft at the rates Radio feeds through the IF filters
//! receivers have, an SNR, timing and frequency-error sweep, overlapping
//! replies, a mended bit, and - the AIS decoder's lesson - noise, Mode A/C
//! fruit and random Mode S at realistic rates, which must show no aircraft
//! at all. And the CPU it costs.
//!
//! Real recordings: tests/adsb_real.rs (ignored). `ADSB_SHOW=1` prints the
//! sweeps.

use rustfft::num_complex::Complex32;
use serde_json::Value as J;
use shack_sdr::adsb::cpr::{self, distance_km, Cpr};
use shack_sdr::adsb::msg::{self, Body, Es, Make, Msg};
use shack_sdr::adsb::track::{Config, Tracker};
use shack_sdr::adsb::tx::{fruit, garbage, scene_iq, Reply, ReplyKind, Scene};
use shack_sdr::adsb::{crc, Adsb};
use shack_sdr::decoder::{Decoder, Input, Output, Params, Sink, Value};
use shack_sdr::testsig::Rng;
use std::collections::BTreeMap;

const DATA: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/adsb");

fn golden() -> J {
    serde_json::from_str(&std::fs::read_to_string(format!("{DATA}/golden.json")).unwrap()).unwrap()
}

fn show() -> bool {
    std::env::var("ADSB_SHOW").is_ok()
}

fn bytes(h: &str) -> Vec<u8> {
    msg::from_hex(h).unwrap()
}

/// A message's fields in pyModeS's terms as tools/adsb_golden.py maps
/// them, for the ones it reads.
fn msg_fields(m: &Msg) -> BTreeMap<String, J> {
    let mut f = BTreeMap::new();
    let mut put = |k: &str, v: J| {
        f.insert(k.to_string(), v);
    };
    put("df", J::from(m.df));
    put("icao", J::from(format!("{:06X}", m.icao)));
    let opt_i = |v: Option<i32>| v.map_or(J::Null, J::from);
    let opt_f = |v: Option<f64>| v.map_or(J::Null, J::from);
    match &m.body {
        Body::AllCall { ca, .. } => put("ca", J::from(*ca)),
        Body::Altitude { fs, alt_ft } => {
            put("fs", J::from(*fs));
            put("altitude", opt_i(*alt_ft));
        }
        Body::Identity { fs, squawk } => {
            put("fs", J::from(*fs));
            put("squawk", J::from(squawk.clone()));
        }
        Body::Es { tc, es, .. } => {
            put("tc", J::from(*tc));
            match es {
                Es::Ident { category, callsign } => {
                    put("callsign", J::from(callsign.clone()));
                    put("category", category.clone().map_or(J::Null, J::from));
                }
                Es::Airborne { alt_ft, cpr, .. } => {
                    put("altitude", opt_i(*alt_ft));
                    put("cprOdd", J::from(cpr.odd));
                    put("cprLat", J::from(cpr.lat));
                    put("cprLon", J::from(cpr.lon));
                }
                Es::Surface { speed_kn, track, cpr } => {
                    put("gs", opt_f(*speed_kn));
                    put("track", opt_f(*track));
                    put("cprOdd", J::from(cpr.odd));
                    put("cprLat", J::from(cpr.lat));
                    put("cprLon", J::from(cpr.lon));
                }
                Es::Velocity(v) => {
                    if v.subtype <= 2 {
                        put("gs", opt_f(v.gs_kn.map(|g| g.floor())));
                        put("track", opt_f(v.track));
                    } else {
                        put("airspeed", opt_f(v.airspeed_kn));
                        put("airspeedType", J::from(if v.tas { "tas" } else { "ias" }));
                        put("heading", opt_f(v.heading));
                    }
                    put("vrate", opt_i(v.vrate_fpm));
                    put("vrateSource", J::from(if v.vrate_baro { "baro" } else { "geo" }));
                    put("geoMinusBaro", opt_i(v.geo_minus_baro_ft));
                }
                Es::Status { emergency, squawk } => {
                    put("emergency", J::from(*emergency));
                    put("squawk", J::from(squawk.clone()));
                }
                Es::OpStatus { version, .. } => put("version", J::from(*version)),
                Es::Other => {}
            }
        }
    }
    f
}

fn same(a: &J, b: &J) -> bool {
    match (a, b) {
        (J::Number(x), J::Number(y)) => (x.as_f64().unwrap() - y.as_f64().unwrap()).abs() < 1e-6,
        _ => a == b,
    }
}

/// Every field pyModeS gave that we read, read the same.
fn assert_reads_as(name: &str, got: &BTreeMap<String, J>, want: &J) {
    for (k, w) in want.as_object().unwrap() {
        let Some(g) = got.get(k) else { continue };
        assert!(same(g, w), "{name}: {k} is {g}, pyModeS read {w}");
    }
}

#[test]
fn messages_heard_by_real_receivers_read_as_pymodes_reads_them() {
    let g = golden();
    let heard = g["heard"].as_array().unwrap();
    assert!(heard.len() > 250, "{}", heard.len());
    let mut fields = 0;
    for c in heard {
        let h = c["hex"].as_str().unwrap();
        let m = msg::parse(&bytes(h)).unwrap_or_else(|| panic!("{h}: not read"));
        let got = msg_fields(&m);
        assert_reads_as(h, &got, &c["pyModeS"]);
        fields += c["pyModeS"].as_object().unwrap().keys().filter(|k| got.contains_key(*k)).count();
    }
    // Most of what pyModeS reads, we read: not a vacuous comparison.
    assert!(fields > 1500, "{fields}");
}

#[test]
fn the_riddles_examples() {
    // The CRC chapter's message is good; one bit turned over is not, and
    // the syndrome table names the bit.
    let mut m = bytes("8D406B902015A678D4D220AA4BDA");
    assert_eq!(crc::remainder(&m), 0);
    m[6] ^= 0x10;
    let rem = crc::remainder(&m);
    assert_ne!(rem, 0);
    assert_eq!((0..112).filter(|&i| crc::syndrome(i) == rem).collect::<Vec<_>>(), vec![6 * 8 + 3]);
    // Every single-bit syndrome is distinct, so a mend is never ambiguous.
    let s = crc::syndromes();
    let mut u = s.to_vec();
    u.sort();
    u.dedup();
    assert_eq!(u.len(), 112);

    let m = msg::parse(&bytes("8D4840D6202CC371C32CE0576098")).unwrap();
    assert!(matches!(&m.body, Body::Es { es: Es::Ident { callsign, .. }, .. } if callsign == "KLM1023"));
    // The position chapter's pair: even newer.
    let even = Cpr { odd: false, lat: 93000, lon: 51372 };
    let odd = Cpr { odd: true, lat: 74158, lon: 50194 };
    let (lat, lon) = cpr::global_airborne(even, odd, false).unwrap();
    assert!((lat - 52.25720).abs() < 1e-4 && (lon - 3.91937).abs() < 1e-4, "{lat} {lon}");
    let (lat, lon) = cpr::local(even, false, 52.258, 3.918);
    assert!((lat - 52.25720).abs() < 1e-4 && (lon - 3.91937).abs() < 1e-4, "{lat} {lon}");
    // The velocity chapter: 159 kn on 182.88, down 832 ft/min, GNSS 550 ft above baro.
    let Body::Es { es: Es::Velocity(v), .. } = msg::parse(&bytes("8D485020994409940838175B284F")).unwrap().body else { panic!() };
    assert!((v.gs_kn.unwrap() - 159.20).abs() < 0.01 && (v.track.unwrap() - 182.88).abs() < 0.01);
    assert_eq!((v.vrate_fpm, v.geo_minus_baro_ft), (Some(-832), Some(550)));
}

#[test]
fn cpr_positions_are_placed_where_pymodes_places_them() {
    let g = golden();
    let pairs = g["pairs"].as_array().unwrap();
    assert!(pairs.len() >= 10, "{}", pairs.len());
    let cpr_of = |h: &str| match msg::parse(&bytes(h)).unwrap().body {
        Body::Es { es: Es::Airborne { cpr, .. }, .. } | Body::Es { es: Es::Surface { cpr, .. }, .. } => cpr,
        b => panic!("{h}: {b:?}"),
    };
    for p in pairs {
        let (e, o) = (cpr_of(p["even"].as_str().unwrap()), cpr_of(p["odd"].as_str().unwrap()));
        let (lat, lon) = cpr::global_airborne(e, o, p["newer"] == "odd").unwrap();
        assert!((lat - p["lat"].as_f64().unwrap()).abs() < 1e-6 && (lon - p["lon"].as_f64().unwrap()).abs() < 1e-6, "{p}: {lat} {lon}");
    }
    for c in g["local"].as_array().unwrap() {
        let r = c["ref"].as_array().unwrap();
        let (lat, lon) = cpr::local(cpr_of(c["hex"].as_str().unwrap()), false, r[0].as_f64().unwrap(), r[1].as_f64().unwrap());
        assert!((lat - c["lat"].as_f64().unwrap()).abs() < 1e-6 && (lon - c["lon"].as_f64().unwrap()).abs() < 1e-6, "{c}: {lat} {lon}");
    }
    for c in g["surface"].as_array().unwrap() {
        let r = c["ref"].as_array().unwrap();
        let (lat, lon) = cpr::local(cpr_of(c["hex"].as_str().unwrap()), true, r[0].as_f64().unwrap(), r[1].as_f64().unwrap());
        assert!((lat - c["lat"].as_f64().unwrap()).abs() < 1e-6 && (lon - c["lon"].as_f64().unwrap()).abs() < 1e-6, "{c}: {lat} {lon}");
    }
    let sp = &g["surfacePair"];
    let r = sp["ref"].as_array().unwrap();
    let (lat, lon) = cpr::global_surface(cpr_of(sp["even"].as_str().unwrap()), cpr_of(sp["odd"].as_str().unwrap()), true, r[0].as_f64().unwrap(), r[1].as_f64().unwrap()).unwrap();
    assert!((lat - sp["lat"].as_f64().unwrap()).abs() < 1e-6 && (lon - sp["lon"].as_f64().unwrap()).abs() < 1e-6, "{lat} {lon}");
}

/// What we write, one of each kind with every field set: tools/adsb_golden.py
/// has pyModeS read each, and `made_messages_...` holds us to both.
fn made_cases() -> Vec<(&'static str, Make, serde_json::Value)> {
    use serde_json::json;
    let icao = 0x4CA2B3;
    vec![
        ("ident A3", Make::Ident { icao, tc: 4, ca: 3, callsign: "BAW123".into() }, json!({"tc": 4, "callsign": "BAW123", "category": "A3"})),
        ("ident C1", Make::Ident { icao: 0x400A1B, tc: 2, ca: 1, callsign: "FIRE1".into() }, json!({"tc": 2, "callsign": "FIRE1", "category": "C1"})),
        ("airborne even", Make::Airborne { icao, tc: 11, alt_ft: Some(38000), odd: false, lat: 55.5, lon: -4.6 }, json!({"altitude": 38000, "cprOdd": false})),
        ("airborne odd", Make::Airborne { icao, tc: 11, alt_ft: Some(38000), odd: true, lat: 55.5, lon: -4.6 }, json!({"altitude": 38000, "cprOdd": true})),
        ("airborne low", Make::Airborne { icao, tc: 9, alt_ft: Some(-975), odd: true, lat: 55.86, lon: -4.43 }, json!({"altitude": -975})),
        ("airborne gnss", Make::Airborne { icao, tc: 20, alt_ft: Some(1250), odd: false, lat: 55.86, lon: -4.43 }, json!({"altitude": 1250, "tc": 20})),
        ("surface even", Make::Surface { icao, tc: 7, speed_kn: Some(17.0), track: Some(92.8125), odd: false, lat: 55.509, lon: -4.594 }, json!({"gs": 17, "track": 92.8125})),
        ("surface odd", Make::Surface { icao, tc: 7, speed_kn: Some(17.0), track: Some(92.8125), odd: true, lat: 55.509, lon: -4.594 }, json!({"gs": 17, "track": 92.8125})),
        ("surface still", Make::Surface { icao, tc: 8, speed_kn: None, track: None, odd: false, lat: 55.509, lon: -4.594 }, json!({"track": null})),
        ("velocity nw", Make::Velocity { icao, subtype: 1, east_kn: -120.0, north_kn: 250.0, vrate_fpm: Some(-1600), baro: true, geo_minus_baro_ft: Some(300) }, json!({"vrate": -1600, "vrateSource": "baro", "geoMinusBaro": 300})),
        ("velocity se", Make::Velocity { icao, subtype: 1, east_kn: 300.0, north_kn: -40.0, vrate_fpm: Some(2944), baro: false, geo_minus_baro_ft: Some(-150) }, json!({"vrate": 2944, "vrateSource": "geo", "geoMinusBaro": -150})),
        ("velocity fast", Make::Velocity { icao, subtype: 2, east_kn: 1200.0, north_kn: 0.0, vrate_fpm: Some(0), baro: true, geo_minus_baro_ft: None }, json!({"gs": 1200, "track": 90.0})),
        ("airspeed tas", Make::Airspeed { icao, subtype: 3, heading: Some(243.984375), airspeed_kn: Some(375.0), tas: true, vrate_fpm: Some(-2304), baro: true }, json!({"airspeed": 375, "airspeedType": "tas", "heading": 243.984375, "vrate": -2304})),
        ("airspeed ias", Make::Airspeed { icao, subtype: 3, heading: Some(10.546875), airspeed_kn: Some(180.0), tas: false, vrate_fpm: None, baro: true }, json!({"airspeed": 180, "airspeedType": "ias"})),
        ("status", Make::Status { icao, emergency: 0, squawk: "6513".into() }, json!({"emergency": 0, "squawk": "6513"})),
        ("hijack", Make::Status { icao, emergency: 5, squawk: "7500".into() }, json!({"emergency": 5, "squawk": "7500"})),
        ("mayday", Make::Status { icao, emergency: 1, squawk: "7700".into() }, json!({"emergency": 1, "squawk": "7700"})),
        ("opstatus", Make::OpStatus { icao, version: 2, surface: false }, json!({"tc": 31})),
        ("all-call", Make::AllCall { icao, ca: 5, iid: 0 }, json!({"df": 11, "ca": 5, "icao": "4CA2B3"})),
        ("df4", Make::Altitude { df: 4, icao, fs: 0, alt_ft: Some(38000) }, json!({"df": 4, "altitude": 38000, "icao": "4CA2B3"})),
        ("df20", Make::Altitude { df: 20, icao, fs: 1, alt_ft: Some(2500) }, json!({"df": 20, "altitude": 2500, "icao": "4CA2B3"})),
        ("df5", Make::Identity { df: 5, icao, fs: 2, squawk: "7700".into() }, json!({"df": 5, "squawk": "7700", "icao": "4CA2B3"})),
        ("df21", Make::Identity { df: 21, icao, fs: 0, squawk: "1200".into() }, json!({"df": 21, "squawk": "1200", "icao": "4CA2B3"})),
    ]
}

#[test]
#[ignore]
fn write_made_cases() {
    // Run once when the writer changes, then tools/adsb_golden.py.
    let cases: Vec<J> = made_cases()
        .into_iter()
        .map(|(name, mk, _)| serde_json::json!({"name": name, "hex": msg::hex(&msg::encode(&mk).unwrap())}))
        .collect();
    std::fs::write(format!("{DATA}/made.json"), serde_json::to_string_pretty(&serde_json::json!({"cases": cases})).unwrap()).unwrap();
}

#[test]
fn made_messages_are_written_bit_for_bit_and_pymodes_reads_what_we_meant() {
    let g = golden();
    let made = g["made"].as_array().unwrap();
    let cases = made_cases();
    assert_eq!(made.len(), cases.len(), "made.json is out of date: run write_made_cases and the tool");
    for ((name, mk, meant), c) in cases.iter().zip(made) {
        assert_eq!(c["name"], *name);
        let b = msg::encode(mk).unwrap();
        assert_eq!(msg::hex(&b), c["hex"].as_str().unwrap(), "{name}: the writer changed");
        // pyModeS - not us - reads what we meant to write.
        let theirs = &c["pyModeS"];
        for (k, w) in meant.as_object().unwrap() {
            assert!(same(&theirs[k], w), "{name}: pyModeS reads {k} as {}, we wrote {w}", theirs[k]);
        }
        // And we read what pyModeS reads.
        assert_reads_as(name, &msg_fields(&msg::parse(&b).unwrap()), theirs);
        // Every Mode S format has good parity as written.
        let rem = crc::remainder(&b);
        match mk {
            Make::Altitude { icao, .. } | Make::Identity { icao, .. } => assert_eq!(rem, *icao, "{name}"),
            _ => assert_eq!(rem, 0, "{name}"),
        }
    }
    // A position written and read back lands within CPR's resolution.
    for (lat, lon) in [(55.5, -4.6), (-33.9, 151.2), (0.01, 179.99), (71.0, 25.8)] {
        for odd in [false, true] {
            let c = cpr::encode(lat, lon, odd, false);
            let (la, lo) = cpr::local(c, false, lat + 0.5, lon - 0.5);
            assert!(distance_km(lat, lon, la, lo) < 0.01, "{lat} {lon} {odd}: {la} {lo}");
        }
    }
}

// ------------------------------------------------------------------ the tracker on real traffic

fn heard_all(t: &mut Tracker, stream: &[(f64, Vec<u8>)]) -> usize {
    let mut taken = 0;
    for (ts, b) in stream {
        let rem = crc::remainder(b);
        let Some(m) = msg::parse(b) else { continue };
        let ok = match m.df {
            17 | 18 => rem == 0,
            11 => rem == 0,
            _ => t.confirmed(m.icao, *ts),
        };
        if ok {
            t.heard(&m, *ts, 1e-3, matches!(m.df, 17 | 18));
            taken += 1;
        }
    }
    taken
}

#[test]
fn the_tracker_on_a_real_stream() {
    let g = golden();
    let st = g["stream"]["station"].as_array().unwrap();
    let station = (st[0].as_f64().unwrap(), st[1].as_f64().unwrap());
    let stream: Vec<(f64, Vec<u8>)> = g["stream"]["messages"].as_array().unwrap().iter().map(|r| (r[0].as_f64().unwrap(), bytes(r[1].as_str().unwrap()))).collect();
    assert_eq!(stream.len(), 2000);
    let mut t = Tracker::new(Config { station: Some(station), max_range_km: 500.0, every_s: 1.0 });
    heard_all(&mut t, &stream);
    let end = stream.last().unwrap().0;
    let shown = t.due(end);
    let get = |f: &[(String, Value)], k: &str| f.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone()).unwrap();
    let mut ids = Vec::new();
    for (_, f) in &shown {
        let (Value::Num(lat), Value::Num(lon)) = (get(f, "lat"), get(f, "lon")) else { panic!("{f:?}") };
        assert!(distance_km(station.0, station.1, lat, lon) < 400.0, "{f:?}");
        if let Value::Num(a) = get(f, "altBaro") {
            assert!((0.0..=45000.0).contains(&a), "{f:?}");
        }
        ids.push(get(f, "icao"));
    }
    if show() {
        for (_, f) in &shown {
            println!("{f:?}");
        }
    }
    // pyModeS's stream is the aircraft the Riddle follows, 406B90
    // (EZY85MH), at FL360 over the Netherlands for twelve minutes: shown,
    // named, at its level and speed, every message counted, never a jump.
    assert_eq!(ids, vec![Value::Text("406B90".into())]);
    let f = &shown[0].1;
    assert_eq!(get(f, "callsign"), Value::Text("EZY85MH".into()));
    assert_eq!(get(f, "altBaro"), Value::Num(36000.0));
    assert_eq!(get(f, "messages"), Value::Num(2000.0));
    assert!(matches!(get(f, "gs"), Value::Num(g) if (400.0..550.0).contains(&g)), "{f:?}");
    assert_eq!(t.rejected.jump, 0, "{:?}", t.rejected);

    // The same stream from a station on the other side of the world shows
    // nothing: every fix is out of range.
    let mut far = Tracker::new(Config { station: Some((-33.9, 151.2)), max_range_km: 500.0, every_s: 1.0 });
    heard_all(&mut far, &stream);
    assert!(far.due(end).is_empty());
    assert!(far.rejected.range > 0);
}

// ------------------------------------------------------------------ the tracker's rules, one at a time

const STATION: (f64, f64) = (55.509, -4.594); // EGPK

fn es(mk: Make) -> Msg {
    msg::parse(&msg::encode(&mk).unwrap()).unwrap()
}

fn pos(icao: u32, odd: bool, lat: f64, lon: f64) -> Msg {
    es(Make::Airborne { icao, tc: 11, alt_ft: Some(12000), odd, lat, lon })
}

fn tracker() -> Tracker {
    Tracker::new(Config { station: Some(STATION), max_range_km: 500.0, every_s: 0.0 })
}

#[test]
fn an_aircraft_is_shown_only_when_it_has_earned_it() {
    let a = 0x400001;
    // One pair: a fix, but not seen twice.
    let mut t = tracker();
    t.heard(&pos(a, false, 55.6, -4.7), 0.0, 1.0, true);
    t.heard(&pos(a, true, 55.6, -4.7), 0.5, 1.0, true);
    assert!(t.due(1.0).is_empty(), "one fix is not enough");
    t.heard(&pos(a, false, 55.601, -4.7), 1.0, 1.0, true);
    assert_eq!(t.due(1.5).len(), 1, "a fix seen twice is");

    // A pair more than 10 s apart makes no fix.
    let mut t = tracker();
    t.heard(&pos(a, false, 55.6, -4.7), 0.0, 1.0, true);
    t.heard(&pos(a, true, 55.6, -4.7), 10.5, 1.0, true);
    t.heard(&pos(a, false, 55.6, -4.7), 21.0, 1.0, true);
    assert!(t.due(22.0).is_empty());

    // Out of range: never shown, however often.
    let mut t = tracker();
    for k in 0..10 {
        t.heard(&pos(a, k % 2 == 1, 60.4, 5.3), k as f64, 1.0, true); // Bergen, 600 km
    }
    assert!(t.due(11.0).is_empty());
    assert!(t.rejected.range > 0);

    // A second fix 100 km from the first, a second later: neither believed.
    let mut t = tracker();
    t.heard(&pos(a, false, 55.6, -4.7), 0.0, 1.0, true);
    t.heard(&pos(a, true, 55.6, -4.7), 0.5, 1.0, true);
    t.heard(&pos(a, false, 56.5, -4.7), 1.5, 1.0, true);
    assert!(t.due(2.0).is_empty());
    assert_eq!(t.rejected.first_fix, 1);

    // Corrected messages update an aircraft; they never confirm one.
    let mut t = tracker();
    for (k, odd) in [false, true, false, true].into_iter().enumerate() {
        t.heard(&pos(a, odd, 55.6, -4.7), k as f64 * 0.5, 1.0, false);
    }
    assert!(t.due(3.0).is_empty());
    assert!(!t.confirmed(a, 3.0));

    // 1200 kn is not believed; 450 is.
    let mut t = tracker();
    t.heard(&pos(a, false, 55.6, -4.7), 0.0, 1.0, true);
    t.heard(&pos(a, true, 55.6, -4.7), 0.5, 1.0, true);
    t.heard(&pos(a, false, 55.6, -4.7), 1.0, 1.0, true);
    t.heard(&es(Make::Velocity { icao: a, subtype: 2, east_kn: 1200.0, north_kn: 0.0, vrate_fpm: None, baro: true, geo_minus_baro_ft: None }), 1.2, 1.0, true);
    assert_eq!(t.rejected.speed, 1);
    t.heard(&es(Make::Velocity { icao: a, subtype: 1, east_kn: 450.0, north_kn: 0.0, vrate_fpm: Some(-640), baro: true, geo_minus_baro_ft: None }), 1.4, 1.0, true);
    let f = &t.due(2.0)[0].1;
    assert!(f.contains(&("gs".to_string(), Value::Num(450.0))) && f.contains(&("vrate".to_string(), Value::Num(-640.0))), "{f:?}");

    // Once shown, a position that jumps is dropped and the last good kept.
    t.heard(&pos(a, true, 57.0, -4.7), 2.0, 1.0, true);
    assert_eq!(t.rejected.jump, 1);
    let f = &t.due(3.0)[0].1;
    let lat = f.iter().find(|(n, _)| n == "lat").unwrap();
    assert!(matches!(lat.1, Value::Num(x) if (x - 55.6).abs() < 1e-3), "{f:?}");
}

#[test]
fn the_address_formats_reach_only_confirmed_aircraft() {
    let a = 0x400002;
    let mut t = tracker();
    let df4 = |icao, ft| es(Make::Altitude { df: 4, icao, fs: 0, alt_ft: Some(ft) });
    // Unknown: ignored, and makes no aircraft.
    t.heard(&df4(a, 5000), 0.0, 1.0, false);
    assert!(!t.confirmed(a, 0.0));
    // Two clean DF17s confirm the address.
    t.heard(&pos(a, false, 55.6, -4.7), 1.0, 1.0, true);
    assert!(!t.confirmed(a, 1.0));
    t.heard(&pos(a, true, 55.6, -4.7), 1.5, 1.0, true);
    assert!(t.confirmed(a, 1.5));
    t.heard(&pos(a, false, 55.6, -4.7), 2.0, 1.0, true);
    t.heard(&df4(a, 12500), 2.5, 1.0, false);
    t.heard(&es(Make::Identity { df: 5, icao: a, fs: 0, squawk: "7700".into() }), 2.6, 1.0, false);
    let f = &t.due(3.0)[0].1;
    for want in [("altBaro", Value::Num(12500.0)), ("squawk", Value::Text("7700".into())), ("emergency", Value::Text("general".into())), ("messages", Value::Num(5.0))] {
        assert!(f.contains(&(want.0.to_string(), want.1.clone())), "{want:?} in {f:?}");
    }
    // And no longer once DF17 has been silent two minutes.
    assert!(!t.confirmed(a, 200.0));
}

#[test]
fn a_surface_pair_is_placed_by_the_station() {
    let a = 0x400003;
    let mut t = tracker();
    for (k, odd) in [false, true, false].into_iter().enumerate() {
        t.heard(&es(Make::Surface { icao: a, tc: 7, speed_kn: Some(12.0), track: Some(310.0), odd, lat: 55.5072, lon: -4.5869 }), k as f64 * 2.0, 1.0, true);
    }
    let f = &t.due(5.0)[0].1;
    let num = |k: &str| match f.iter().find(|(n, _)| n == k).unwrap().1 {
        Value::Num(x) => x,
        ref v => panic!("{k}: {v:?}"),
    };
    assert!(distance_km(55.5072, -4.5869, num("lat"), num("lon")) < 0.02, "{f:?}");
    assert!(f.contains(&("onGround".to_string(), Value::Bool(true))));
}

// ------------------------------------------------------------------ on the air

/// An aircraft's squitters as a transponder sends them: positions twice a
/// second (even and odd alternately), velocity twice a second, its
/// identification every five seconds, flying straight.
struct Flight {
    icao: u32,
    callsign: &'static str,
    lat: f64,
    lon: f64,
    alt: i32,
    kn: f64,
    track: f64,
    level_db: f64,
}

fn squitters(f: &Flight, duration_s: f64, rng: &mut Rng) -> Vec<Reply> {
    let mut out = Vec::new();
    let mut t = 0.05 + rng.uniform() * 0.4;
    let mut k = 0;
    let (s, c) = f.track.to_radians().sin_cos();
    while t < duration_s - 0.001 {
        let km = f.kn * 1.852 / 3600.0 * t;
        let lat = f.lat + km * c / 111.2;
        let lon = f.lon + km * s / (111.2 * f.lat.to_radians().cos());
        let mk = match k % 4 {
            0 | 2 => Make::Airborne { icao: f.icao, tc: 11, alt_ft: Some(f.alt), odd: k % 8 >= 4, lat, lon },
            1 => Make::Velocity { icao: f.icao, subtype: 1, east_kn: f.kn * s, north_kn: f.kn * c, vrate_fpm: Some(0), baro: true, geo_minus_baro_ft: Some(200) },
            _ => {
                if k % 10 == 3 {
                    Make::Ident { icao: f.icao, tc: 4, ca: 3, callsign: f.callsign.into() }
                } else {
                    Make::AllCall { icao: f.icao, ca: 5, iid: 0 }
                }
            }
        };
        let mut r = Reply::mode_s(t, msg::encode(&mk).unwrap());
        r.level_db = f.level_db;
        r.offset_hz = (rng.uniform() - 0.5) * 200e3;
        r.phase = rng.uniform();
        out.push(r);
        t += 0.25 + rng.uniform() * 0.02;
        k += 1;
    }
    out
}

fn flights() -> Vec<Flight> {
    vec![
        Flight { icao: 0x400A01, callsign: "EZY64QT", lat: 55.62, lon: -4.95, alt: 34000, kn: 450.0, track: 135.0, level_db: 0.0 },
        Flight { icao: 0x4CA7B2, callsign: "RYR8LK", lat: 55.45, lon: -4.40, alt: 3500, kn: 160.0, track: 310.0, level_db: -6.0 },
        Flight { icao: 0x407F10, callsign: "LOG47", lat: 55.85, lon: -4.30, alt: 6000, kn: 210.0, track: 50.0, level_db: -8.0 },
    ]
}

fn run(iq: &[Complex32], rate: f64, params: Params, block: usize) -> (Vec<(f64, Vec<(String, Value)>)>, Adsb) {
    let mut d = Adsb::new();
    d.configure(&params).unwrap();
    let mut sink = Sink::new();
    for (i, c) in iq.chunks(block).enumerate() {
        let index = (i * block) as u64;
        d.feed(Input::Iq { samples: c, rate, t0: 1000.0 + index as f64 / rate, index }, &mut sink).unwrap();
    }
    let out = sink.take().into_iter().filter_map(|(t, o)| match o {
        Output::Frame { kind: "adsb", fields, .. } => Some((t, fields)),
        _ => None,
    }).collect();
    (out, d)
}

fn station() -> Params {
    Params::new().with("refLat", Value::Num(STATION.0)).with("refLon", Value::Num(STATION.1))
}

fn counter(d: &Adsb, k: &str) -> u64 {
    d.status().counters.iter().find(|(n, _)| *n == k).unwrap().1
}

fn last_states(out: &[(f64, Vec<(String, Value)>)]) -> BTreeMap<String, Vec<(String, Value)>> {
    out.iter().map(|(_, f)| (match &f[0].1 { Value::Text(s) => s.clone(), _ => panic!() }, f.clone())).collect()
}

/// The rates Radio feeds and the IF filters the receivers there have: an
/// RTL-SDR at 2.4 MS/s (no real IF filter; 2.4 MHz of band), an RSP at 6
/// and 8 MS/s (5 MHz), and 2 MS/s, the least the decoder takes.
#[test]
fn aircraft_through_the_air_at_the_rates_radio_feeds() {
    for (rate, bw) in [(2.0e6, 2.0e6), (2.4e6, 2.4e6), (6.0e6, 5.0e6), (8.0e6, 5.0e6)] {
        let mut rng = Rng::new(7);
        let replies: Vec<Reply> = flights().iter().flat_map(|f| squitters(f, 6.0, &mut rng)).collect();
        let iq = scene_iq(&Scene { rate, duration_s: 6.0, replies, snr_db: Some(25.0), if_bw_hz: Some(bw), seed: 3 });
        // Blocks of 100 ms, as Radio feeds, and an odd size, so replies
        // across block edges are read whole.
        for block in [(rate / 10.0) as usize, 77_777] {
            let (out, d) = run(&iq, rate, station(), block);
            let last = last_states(&out);
            assert_eq!(last.len(), 3, "{rate} {block}: {last:?} {:?}", d.status().counters);
            for f in flights() {
                let s = &last[&format!("{:06X}", f.icao)];
                let get = |k: &str| s.iter().find(|(n, _)| n == k).unwrap().1.clone();
                assert_eq!(get("callsign"), Value::Text(f.callsign.into()), "{rate}: {s:?}");
                assert_eq!(get("altBaro"), Value::Num(f.alt as f64));
                assert_eq!(get("altGeo"), Value::Num(f.alt as f64 + 200.0));
                let Value::Num(gs) = get("gs") else { panic!() };
                assert!((gs - f.kn).abs() < 1.5, "{gs}");
                let (Value::Num(lat), Value::Num(lon)) = (get("lat"), get("lon")) else { panic!() };
                // Where it has flown to by its last position.
                assert!(distance_km(f.lat, f.lon, lat, lon) < f.kn * 1.852 / 3600.0 * 6.0 + 0.1, "{rate}: {lat} {lon}");
            }
            assert_eq!(counter(&d, "rejectedJump"), 0);
        }
    }
}

/// One reply, `n` times, at `snr` dB and `rate`, sub-sample timing and
/// frequency error varied: how many came through the parity check.
fn heard_of(n: usize, snr: f64, rate: f64, bw: f64, offset_hz: f64, correct: bool) -> u64 {
    let b = msg::encode(&Make::Velocity { icao: 0x400A01, subtype: 1, east_kn: 300.0, north_kn: -100.0, vrate_fpm: Some(1024), baro: true, geo_minus_baro_ft: None }).unwrap();
    let replies = (0..n).map(|k| {
        let mut r = Reply::mode_s(0.0005 + k as f64 * 0.0004 + (k % 7) as f64 / 7.0 / rate, b.clone());
        r.offset_hz = offset_hz;
        r.phase = k as f64 * 0.37;
        r
    }).collect();
    let dur = 0.001 + n as f64 * 0.0004;
    let iq = scene_iq(&Scene { rate, duration_s: dur, replies, snr_db: Some(snr), if_bw_hz: Some(bw), seed: 11 });
    let (_, d) = run(&iq, rate, Params::new().with("correct", Value::Bool(correct)), (rate / 10.0) as usize);
    counter(&d, "df17")
}

#[test]
fn the_snr_sweep() {
    // SNR is a pulse's power over the noise in the whole sample band.
    let mut at = BTreeMap::new();
    for (rate, bw) in [(2.4e6, 2.4e6), (8.0e6, 5.0e6)] {
        for snr in [6.0, 7.0, 8.0, 10.0, 12.0, 15.0, 20.0] {
            let got = heard_of(200, snr, rate, bw, 0.0, false);
            if show() {
                println!("{:.1} MS/s, {snr:4.0} dB: {got}/200", rate / 1e6);
            }
            at.insert(((rate / 1e5) as u64, snr as u64), got);
        }
    }
    // At 2.4 MS/s magnitude slicing is noise-limited near 12 dB: slicing
    // at the exact timing, which no receiver knows, was clean for 74 of
    // 200 there.
    assert!(at[&(24, 15)] >= 165, "{at:?}");
    assert!(at[&(24, 20)] >= 198, "{at:?}");
    // The same air at 8 MS/s: the noise density is the receiver's, so a
    // reply's SNR per sample is 5.2 dB lower than at 2.4 MS/s - and still
    // it hears more, the wider filter keeping each pulse's edges.
    for (fast, slow) in [(7, 12), (10, 15), (15, 20)] {
        assert!(at[&(80, fast)] >= at[&(24, slow)], "{at:?}");
    }
}

#[test]
fn transponder_frequency_errors() {
    // Annex 10 allows ±1 MHz; a magnitude demodulator does not care while
    // the signal stays inside the receiver's filter.
    for off in [-400e3, -100e3, 0.0, 100e3, 400e3] {
        let got = heard_of(50, 20.0, 8.0e6, 5.0e6, off, false);
        assert!(got >= 49, "{off}: {got}");
    }
    for off in [-150e3, 150e3] {
        let got = heard_of(50, 20.0, 2.4e6, 2.4e6, off, false);
        assert!(got >= 48, "{off}: {got}");
    }
}

#[test]
fn overlapping_replies_the_stronger_is_read() {
    let strong = msg::encode(&Make::Ident { icao: 0x400A01, tc: 4, ca: 3, callsign: "STRONG1".into() }).unwrap();
    let weak = msg::encode(&Make::Ident { icao: 0x400A02, tc: 4, ca: 3, callsign: "WEAK2".into() }).unwrap();
    let mut got = 0;
    for k in 0..20 {
        let mut a = Reply::mode_s(0.0002, strong.clone());
        let mut b = Reply::mode_s(0.0002 + (10.0 + 4.0 * k as f64) * 1e-6, weak.clone());
        b.level_db = -12.0;
        b.phase = 0.3 * k as f64;
        a.phase = 0.1;
        let iq = scene_iq(&Scene { rate: 8e6, duration_s: 0.0005, replies: vec![a, b], snr_db: Some(30.0), if_bw_hz: Some(5e6), seed: k });
        let (_, d) = run(&iq, 8e6, Params::new(), 1 << 20);
        got += counter(&d, "df17");
    }
    assert!(got >= 18, "{got}");
}

#[test]
fn one_weak_bit_is_mended_and_a_strong_wrong_one_is_not() {
    let good = msg::encode(&Make::Ident { icao: 0x400A01, tc: 4, ca: 3, callsign: "MEND1".into() }).unwrap();
    let mut bad = good.clone();
    bad[7] ^= 0x04; // bit 61
    // Sent wrong outright: the wrong bit is as certain as any. Not mended.
    let r = |b: &Vec<u8>, lv: f64| {
        let mut x = Reply::mode_s(0.0002, b.clone());
        x.level_db = lv;
        x
    };
    let iq = scene_iq(&Scene { rate: 8e6, duration_s: 0.0004, replies: vec![r(&bad, 0.0)], snr_db: Some(30.0), if_bw_hz: Some(5e6), seed: 1 });
    let (_, d) = run(&iq, 8e6, Params::new(), 1 << 20);
    assert_eq!((counter(&d, "df17"), counter(&d, "corrected")), (0, 0));
    // The right and the wrong one over each other, in phase: bit 61 has a
    // pulse in both halves - the weakest bit of the message. Mended.
    let iq = scene_iq(&Scene { rate: 8e6, duration_s: 0.0004, replies: vec![r(&bad, 0.0), r(&good, -4.0)], snr_db: Some(30.0), if_bw_hz: Some(5e6), seed: 1 });
    let (_, d) = run(&iq, 8e6, Params::new(), 1 << 20);
    assert_eq!((counter(&d, "df17"), counter(&d, "corrected")), (1, 1));
    // Off, never. (Another slicing phase may still read the weak bit the
    // right way: that is a clean message, not a mended one.)
    let (_, d) = run(&iq, 8e6, Params::new().with("correct", Value::Bool(false)), 1 << 20);
    assert_eq!(counter(&d, "corrected"), 0);
    // Nor at a weak SNR: the threshold is the preamble's.
    let iq = scene_iq(&Scene { rate: 8e6, duration_s: 0.0004, replies: vec![r(&bad, 0.0), r(&good, -4.0)], snr_db: Some(8.0), if_bw_hz: Some(5e6), seed: 1 });
    let (_, d) = run(&iq, 8e6, Params::new(), 1 << 20);
    assert_eq!(counter(&d, "corrected"), 0);
}

// ------------------------------------------------------------------ what must show nothing

#[test]
fn noise_fruit_and_random_mode_s_show_no_aircraft() {
    // A minute of a busy sky with nobody's ADS-B in it, at 2.4 MS/s: noise,
    // 3000 Mode A/C replies a second (a receiver under several radars), and
    // 300 random Mode S replies a second in every format we read, levels
    // from the noise up to 30 dB over it. Generated and fed ten seconds at
    // a time to keep the memory small.
    let rate = 2.4e6;
    let mut d = Adsb::new();
    d.configure(&station()).unwrap();
    let mut sink = Sink::new();
    let mut rng = Rng::new(99);
    let mut index = 0u64;
    for chunk in 0..6 {
        let mut replies = fruit(&mut rng, 10.0, 3000.0, -15.0, 15.0);
        replies.extend(garbage(&mut rng, 10.0, 300.0, -15.0, 15.0));
        let iq = scene_iq(&Scene { rate, duration_s: 10.0, replies, snr_db: Some(15.0), if_bw_hz: Some(2.4e6), seed: 100 + chunk });
        for c in iq.chunks(240_000) {
            d.feed(Input::Iq { samples: c, rate, t0: index as f64 / rate, index }, &mut sink).unwrap();
            index += c.len() as u64;
        }
    }
    let shown: Vec<_> = sink.take().into_iter().filter(|(_, o)| matches!(o, Output::Frame { .. })).collect();
    let c = d.status().counters;
    if show() {
        println!("{c:?}");
    }
    assert!(shown.is_empty(), "{shown:?}");
    // Every random DF4/5/20/21 fails: no address is confirmed.
    assert_eq!(counter(&d, "surveillance"), 0, "{c:?}");
    // 18,000 random DF17/18-ish replies and many more false preambles: at
    // most a chance few pass the parity, and none could confirm an address.
    assert!(counter(&d, "df17") <= 2, "{c:?}");
    assert!(counter(&d, "preambles") > 10_000, "the test must try the parity often: {c:?}");
}

#[test]
fn an_empty_band_at_8_ms_prints_nothing() {
    let rate = 8e6;
    let iq = scene_iq(&Scene { rate, duration_s: 5.0, replies: vec![], snr_db: Some(0.0), if_bw_hz: None, seed: 5 });
    let (out, d) = run(&iq, rate, station(), 800_000);
    assert!(out.is_empty());
    assert_eq!(counter(&d, "messages"), 0, "{:?}", d.status().counters);
}

#[test]
fn a_real_aircraft_among_the_fruit_is_still_shown() {
    let rate = 2.4e6;
    let mut rng = Rng::new(3);
    let mut replies = squitters(&flights()[0], 8.0, &mut rng);
    replies.extend(fruit(&mut rng, 8.0, 3000.0, -10.0, 10.0));
    replies.extend(garbage(&mut rng, 8.0, 300.0, -10.0, 10.0));
    let iq = scene_iq(&Scene { rate, duration_s: 8.0, replies, snr_db: Some(20.0), if_bw_hz: Some(2.4e6), seed: 4 });
    let (out, _) = run(&iq, rate, station(), 240_000);
    assert_eq!(last_states(&out).keys().collect::<Vec<_>>(), vec!["400A01"]);
}

#[test]
fn the_cpu_it_costs() {
    // Seconds of one core per second of input (this thread's CPU time, so
    // a busy host does not inflate it), release build: an empty band, and
    // three aircraft in the Mode A/C fruit of a receiver under two radars.
    let mut worst: f64 = 0.0;
    for (rate, bw) in [(2.4e6, 2.4e6), (6.0e6, 5.0e6), (8.0e6, 5.0e6)] {
        for busy in [false, true] {
            let mut rng = Rng::new(1);
            let mut replies: Vec<Reply> = Vec::new();
            if busy {
                replies = flights().iter().flat_map(|f| squitters(f, 3.0, &mut rng)).collect();
                replies.extend(fruit(&mut rng, 3.0, 1000.0, -10.0, 10.0));
            }
            let iq = scene_iq(&Scene { rate, duration_s: 3.0, replies, snr_db: Some(15.0), if_bw_hz: Some(bw), seed: 2 });
            let start = shack_sdr::cpu::thread_time();
            let (out, _) = run(&iq, rate, station(), (rate / 10.0) as usize);
            let cpu = (shack_sdr::cpu::thread_time() - start).as_secs_f64() / 3.0;
            println!("ADS-B at {:.1} MS/s, {}: {:5.1} ms of CPU a second, {} aircraft", rate / 1e6, if busy { "busy" } else { "empty" }, cpu * 1e3, last_states(&out).len());
            worst = worst.max(cpu);
        }
    }
    if cfg!(not(debug_assertions)) {
        // About 30 ms at 2.4 MS/s and 90 at 8 on shack; generous for a
        // loaded CI host.
        assert!(worst < 0.25, "{worst}");
    }
}

#[test]
fn the_decoder_takes_iq_at_2_ms_or_more_only() {
    let mut d = Adsb::new();
    let x = vec![Complex32::new(0.0, 0.0); 100];
    let mut s = Sink::new();
    assert!(d.feed(Input::Iq { samples: &x, rate: 1.2e6, t0: 0.0, index: 0 }, &mut s).is_err());
    assert!(d.feed(Input::Audio { samples: &[0.0; 10], rate: 48e3, t0: 0.0, index: 0 }, &mut s).is_err());
    assert!(d.configure(&Params::new().with("refLat", Value::Num(95.0))).is_err());
    assert!(d.configure(&Params::new().with("nonsense", Value::Num(1.0))).is_err());
    let _ = ReplyKind::ModeAc(0);
}
