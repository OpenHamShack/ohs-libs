//! Mode S downlink formats as the decoder reads them (ICAO Annex 10 Vol IV,
//! DO-260B; "The 1090MHz Riddle", Junzi Sun, for the worked examples the
//! tests hold us to), and the same layouts written, for the test
//! transmitter.
//!
//! `parse` reads the fields of any format we use; it does **not** decide
//! whether to believe a message (the parity says that, and for the address
//! formats only together with the aircraft already heard: mod.rs). Read:
//!
//! - DF11 all-call reply: capability and address.
//! - DF4/20 altitude and DF5/21 identity (squawk) replies, and their flight
//!   status (on the ground, alert).
//! - DF17 (and DF18, a non-transponder ADS-B, ADS-R or TIS-B) extended
//!   squitter: identification and category (TC 1-4), surface position
//!   (5-8), airborne position with barometric (9-18) or GNSS (20-22)
//!   altitude, velocity (19: ground speed or air speed, vertical rate, the
//!   GNSS-baro difference), emergency and squawk (28/1), operational status
//!   (31: the ADS-B version). Other type codes are counted, not read.
//!
//! Units: feet, knots, degrees true, feet a minute.

use super::cpr::{self, Cpr};
use super::crc;

/// The 64-character set of an identification (Annex 10 Table 3-7); `#`
/// marks a code that is no character.
const CHARS: &[u8; 64] = b"#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######";

const FT_PER_M: f64 = 3.28084;

/// Bits `from..from+n` of a message, MSB first (bit 0 is the first sent).
pub fn bits(m: &[u8], from: usize, n: usize) -> u32 {
    let mut v = 0u32;
    for i in from..from + n {
        v = (v << 1) | ((m[i / 8] >> (7 - i % 8)) & 1) as u32;
    }
    v
}

fn put(m: &mut [u8], from: usize, n: usize, v: u32) {
    for k in 0..n {
        let i = from + k;
        let bit = ((v >> (n - 1 - k)) & 1) as u8;
        m[i / 8] = (m[i / 8] & !(0x80 >> (i % 8))) | (bit << (7 - i % 8));
    }
}

/// How long a format is, in bits; None for one we never take (the rest of
/// the 32 are military, Comm-D, or undefined, and are noise to us).
pub fn length(df: u32) -> Option<usize> {
    match df {
        0 | 4 | 5 | 11 => Some(56),
        16 | 17 | 18 | 20 | 21 => Some(112),
        _ => None,
    }
}

// ---------------------------------------------------------------- altitude

/// The 13-bit identity layout (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4) as
/// four octal digits in hex nibbles: A B C D, so 7700 is 0x7700.
fn id13_to_digits(id: u32) -> u32 {
    let mut h = 0;
    let map = [
        (0x1000, 0x0010), // C1
        (0x0800, 0x1000), // A1
        (0x0400, 0x0020), // C2
        (0x0200, 0x2000), // A2
        (0x0100, 0x0040), // C4
        (0x0080, 0x4000), // A4
        (0x0020, 0x0100), // B1
        (0x0010, 0x0001), // D1
        (0x0008, 0x0200), // B2
        (0x0004, 0x0002), // D2
        (0x0002, 0x0400), // B4
        (0x0001, 0x0004), // D4
    ];
    for (from, to) in map {
        if id & from != 0 {
            h |= to;
        }
    }
    h
}

fn digits_to_id13(h: u32) -> u32 {
    let map = [
        (0x1000, 0x0010),
        (0x0800, 0x1000),
        (0x0400, 0x0020),
        (0x0200, 0x2000),
        (0x0100, 0x0040),
        (0x0080, 0x4000),
        (0x0020, 0x0100),
        (0x0010, 0x0001),
        (0x0008, 0x0200),
        (0x0004, 0x0002),
        (0x0002, 0x0400),
        (0x0001, 0x0004),
    ];
    map.iter().filter(|(_, to)| h & to != 0).fold(0, |a, (from, _)| a | from)
}

/// A Gillham (Mode C) code, as digits, to hundreds of feet; None if it is
/// not one (C must not be zero, D1 never set, only 1-5 in the hundreds).
fn gillham_hundreds(a: u32) -> Option<i32> {
    if a & 0xFFFF_8889 != 0 || a & 0xF0 == 0 {
        return None;
    }
    let mut hundreds = 0u32;
    if a & 0x0010 != 0 {
        hundreds ^= 7;
    }
    if a & 0x0020 != 0 {
        hundreds ^= 3;
    }
    if a & 0x0040 != 0 {
        hundreds ^= 1;
    }
    if hundreds & 5 == 5 {
        hundreds ^= 2;
    }
    if hundreds > 5 {
        return None;
    }
    let mut fives = 0u32;
    for (bit, x) in [(0x0002, 0xFF), (0x0004, 0x7F), (0x1000, 0x3F), (0x2000, 0x1F), (0x4000, 0x0F), (0x0100, 7), (0x0200, 3), (0x0400, 1)] {
        if a & bit != 0 {
            fives ^= x;
        }
    }
    if fives & 1 != 0 {
        hundreds = 6 - hundreds;
    }
    Some((fives * 5 + hundreds) as i32 - 13)
}

/// The 13-bit altitude code of DF0/4/16/20 (M at bit 6, Q at bit 4 from the
/// right): feet, or None (0: not known; metres: not used by civil aircraft
/// here and not believed).
pub fn ac13_feet(ac: u32) -> Option<i32> {
    if ac == 0 || ac & 0x40 != 0 {
        return None;
    }
    if ac & 0x10 != 0 {
        let n = ((ac & 0x1F80) >> 2) | ((ac & 0x20) >> 1) | (ac & 0x0F);
        return Some(n as i32 * 25 - 1000);
    }
    gillham_hundreds(id13_to_digits(ac)).filter(|h| *h >= -12).map(|h| h * 100)
}

/// The 12-bit altitude of an airborne position (no M bit; Q at bit 4).
pub fn ac12_feet(ac: u32) -> Option<i32> {
    if ac == 0 {
        return None;
    }
    if ac & 0x10 != 0 {
        let n = ((ac & 0x0FE0) >> 1) | (ac & 0x0F);
        return Some(n as i32 * 25 - 1000);
    }
    let ac13 = ((ac & 0x0FC0) << 1) | (ac & 0x3F);
    gillham_hundreds(id13_to_digits(ac13)).filter(|h| *h >= -12).map(|h| h * 100)
}

/// Feet as a 12-bit, 25 ft altitude (the transmitter's).
pub fn feet_ac12(ft: i32) -> u32 {
    let n = (((ft + 1000) as f64 / 25.0).round().max(0.0) as u32) & 0x7FF;
    ((n & 0x7F0) << 1) | 0x10 | (n & 0x0F)
}

/// Feet as a 13-bit, 25 ft altitude code.
pub fn feet_ac13(ft: i32) -> u32 {
    let n = (((ft + 1000) as f64 / 25.0).round().max(0.0) as u32) & 0x7FF;
    ((n & 0x7E0) << 2) | ((n & 0x10) << 1) | 0x10 | (n & 0x0F)
}

/// A squawk ("7700") from a 13-bit identity field.
pub fn squawk(id: u32) -> String {
    format!("{:04x}", id13_to_digits(id))
}

/// The 13-bit identity field of a squawk; None unless four octal digits.
pub fn squawk_id13(s: &str) -> Option<u32> {
    if s.len() != 4 || !s.bytes().all(|c| (b'0'..=b'7').contains(&c)) {
        return None;
    }
    Some(digits_to_id13(u32::from_str_radix(s, 16).ok()?))
}

/// Surface movement (7 bits) in knots; None for 0 (not known) and the
/// reserved codes. 124 is "175 kn or more".
pub fn movement_kn(m: u32) -> Option<f64> {
    let m = m as f64;
    Some(match m as u32 {
        1 => 0.0,
        2..=8 => 0.125 * (m - 1.0),
        9..=12 => 1.0 + 0.25 * (m - 9.0),
        13..=38 => 2.0 + 0.5 * (m - 13.0),
        39..=93 => 15.0 + (m - 39.0),
        94..=108 => 70.0 + 2.0 * (m - 94.0),
        109..=123 => 100.0 + 5.0 * (m - 109.0),
        124 => 175.0,
        _ => return None,
    })
}

/// Knots as the movement code nearest below them.
pub fn kn_movement(kn: f64) -> u32 {
    (1..=124).rev().find(|&m| movement_kn(m).is_some_and(|v| v <= kn + 1e-9)).unwrap_or(1)
}

// ---------------------------------------------------------------- reading

/// What an extended squitter carries.
#[derive(Clone, Debug, PartialEq)]
pub enum Es {
    Ident { category: Option<String>, callsign: String },
    /// Airborne position (`geo`: the altitude is GNSS height, TC 20-22).
    Airborne { alt_ft: Option<i32>, geo: bool, cpr: Cpr, nic_tc: u32 },
    Surface { speed_kn: Option<f64>, track: Option<f64>, cpr: Cpr },
    Velocity(Velocity),
    /// TC 28/1: the emergency state (0 none; Table 2-89) and the squawk.
    Status { emergency: u32, squawk: String },
    /// TC 31: the ADS-B version (0, 1, 2) and whether on the surface.
    OpStatus { version: u32, surface: bool },
    Other,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Velocity {
    pub subtype: u32,
    pub gs_kn: Option<f64>,
    pub track: Option<f64>,
    /// Air speed (subtypes 3/4) and whether it is true (else indicated).
    pub airspeed_kn: Option<f64>,
    pub tas: bool,
    pub heading: Option<f64>,
    pub vrate_fpm: Option<i32>,
    /// The vertical rate is barometric (else GNSS).
    pub vrate_baro: bool,
    /// GNSS height minus barometric altitude.
    pub geo_minus_baro_ft: Option<i32>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Body {
    /// DF11: the capability (CA), and the interrogator code its parity
    /// carries (0 for a squitter).
    AllCall { ca: u32, iid: u32 },
    /// DF4/20: altitude; DF5/21: squawk. `fs` the flight status.
    Altitude { fs: u32, alt_ft: Option<i32> },
    Identity { fs: u32, squawk: String },
    /// DF17/18. `ca` the capability (DF17) or control field (DF18).
    Es { ca: u32, tc: u32, es: Es },
}

#[derive(Clone, Debug, PartialEq)]
pub struct Msg {
    pub df: u32,
    /// The address: in clear for DF11/17/18; for the AP formats, the
    /// parity remainder (which is the address only if the message is good).
    pub icao: u32,
    pub body: Body,
}

impl Msg {
    /// On the ground, if the message says either way.
    pub fn on_ground(&self) -> Option<bool> {
        match &self.body {
            Body::AllCall { ca, .. } => match ca {
                4 => Some(true),
                5 => Some(false),
                _ => None,
            },
            Body::Altitude { fs, .. } | Body::Identity { fs, .. } => match fs {
                0 | 2 => Some(false),
                1 | 3 => Some(true),
                _ => None,
            },
            Body::Es { es: Es::Surface { .. }, .. } => Some(true),
            Body::Es { es: Es::Airborne { .. }, .. } => Some(false),
            Body::Es { es: Es::OpStatus { surface, .. }, .. } => Some(*surface),
            _ => None,
        }
    }
}

/// The aircraft category from an identification's type code and CA
/// ("A3"); None when CA is 0 (no information).
pub fn category(tc: u32, ca: u32) -> Option<String> {
    let set = match tc {
        4 => 'A',
        3 => 'B',
        2 => 'C',
        1 => 'D',
        _ => return None,
    };
    (ca != 0).then(|| format!("{set}{ca}"))
}

fn read_es(m: &[u8], tc: u32) -> Es {
    let me = 32;
    match tc {
        1..=4 => {
            let s: String = (0..8).map(|i| CHARS[bits(m, me + 8 + 6 * i, 6) as usize] as char).collect();
            // The category's number is the ME's own 3 bits after the type
            // code, not the message's capability field.
            Es::Ident { category: category(tc, bits(m, me + 5, 3)), callsign: s.trim_end_matches([' ', '#']).to_string() }
        }
        5..=8 => {
            let mov = bits(m, me + 5, 7);
            let trk_ok = bits(m, me + 12, 1) == 1;
            let trk = bits(m, me + 13, 7) as f64 * 360.0 / 128.0;
            Es::Surface {
                speed_kn: movement_kn(mov),
                track: trk_ok.then_some(trk),
                cpr: Cpr { odd: bits(m, me + 21, 1) == 1, lat: bits(m, me + 22, 17), lon: bits(m, me + 39, 17) },
            }
        }
        9..=18 | 20..=22 => Es::Airborne {
            // TC 20-22's altitude is GNSS height as a plain count of metres,
            // not the barometric code ("The 1090MHz Riddle" ch. 5; pyModeS).
            alt_ft: if tc >= 20 {
                Some(bits(m, me + 8, 12)).filter(|v| *v != 0).map(|v| (v as f64 * FT_PER_M).round() as i32)
            } else {
                ac12_feet(bits(m, me + 8, 12))
            },
            geo: tc >= 20,
            cpr: Cpr { odd: bits(m, me + 21, 1) == 1, lat: bits(m, me + 22, 17), lon: bits(m, me + 39, 17) },
            nic_tc: tc,
        },
        19 => Es::Velocity(read_velocity(m)),
        28 if bits(m, me + 5, 3) == 1 => Es::Status { emergency: bits(m, me + 8, 3), squawk: squawk(bits(m, me + 11, 13)) },
        31 => Es::OpStatus { version: bits(m, me + 40, 3), surface: bits(m, me + 5, 3) == 1 },
        _ => Es::Other,
    }
}

fn read_velocity(m: &[u8]) -> Velocity {
    let me = 32;
    let st = bits(m, me + 5, 3);
    let mut v = Velocity { subtype: st, ..Default::default() };
    let vr = bits(m, me + 37, 9);
    if vr != 0 {
        let r = (vr as i32 - 1) * 64;
        v.vrate_fpm = Some(if bits(m, me + 36, 1) == 1 { -r } else { r });
    }
    v.vrate_baro = bits(m, me + 35, 1) == 1;
    let d = bits(m, me + 49, 7);
    if d != 0 {
        let x = (d as i32 - 1) * 25;
        v.geo_minus_baro_ft = Some(if bits(m, me + 48, 1) == 1 { -x } else { x });
    }
    match st {
        1 | 2 => {
            let k = if st == 2 { 4.0 } else { 1.0 };
            let (ew, ns) = (bits(m, me + 14, 10), bits(m, me + 25, 10));
            if ew != 0 && ns != 0 {
                let vx = (ew as f64 - 1.0) * k * if bits(m, me + 13, 1) == 1 { -1.0 } else { 1.0 };
                let vy = (ns as f64 - 1.0) * k * if bits(m, me + 24, 1) == 1 { -1.0 } else { 1.0 };
                v.gs_kn = Some((vx * vx + vy * vy).sqrt());
                let t = vx.atan2(vy).to_degrees();
                v.track = Some(if t < 0.0 { t + 360.0 } else { t });
            }
        }
        3 | 4 => {
            if bits(m, me + 13, 1) == 1 {
                v.heading = Some(bits(m, me + 14, 10) as f64 * 360.0 / 1024.0);
            }
            v.tas = bits(m, me + 24, 1) == 1;
            let a = bits(m, me + 25, 10);
            if a != 0 {
                v.airspeed_kn = Some((a as f64 - 1.0) * if st == 4 { 4.0 } else { 1.0 });
            }
        }
        _ => {}
    }
    v
}

/// The fields of a message of a format we read (7 or 14 bytes). The
/// parity is not checked here.
pub fn parse(m: &[u8]) -> Option<Msg> {
    let df = bits(m, 0, 5);
    if length(df)? != m.len() * 8 {
        return None;
    }
    let body = match df {
        11 => Body::AllCall { ca: bits(m, 5, 3), iid: crc::remainder(m) },
        0 | 4 | 16 | 20 => Body::Altitude { fs: bits(m, 5, 3), alt_ft: ac13_feet(bits(m, 19, 13)) },
        5 | 21 => Body::Identity { fs: bits(m, 5, 3), squawk: squawk(bits(m, 19, 13)) },
        17 | 18 => {
            let (ca, tc) = (bits(m, 5, 3), bits(m, 32, 5));
            Body::Es { ca, tc, es: read_es(m, tc) }
        }
        _ => return None,
    };
    let icao = match df {
        11 | 17 | 18 => bits(m, 8, 24),
        _ => crc::remainder(m),
    };
    Some(Msg { df, icao, body })
}

// ---------------------------------------------------------------- writing

/// A message to write: the transmitter's description of one, in the same
/// units the reader gives.
#[derive(Clone, Debug, PartialEq)]
pub enum Make {
    Ident { icao: u32, tc: u32, ca: u32, callsign: String },
    Airborne { icao: u32, tc: u32, alt_ft: Option<i32>, odd: bool, lat: f64, lon: f64 },
    Surface { icao: u32, tc: u32, speed_kn: Option<f64>, track: Option<f64>, odd: bool, lat: f64, lon: f64 },
    /// Subtype 1/2 from east and north speeds (knots; west and south
    /// negative); vertical rate in ft/min.
    Velocity { icao: u32, subtype: u32, east_kn: f64, north_kn: f64, vrate_fpm: Option<i32>, baro: bool, geo_minus_baro_ft: Option<i32> },
    Airspeed { icao: u32, subtype: u32, heading: Option<f64>, airspeed_kn: Option<f64>, tas: bool, vrate_fpm: Option<i32>, baro: bool },
    Status { icao: u32, emergency: u32, squawk: String },
    OpStatus { icao: u32, version: u32, surface: bool },
    AllCall { icao: u32, ca: u32, iid: u32 },
    /// DF4 or 20 (`df`) with an altitude; DF20's MB is zero.
    Altitude { df: u32, icao: u32, fs: u32, alt_ft: Option<i32> },
    Identity { df: u32, icao: u32, fs: u32, squawk: String },
}

fn es_head(icao: u32, df: u32, ca: u32, tc: u32) -> [u8; 14] {
    let mut m = [0u8; 14];
    put(&mut m, 0, 5, df);
    put(&mut m, 5, 3, ca);
    put(&mut m, 8, 24, icao);
    put(&mut m, 32, 5, tc);
    m
}

fn finish_pi(m: &mut [u8], xor: u32) {
    let n = m.len();
    let p = crc::parity(&m[..n - 3]) ^ xor;
    m[n - 3] = (p >> 16) as u8;
    m[n - 2] = (p >> 8) as u8;
    m[n - 1] = p as u8;
}

fn signed(v: f64, bits: u32, step: f64) -> (u32, u32) {
    let mag = ((v.abs() / step).round() as u32 + 1).min((1 << bits) - 1);
    ((v < 0.0) as u32, mag)
}

/// A message's bytes, parity included. `Err` names what is out of range.
pub fn encode(mk: &Make) -> Result<Vec<u8>, String> {
    let ok24 = |a: u32| if a <= 0xFF_FFFF { Ok(a) } else { Err("icao".to_string()) };
    Ok(match mk {
        Make::Ident { icao, tc, ca, callsign } => {
            if !(1..=4).contains(tc) {
                return Err("tc".into());
            }
            let mut m = es_head(ok24(*icao)?, 17, 5, *tc);
            put(&mut m, 37, 3, *ca);
            let up = callsign.to_ascii_uppercase();
            let mut cs: Vec<u8> = up.bytes().collect();
            if cs.len() > 8 {
                return Err("callsign".into());
            }
            cs.resize(8, b' ');
            for (i, c) in cs.iter().enumerate() {
                let code = CHARS.iter().position(|x| x == c && *c != b'#').ok_or("callsign")?;
                put(&mut m, 40 + 6 * i, 6, code as u32);
            }
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::Airborne { icao, tc, alt_ft, odd, lat, lon } => {
            if !((9..=18).contains(tc) || (20..=22).contains(tc)) {
                return Err("tc".into());
            }
            let mut m = es_head(ok24(*icao)?, 17, 5, *tc);
            let alt = if *tc >= 20 {
                alt_ft.map_or(0, |f| ((f as f64 / FT_PER_M).round().max(0.0) as u32).min(0xFFF))
            } else {
                alt_ft.map_or(0, feet_ac12)
            };
            put(&mut m, 40, 12, alt);
            let c = cpr::encode(*lat, *lon, *odd, false);
            put(&mut m, 53, 1, *odd as u32);
            put(&mut m, 54, 17, c.lat);
            put(&mut m, 71, 17, c.lon);
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::Surface { icao, tc, speed_kn, track, odd, lat, lon } => {
            if !(5..=8).contains(tc) {
                return Err("tc".into());
            }
            let mut m = es_head(ok24(*icao)?, 17, 4, *tc);
            put(&mut m, 37, 7, speed_kn.map_or(0, kn_movement));
            if let Some(t) = track {
                put(&mut m, 44, 1, 1);
                put(&mut m, 45, 7, ((t.rem_euclid(360.0) * 128.0 / 360.0).round() as u32) & 0x7F);
            }
            let c = cpr::encode(*lat, *lon, *odd, true);
            put(&mut m, 53, 1, *odd as u32);
            put(&mut m, 54, 17, c.lat);
            put(&mut m, 71, 17, c.lon);
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::Velocity { icao, subtype, east_kn, north_kn, vrate_fpm, baro, geo_minus_baro_ft } => {
            if !(1..=2).contains(subtype) {
                return Err("subtype".into());
            }
            let mut m = es_head(ok24(*icao)?, 17, 5, 19);
            put(&mut m, 37, 3, *subtype);
            let k = if *subtype == 2 { 4.0 } else { 1.0 };
            let (dw, vw) = signed(*east_kn, 10, k);
            let (ds, vs) = signed(*north_kn, 10, k);
            put(&mut m, 45, 1, dw);
            put(&mut m, 46, 10, vw);
            put(&mut m, 56, 1, ds);
            put(&mut m, 57, 10, vs);
            write_vertical(&mut m, *vrate_fpm, *baro, *geo_minus_baro_ft);
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::Airspeed { icao, subtype, heading, airspeed_kn, tas, vrate_fpm, baro } => {
            if !(3..=4).contains(subtype) {
                return Err("subtype".into());
            }
            let mut m = es_head(ok24(*icao)?, 17, 5, 19);
            put(&mut m, 37, 3, *subtype);
            if let Some(h) = heading {
                put(&mut m, 45, 1, 1);
                put(&mut m, 46, 10, ((h.rem_euclid(360.0) * 1024.0 / 360.0).round() as u32) & 0x3FF);
            }
            put(&mut m, 56, 1, *tas as u32);
            let k = if *subtype == 4 { 4.0 } else { 1.0 };
            put(&mut m, 57, 10, airspeed_kn.map_or(0, |a| signed(a, 10, k).1));
            write_vertical(&mut m, *vrate_fpm, *baro, None);
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::Status { icao, emergency, squawk } => {
            let mut m = es_head(ok24(*icao)?, 17, 5, 28);
            put(&mut m, 37, 3, 1);
            put(&mut m, 40, 3, (*emergency).min(7));
            put(&mut m, 43, 13, squawk_id13(squawk).ok_or("squawk")?);
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::OpStatus { icao, version, surface } => {
            let mut m = es_head(ok24(*icao)?, 17, 5, 31);
            put(&mut m, 37, 3, *surface as u32);
            put(&mut m, 72, 3, (*version).min(7));
            finish_pi(&mut m, 0);
            m.to_vec()
        }
        Make::AllCall { icao, ca, iid } => {
            let mut m = vec![0u8; 7];
            put(&mut m, 0, 5, 11);
            put(&mut m, 5, 3, *ca);
            put(&mut m, 8, 24, ok24(*icao)?);
            finish_pi(&mut m, *iid & 0x7F);
            m
        }
        Make::Altitude { df, icao, fs, alt_ft } => {
            let n = match df {
                4 => 7,
                20 => 14,
                _ => return Err("df".into()),
            };
            let mut m = vec![0u8; n];
            put(&mut m, 0, 5, *df);
            put(&mut m, 5, 3, *fs);
            put(&mut m, 19, 13, alt_ft.map_or(0, feet_ac13));
            finish_pi(&mut m, ok24(*icao)?);
            m
        }
        Make::Identity { df, icao, fs, squawk } => {
            let n = match df {
                5 => 7,
                21 => 14,
                _ => return Err("df".into()),
            };
            let mut m = vec![0u8; n];
            put(&mut m, 0, 5, *df);
            put(&mut m, 5, 3, *fs);
            put(&mut m, 19, 13, squawk_id13(squawk).ok_or("squawk")?);
            finish_pi(&mut m, ok24(*icao)?);
            m
        }
    })
}

fn write_vertical(m: &mut [u8], vrate: Option<i32>, baro: bool, dalt: Option<i32>) {
    put(m, 67, 1, baro as u32);
    if let Some(v) = vrate {
        let (s, x) = signed(v as f64, 9, 64.0);
        put(m, 68, 1, s);
        put(m, 69, 9, x);
    }
    if let Some(d) = dalt {
        let (s, x) = signed(d as f64, 7, 25.0);
        put(m, 80, 1, s);
        put(m, 81, 7, x);
    }
}

/// Hex of a message, upper case, as the literature writes them.
pub fn hex(m: &[u8]) -> String {
    m.iter().map(|b| format!("{b:02X}")).collect()
}

/// A message from hex; None for odd lengths or other characters.
pub fn from_hex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok()).collect()
}
