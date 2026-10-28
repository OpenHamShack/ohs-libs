//! AIS messages (ITU-R M.1371-5 Annex 8): the bit layouts of the types a
//! map needs, read into raw numbers and text, turned into the units a
//! client shows, written back for the test transmitter, and armoured into
//! NMEA 0183 `!AIVDM` sentences (IEC 61162-1) for logging and for other
//! software.
//!
//! Each layout is a table of fields, read and written by one walker, so the
//! reader and the test transmitter can never disagree about a field's
//! place. Field names in `Raw` are the standard's (snake_case, raw units);
//! `fields` is the client's face: camelCase, real units, and `Null` for
//! the standard's "not available" values.
//!
//! Types read in full: 1, 2, 3 (Class A position), 4 and 11 (base station,
//! UTC answer), 5 (static and voyage), 18 and 19 (Class B), 21 (aids to
//! navigation), 24 (Class B static, parts A and B) and 27 (long-range).
//! Every other type is passed on with its header (type, repeat, MMSI) and
//! its sentence, so nothing heard is lost from the log.

use crate::decoder::{Fields, Value};
use std::collections::BTreeMap;

/// A field's kind in a layout.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum K {
    /// Unsigned.
    U,
    /// Two's complement.
    I,
    /// One bit, a flag.
    B,
    /// Six-bit text, `bits / 6` characters.
    T,
    /// Spare or reserved: skipped on reading, zeros on writing.
    X,
}

#[derive(Clone, Copy, Debug)]
struct F {
    name: &'static str,
    bits: usize,
    kind: K,
}

const fn f(name: &'static str, bits: usize, kind: K) -> F {
    F { name, bits, kind }
}

const HEADER: &[F] = &[f("type", 6, K::U), f("repeat", 2, K::U), f("mmsi", 30, K::U)];

const POSITION_A: &[F] = &[
    f("status", 4, K::U),
    f("turn", 8, K::I),
    f("speed", 10, K::U),
    f("accuracy", 1, K::B),
    f("lon", 28, K::I),
    f("lat", 27, K::I),
    f("course", 12, K::U),
    f("heading", 9, K::U),
    f("second", 6, K::U),
    f("maneuver", 2, K::U),
    f("", 3, K::X),
    f("raim", 1, K::B),
    f("radio", 19, K::U),
];

const BASE_STATION: &[F] = &[
    f("year", 14, K::U),
    f("month", 4, K::U),
    f("day", 5, K::U),
    f("hour", 5, K::U),
    f("minute", 6, K::U),
    f("second", 6, K::U),
    f("accuracy", 1, K::B),
    f("lon", 28, K::I),
    f("lat", 27, K::I),
    f("epfd", 4, K::U),
    f("", 10, K::X),
    f("raim", 1, K::B),
    f("radio", 19, K::U),
];

const STATIC_VOYAGE: &[F] = &[
    f("ais_version", 2, K::U),
    f("imo", 30, K::U),
    f("callsign", 42, K::T),
    f("shipname", 120, K::T),
    f("shiptype", 8, K::U),
    f("to_bow", 9, K::U),
    f("to_stern", 9, K::U),
    f("to_port", 6, K::U),
    f("to_starboard", 6, K::U),
    f("epfd", 4, K::U),
    f("month", 4, K::U),
    f("day", 5, K::U),
    f("hour", 5, K::U),
    f("minute", 6, K::U),
    f("draught", 8, K::U),
    f("destination", 120, K::T),
    f("dte", 1, K::B),
    f("", 1, K::X),
];

const POSITION_B: &[F] = &[
    f("", 8, K::X),
    f("speed", 10, K::U),
    f("accuracy", 1, K::B),
    f("lon", 28, K::I),
    f("lat", 27, K::I),
    f("course", 12, K::U),
    f("heading", 9, K::U),
    f("second", 6, K::U),
    f("", 2, K::X),
    f("cs", 1, K::B),
    f("display", 1, K::B),
    f("dsc", 1, K::B),
    f("band", 1, K::B),
    f("msg22", 1, K::B),
    f("assigned", 1, K::B),
    f("raim", 1, K::B),
    f("radio", 20, K::U),
];

const POSITION_B_EXT: &[F] = &[
    f("", 8, K::X),
    f("speed", 10, K::U),
    f("accuracy", 1, K::B),
    f("lon", 28, K::I),
    f("lat", 27, K::I),
    f("course", 12, K::U),
    f("heading", 9, K::U),
    f("second", 6, K::U),
    f("", 4, K::X),
    f("shipname", 120, K::T),
    f("shiptype", 8, K::U),
    f("to_bow", 9, K::U),
    f("to_stern", 9, K::U),
    f("to_port", 6, K::U),
    f("to_starboard", 6, K::U),
    f("epfd", 4, K::U),
    f("raim", 1, K::B),
    f("dte", 1, K::B),
    f("assigned", 1, K::B),
    f("", 4, K::X),
];

/// Type 21 to its fixed end; `name_ext` (up to 14 characters) follows.
const AID: &[F] = &[
    f("aid_type", 5, K::U),
    f("name", 120, K::T),
    f("accuracy", 1, K::B),
    f("lon", 28, K::I),
    f("lat", 27, K::I),
    f("to_bow", 9, K::U),
    f("to_stern", 9, K::U),
    f("to_port", 6, K::U),
    f("to_starboard", 6, K::U),
    f("epfd", 4, K::U),
    f("second", 6, K::U),
    f("off_position", 1, K::B),
    f("", 8, K::X),
    f("raim", 1, K::B),
    f("virtual_aid", 1, K::B),
    f("assigned", 1, K::B),
    f("", 1, K::X),
];

const STATIC_B_PART: &[F] = &[f("partno", 2, K::U)];
const STATIC_B_A: &[F] = &[f("shipname", 120, K::T)];
const STATIC_B_B: &[F] = &[
    f("shiptype", 8, K::U),
    f("vendorid", 18, K::T),
    f("model", 4, K::U),
    f("serial", 20, K::U),
    f("callsign", 42, K::T),
];
const STATIC_B_DIMS: &[F] = &[
    f("to_bow", 9, K::U),
    f("to_stern", 9, K::U),
    f("to_port", 6, K::U),
    f("to_starboard", 6, K::U),
    f("", 6, K::X),
];
const STATIC_B_MOTHER: &[F] = &[f("mothership_mmsi", 30, K::U), f("", 6, K::X)];

const LONG_RANGE: &[F] = &[
    f("accuracy", 1, K::B),
    f("raim", 1, K::B),
    f("status", 4, K::U),
    f("lon", 18, K::I),
    f("lat", 17, K::I),
    f("speed", 6, K::U),
    f("course", 9, K::U),
    f("gnss", 1, K::B),
    f("", 1, K::X),
];

/// A raw value: a number in the standard's own units, or text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RawV {
    Int(i64),
    Text(String),
}

/// A message's fields by the standard's names, in raw units.
pub type Raw = BTreeMap<String, RawV>;

/// A message read from a frame.
#[derive(Clone, Debug, PartialEq)]
pub struct Message {
    pub kind: u8,
    pub mmsi: u32,
    /// The payload's bits, MSB-first as the tables number them, as long as
    /// the frame (whole octets).
    pub bits: Vec<u8>,
    pub raw: Raw,
}

/// The lengths, in bits, a frame of each type may have (whole octets, as
/// HDLC carries them). A type outside 1-27 or a length its definition
/// never has is no message: with the CRC this is what keeps noise from
/// ever printing one (tests/ais.rs listens to an empty channel).
pub fn length_ok(kind: u8, n: usize) -> bool {
    let between = |a: usize, b: usize| n >= a && n <= b;
    match kind {
        1 | 2 | 3 | 4 | 11 | 18 => n == 168,
        5 => n == 424,
        19 => n == 312,
        21 => between(272, 360),
        24 => n == 160 || n == 168,
        27 => n == 96,
        6 => between(88, 1008),
        7 | 13 => between(72, 168),
        8 => between(56, 1008),
        9 | 22 => n == 168,
        10 => n == 72,
        12 => between(72, 1008),
        14 => between(40, 1008),
        15 => between(88, 160),
        16 => between(96, 144),
        17 => between(80, 816),
        20 => between(72, 160),
        23 => n == 160,
        25 => between(40, 168),
        26 => between(60, 1064),
        _ => false,
    }
}

/// The layout after the header for `kind`, given the bits so far where a
/// type chooses its layout by a field (24's part number, 24 B's MMSI).
fn layout(kind: u8, partno: Option<i64>, mmsi: u32) -> Vec<&'static [F]> {
    match kind {
        1..=3 => vec![POSITION_A],
        4 | 11 => vec![BASE_STATION],
        5 => vec![STATIC_VOYAGE],
        18 => vec![POSITION_B],
        19 => vec![POSITION_B_EXT],
        21 => vec![AID],
        24 => match partno {
            Some(0) => vec![STATIC_B_PART, STATIC_B_A],
            Some(1) => {
                // An auxiliary craft (98MIDxxxx) names its mother ship
                // where others give their size (§ Table 76).
                let tail = if mmsi / 10_000_000 == 98 { STATIC_B_MOTHER } else { STATIC_B_DIMS };
                vec![STATIC_B_PART, STATIC_B_B, tail]
            }
            _ => vec![STATIC_B_PART],
        },
        27 => vec![LONG_RANGE],
        _ => vec![],
    }
}

fn get(bits: &[u8], at: usize, n: usize) -> u64 {
    bits[at..at + n].iter().fold(0u64, |v, &b| (v << 1) | b as u64)
}

const SIXBIT: &[u8; 64] = b"@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?";

fn text_of(bits: &[u8], at: usize, n: usize) -> String {
    let s: String = (0..n / 6).map(|i| SIXBIT[get(bits, at + 6 * i, 6) as usize] as char).collect();
    // '@' is the standard's padding; trailing spaces are what most
    // transmitters pad with instead.
    s.trim_end_matches(['@', ' ']).to_string()
}

fn read_fields(bits: &[u8], at: &mut usize, spec: &[F], raw: &mut Raw) -> bool {
    for fl in spec {
        if *at + fl.bits > bits.len() {
            return false;
        }
        match fl.kind {
            K::X => {}
            K::T => {
                raw.insert(fl.name.into(), RawV::Text(text_of(bits, *at, fl.bits)));
            }
            K::I => {
                let v = get(bits, *at, fl.bits) as i64;
                let v = if v >> (fl.bits - 1) != 0 { v - (1i64 << fl.bits) } else { v };
                raw.insert(fl.name.into(), RawV::Int(v));
            }
            K::U | K::B => {
                raw.insert(fl.name.into(), RawV::Int(get(bits, *at, fl.bits) as i64));
            }
        }
        *at += fl.bits;
    }
    true
}

/// Read a frame's payload (MSB-first bits). None when it is too short for
/// its header, its type and length are not ones AIS sends, or a field runs
/// past its end.
pub fn parse(bits: &[u8]) -> Option<Message> {
    if bits.len() < 38 {
        return None;
    }
    let kind = get(bits, 0, 6) as u8;
    if !length_ok(kind, bits.len()) {
        return None;
    }
    let mut raw = Raw::new();
    let mut at = 0;
    read_fields(bits, &mut at, HEADER, &mut raw);
    let mmsi = get(bits, 8, 30) as u32;
    let partno = if kind == 24 { Some(get(bits, 38, 2) as i64) } else { None };
    for spec in layout(kind, partno, mmsi) {
        if !read_fields(bits, &mut at, spec, &mut raw) {
            return None;
        }
    }
    if kind == 21 && bits.len() >= at + 6 {
        let n = ((bits.len() - at) / 6).min(14) * 6;
        raw.insert("name_ext".into(), RawV::Text(text_of(bits, at, n)));
    }
    Some(Message { kind, mmsi, bits: bits.to_vec(), raw })
}

fn put(out: &mut Vec<u8>, v: u64, n: usize) {
    for i in (0..n).rev() {
        out.push(((v >> i) & 1) as u8);
    }
}

fn sixbit_code(c: char) -> Option<u64> {
    let c = c.to_ascii_uppercase();
    SIXBIT.iter().position(|&s| s as char == c).map(|p| p as u64)
}

fn write_fields(raw: &Raw, spec: &[F], out: &mut Vec<u8>) -> Result<(), &'static str> {
    for fl in spec {
        match fl.kind {
            K::X => put(out, 0, fl.bits),
            K::T => {
                let s = match raw.get(fl.name) {
                    Some(RawV::Text(s)) => s.as_str(),
                    None => "",
                    Some(_) => return Err("bad_field"),
                };
                let n = fl.bits / 6;
                if s.chars().count() > n {
                    return Err("text_too_long");
                }
                for c in s.chars().chain(std::iter::repeat('@')).take(n) {
                    put(out, sixbit_code(c).ok_or("bad_text")?, 6);
                }
            }
            K::U | K::B | K::I => {
                let v = match raw.get(fl.name) {
                    Some(RawV::Int(v)) => *v,
                    None => 0,
                    Some(_) => return Err("bad_field"),
                };
                let (lo, hi) = if fl.kind == K::I {
                    (-(1i64 << (fl.bits - 1)), (1i64 << (fl.bits - 1)) - 1)
                } else {
                    (0, (1i64 << fl.bits) - 1)
                };
                if v < lo || v > hi {
                    return Err("out_of_range");
                }
                put(out, (v as u64) & ((1u64 << fl.bits) - 1), fl.bits);
            }
        }
    }
    Ok(())
}

/// Write a message from its raw fields (the test transmitter and the
/// simulators): `type` and `mmsi` at least; a field left out is 0 (text:
/// empty). Returns the payload's bits, MSB-first, padded to whole octets
/// as HDLC carries them.
pub fn encode(raw: &Raw) -> Result<Vec<u8>, &'static str> {
    let kind = match raw.get("type") {
        Some(RawV::Int(k)) if (1..=27).contains(k) => *k as u8,
        _ => return Err("bad_type"),
    };
    let mmsi = match raw.get("mmsi") {
        Some(RawV::Int(m)) => *m as u32,
        _ => 0,
    };
    let partno = match raw.get("partno") {
        Some(RawV::Int(p)) => Some(*p),
        _ => Some(0),
    };
    let mut out = Vec::with_capacity(424);
    write_fields(raw, HEADER, &mut out)?;
    for spec in layout(kind, if kind == 24 { partno } else { None }, mmsi) {
        write_fields(raw, spec, &mut out)?;
    }
    if kind == 21 {
        if let Some(RawV::Text(s)) = raw.get("name_ext") {
            if s.chars().count() > 14 {
                return Err("text_too_long");
            }
            for c in s.chars() {
                put(&mut out, sixbit_code(c).ok_or("bad_text")?, 6);
            }
        }
    }
    while out.len() % 8 != 0 {
        out.push(0);
    }
    Ok(out)
}

/// The payload's bits as NMEA's six-bit armour, and the fill bits added to
/// make the last character whole.
pub fn armour(bits: &[u8]) -> (String, u8) {
    let fill = (6 - bits.len() % 6) % 6;
    let mut s = String::with_capacity(bits.len() / 6 + 1);
    for c in bits.chunks(6) {
        let mut v = c.iter().fold(0u8, |v, &b| (v << 1) | b);
        v <<= 6 - c.len();
        s.push((if v < 40 { v + 48 } else { v + 56 }) as char);
    }
    (s, fill as u8)
}

/// The reverse of `armour`: None for a character outside the armour.
pub fn dearmour(payload: &str, fill: u8) -> Option<Vec<u8>> {
    let mut bits = Vec::with_capacity(payload.len() * 6);
    for ch in payload.bytes() {
        let v = match ch {
            48..=87 => ch - 48,
            96..=119 => ch - 56,
            _ => return None,
        };
        put(&mut bits, v as u64, 6);
    }
    bits.truncate(bits.len().checked_sub(fill as usize)?);
    Some(bits)
}

/// The NMEA checksum: the XOR of every character between `!` and `*`.
pub fn checksum(body: &str) -> u8 {
    body.bytes().fold(0, |c, b| c ^ b)
}

/// The most armoured characters one sentence carries: a sentence is at most
/// 82 characters, and 60 is what receivers in practice put in each.
pub const SENTENCE_CHARS: usize = 60;

/// `!AIVDM` sentences for a payload heard on `channel` ('A' or 'B'). A
/// message longer than one sentence is split, every part carrying `seq`
/// (0-9), as receivers do; a single sentence leaves it empty.
pub fn nmea(bits: &[u8], channel: char, seq: u8) -> Vec<String> {
    let (payload, fill) = armour(bits);
    let parts: Vec<&str> = payload
        .as_bytes()
        .chunks(SENTENCE_CHARS)
        .map(|c| std::str::from_utf8(c).expect("armour is ASCII"))
        .collect();
    let n = parts.len();
    let seq = if n > 1 { (seq % 10).to_string() } else { String::new() };
    parts
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let f = if i + 1 == n { fill } else { 0 };
            let body = format!("AIVDM,{n},{},{seq},{channel},{p},{f}", i + 1);
            format!("!{body}*{:02X}", checksum(&body))
        })
        .collect()
}

fn int(raw: &Raw, k: &str) -> Option<i64> {
    match raw.get(k) {
        Some(RawV::Int(v)) => Some(*v),
        _ => None,
    }
}

fn num(v: f64) -> Value {
    Value::Num(v)
}

/// Rounded to `places` decimals, so 0.1-knot speeds read 12.3, not
/// 12.299999.
fn round(v: f64, places: i32) -> f64 {
    let m = 10f64.powi(places);
    (v * m).round() / m
}

/// A client's view of a message: camelCase keys, real units, `Null` for
/// "not available". Positions are degrees (east and north positive),
/// speeds knots, courses and headings degrees true, rate of turn degrees a
/// minute, dimensions metres, draught metres. `navStatus`, `shipType`,
/// `epfd` and `aidType` stay the standard's numbers: the app words them.
pub fn fields(m: &Message) -> Fields {
    let r = &m.raw;
    let mut out: Fields = vec![
        ("type".into(), num(m.kind as f64)),
        ("repeat".into(), num(int(r, "repeat").unwrap_or(0) as f64)),
        ("mmsi".into(), num(m.mmsi as f64)),
    ];
    let mut push = |k: &str, v: Value| out.push((k.to_string(), v));
    let opt = |v: Option<f64>| v.map(Value::Num).unwrap_or(Value::Null);
    let flag = |k: &str| Value::Bool(int(r, k).unwrap_or(0) != 0);
    let text = |k: &str| match r.get(k) {
        Some(RawV::Text(s)) if !s.is_empty() => Value::Text(s.clone()),
        _ => Value::Null,
    };
    // 1/10000 minute, 181 and 91 degrees meaning "not available".
    let lon = |k: &str, scale: f64| {
        int(r, k).and_then(|v| {
            let d = v as f64 / scale;
            (d.abs() <= 180.0).then(|| round(d, 6))
        })
    };
    let lat = |k: &str, scale: f64| {
        int(r, k).and_then(|v| {
            let d = v as f64 / scale;
            (d.abs() <= 90.0).then(|| round(d, 6))
        })
    };
    let speed = || int(r, "speed").and_then(|v| (v != 1023).then(|| round(v as f64 / 10.0, 1)));
    let course = || int(r, "course").and_then(|v| (v < 3600).then(|| round(v as f64 / 10.0, 1)));
    let heading = || int(r, "heading").and_then(|v| (v < 360).then_some(v as f64));
    let second = || int(r, "second").and_then(|v| (v < 60).then_some(v as f64));
    let dims = |push: &mut dyn FnMut(&str, Value)| {
        for k in [("to_bow", "toBow"), ("to_stern", "toStern"), ("to_port", "toPort"), ("to_starboard", "toStarboard")] {
            push(k.1, int(r, k.0).map(|v| Value::Num(v as f64)).unwrap_or(Value::Null));
        }
    };
    match m.kind {
        1..=3 => {
            push("navStatus", num(int(r, "status").unwrap_or(15) as f64));
            // ROT_AIS = 4.733 sqrt(ROT); -128 is "not available", +-127
            // "turning faster than 5 degrees in 30 s, no indicator": kept
            // in turnRaw, with no rate.
            let t = int(r, "turn").unwrap_or(-128);
            let rate = if t.abs() <= 126 { Some(round((t as f64 / 4.733).powi(2) * t.signum() as f64, 1)) } else { None };
            push("turn", opt(rate));
            push("turnRaw", num(t as f64));
            push("speed", opt(speed()));
            push("accuracy", flag("accuracy"));
            push("lon", opt(lon("lon", 600_000.0)));
            push("lat", opt(lat("lat", 600_000.0)));
            push("course", opt(course()));
            push("heading", opt(heading()));
            push("second", opt(second()));
            push("maneuver", num(int(r, "maneuver").unwrap_or(0) as f64));
            push("raim", flag("raim"));
            push("radio", num(int(r, "radio").unwrap_or(0) as f64));
        }
        4 | 11 => {
            for k in ["year", "month", "day", "hour", "minute", "second"] {
                push(k, num(int(r, k).unwrap_or(0) as f64));
            }
            push("accuracy", flag("accuracy"));
            push("lon", opt(lon("lon", 600_000.0)));
            push("lat", opt(lat("lat", 600_000.0)));
            push("epfd", num(int(r, "epfd").unwrap_or(0) as f64));
            push("raim", flag("raim"));
            push("radio", num(int(r, "radio").unwrap_or(0) as f64));
        }
        5 => {
            push("aisVersion", num(int(r, "ais_version").unwrap_or(0) as f64));
            push("imo", int(r, "imo").filter(|&v| v != 0).map(|v| Value::Num(v as f64)).unwrap_or(Value::Null));
            push("callsign", text("callsign"));
            push("shipName", text("shipname"));
            push("shipType", num(int(r, "shiptype").unwrap_or(0) as f64));
            dims(&mut push);
            push("epfd", num(int(r, "epfd").unwrap_or(0) as f64));
            let eta = |k: &str, none: i64| int(r, k).filter(|&v| v != none).map(|v| Value::Num(v as f64)).unwrap_or(Value::Null);
            push("etaMonth", eta("month", 0));
            push("etaDay", eta("day", 0));
            push("etaHour", eta("hour", 24));
            push("etaMinute", eta("minute", 60));
            push("draught", opt(int(r, "draught").filter(|&v| v != 0).map(|v| round(v as f64 / 10.0, 1))));
            push("destination", text("destination"));
            push("dte", flag("dte"));
        }
        18 | 19 => {
            push("speed", opt(speed()));
            push("accuracy", flag("accuracy"));
            push("lon", opt(lon("lon", 600_000.0)));
            push("lat", opt(lat("lat", 600_000.0)));
            push("course", opt(course()));
            push("heading", opt(heading()));
            push("second", opt(second()));
            if m.kind == 18 {
                for k in ["cs", "display", "dsc", "band", "msg22", "assigned"] {
                    push(k, flag(k));
                }
                push("raim", flag("raim"));
                push("radio", num(int(r, "radio").unwrap_or(0) as f64));
            } else {
                push("shipName", text("shipname"));
                push("shipType", num(int(r, "shiptype").unwrap_or(0) as f64));
                dims(&mut push);
                push("epfd", num(int(r, "epfd").unwrap_or(0) as f64));
                push("raim", flag("raim"));
                push("dte", flag("dte"));
                push("assigned", flag("assigned"));
            }
        }
        21 => {
            push("aidType", num(int(r, "aid_type").unwrap_or(0) as f64));
            let mut name = match r.get("name") {
                Some(RawV::Text(s)) => s.clone(),
                _ => String::new(),
            };
            if let Some(RawV::Text(e)) = r.get("name_ext") {
                name.push_str(e);
            }
            push("name", if name.is_empty() { Value::Null } else { Value::Text(name) });
            push("accuracy", flag("accuracy"));
            push("lon", opt(lon("lon", 600_000.0)));
            push("lat", opt(lat("lat", 600_000.0)));
            dims(&mut push);
            push("epfd", num(int(r, "epfd").unwrap_or(0) as f64));
            push("second", opt(second()));
            push("offPosition", flag("off_position"));
            push("raim", flag("raim"));
            push("virtualAid", flag("virtual_aid"));
            push("assigned", flag("assigned"));
        }
        24 => {
            let part = int(r, "partno").unwrap_or(0);
            push("partNo", num(part as f64));
            if part == 0 {
                push("shipName", text("shipname"));
            } else if part == 1 {
                push("shipType", num(int(r, "shiptype").unwrap_or(0) as f64));
                push("vendorId", text("vendorid"));
                push("model", num(int(r, "model").unwrap_or(0) as f64));
                push("serial", num(int(r, "serial").unwrap_or(0) as f64));
                push("callsign", text("callsign"));
                if let Some(mother) = int(r, "mothership_mmsi") {
                    push("mothershipMmsi", num(mother as f64));
                } else {
                    dims(&mut push);
                }
            }
        }
        27 => {
            push("accuracy", flag("accuracy"));
            push("raim", flag("raim"));
            push("navStatus", num(int(r, "status").unwrap_or(15) as f64));
            // 1/10 minute here.
            push("lon", opt(lon("lon", 600.0)));
            push("lat", opt(lat("lat", 600.0)));
            push("speed", opt(int(r, "speed").and_then(|v| (v != 63).then_some(v as f64))));
            push("course", opt(int(r, "course").and_then(|v| (v != 511).then_some(v as f64))));
            // "Position latency": set when the position is more than 5 s old.
            push("latency", flag("gnss"));
        }
        _ => {}
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn armour_and_dearmour_round_trip_with_fill() {
        let bits: Vec<u8> = (0..424).map(|i| ((i * 13) % 7 < 3) as u8).collect();
        let (s, fill) = armour(&bits);
        assert_eq!(fill, 2);
        assert_eq!(s.len(), 71);
        assert_eq!(dearmour(&s, fill).unwrap(), bits);
    }

    #[test]
    fn a_long_message_is_two_sentences_sharing_a_sequence_id() {
        let bits = vec![0u8; 424];
        let s = nmea(&bits, 'B', 3);
        assert_eq!(s.len(), 2);
        assert!(s[0].starts_with("!AIVDM,2,1,3,B,"), "{}", s[0]);
        assert!(s[1].starts_with("!AIVDM,2,2,3,B,"), "{}", s[1]);
        assert!(s[1].contains(",2*"), "{}", s[1]);
        let one = nmea(&vec![0u8; 168], 'A', 3);
        assert!(one[0].starts_with("!AIVDM,1,1,,A,"), "{}", one[0]);
    }
}
