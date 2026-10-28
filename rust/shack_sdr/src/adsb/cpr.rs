//! Compact Position Reporting (DO-260B §A.1.7, "The 1090MHz Riddle" ch. 5):
//! a position in 17 bits of latitude and 17 of longitude, sent alternately
//! in two formats, even (0) and odd (1), whose zones differ in size.
//!
//! - **Global** decoding needs one of each format, close enough in time
//!   that the aircraft has not left the zone pair (10 s airborne; track.rs
//!   holds the timing). It needs no reference, so it is how an aircraft's
//!   position starts, and it is checked: both latitudes must fall in the
//!   same longitude-zone count (NL), or the pair straddles a boundary and
//!   gives nothing.
//! - **Local** decoding reads one message against a reference within half
//!   a zone (about 180 NM airborne, 45 NM on the surface): the aircraft's
//!   own last position, once it has one.
//! - **Surface** messages use zones a quarter the size (90 degrees in place
//!   of 360), so a pair alone fixes the position only to one of four
//!   quadrants; the one nearest a reference (the station) is taken.

use std::f64::consts::PI;

const NZ: f64 = 15.0;
const SCALE: f64 = 131_072.0; // 2^17

/// The number of longitude zones at a latitude (NL, DO-260B A.1.7.2d).
pub fn nl(lat: f64) -> u32 {
    let a = lat.abs();
    if a < 1e-9 {
        return 59;
    }
    if (a - 87.0).abs() < 1e-9 {
        return 2;
    }
    if a > 87.0 {
        return 1;
    }
    let x = 1.0 - (1.0 - (PI / (2.0 * NZ)).cos()) / (PI / 180.0 * a).cos().powi(2);
    (2.0 * PI / x.acos()).floor() as u32
}

fn modulo(a: f64, b: f64) -> f64 {
    a - b * (a / b).floor()
}

fn wrap_lon(lon: f64) -> f64 {
    if lon >= 180.0 {
        lon - 360.0
    } else if lon < -180.0 {
        lon + 360.0
    } else {
        lon
    }
}

/// The span of a format's latitude zone: 360 (airborne) or 90 (surface)
/// over 60 (even) or 59 (odd).
fn dlat(odd: bool, surface: bool) -> f64 {
    (if surface { 90.0 } else { 360.0 }) / (4.0 * NZ - odd as u8 as f64)
}

fn span(surface: bool) -> f64 {
    if surface {
        90.0
    } else {
        360.0
    }
}

/// One format's raw numbers.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Cpr {
    pub odd: bool,
    pub lat: u32,
    pub lon: u32,
}

/// Global decode of an even and an odd message; `newer_odd` says which was
/// heard last (the position given is that one's). None when the two
/// latitudes are in different NL zones. Airborne only: see `global_surface`.
pub fn global_airborne(even: Cpr, odd: Cpr, newer_odd: bool) -> Option<(f64, f64)> {
    let (le, lo) = (even.lat as f64 / SCALE, odd.lat as f64 / SCALE);
    let (de, dodd) = (dlat(false, false), dlat(true, false));
    let j = (59.0 * le - 60.0 * lo + 0.5).floor();
    let mut lat_e = de * (modulo(j, 60.0) + le);
    let mut lat_o = dodd * (modulo(j, 59.0) + lo);
    if lat_e >= 270.0 {
        lat_e -= 360.0;
    }
    if lat_o >= 270.0 {
        lat_o -= 360.0;
    }
    if !(-90.0..=90.0).contains(&lat_e) || !(-90.0..=90.0).contains(&lat_o) || nl(lat_e) != nl(lat_o) {
        return None;
    }
    let lat = if newer_odd { lat_o } else { lat_e };
    let (xe, xo) = (even.lon as f64 / SCALE, odd.lon as f64 / SCALE);
    let n = nl(lat) as f64;
    let m = (xe * (n - 1.0) - xo * n + 0.5).floor();
    let (ni, x) = if newer_odd { ((n - 1.0).max(1.0), xo) } else { (n.max(1.0), xe) };
    let lon = 360.0 / ni * (modulo(m, ni) + x);
    Some((lat, wrap_lon(lon)))
}

/// Global decode of a surface pair: of the four positions the pair allows,
/// the one nearest (`ref_lat`, `ref_lon`). None as for airborne.
pub fn global_surface(even: Cpr, odd: Cpr, newer_odd: bool, ref_lat: f64, ref_lon: f64) -> Option<(f64, f64)> {
    let (le, lo) = (even.lat as f64 / SCALE, odd.lat as f64 / SCALE);
    let (de, dodd) = (dlat(false, true), dlat(true, true));
    let j = (59.0 * le - 60.0 * lo + 0.5).floor();
    let lat_e = de * (modulo(j, 60.0) + le);
    let lat_o = dodd * (modulo(j, 59.0) + lo);
    // Northern answers in 0-90; the southern one is 90 less.
    let pick = |l: f64| if (l - 90.0 - ref_lat).abs() < (l - ref_lat).abs() { l - 90.0 } else { l };
    let (lat_e, lat_o) = (pick(lat_e), pick(lat_o));
    if nl(lat_e) != nl(lat_o) {
        return None;
    }
    let lat = if newer_odd { lat_o } else { lat_e };
    let (xe, xo) = (even.lon as f64 / SCALE, odd.lon as f64 / SCALE);
    let n = nl(lat) as f64;
    let m = (xe * (n - 1.0) - xo * n + 0.5).floor();
    let (ni, x) = if newer_odd { ((n - 1.0).max(1.0), xo) } else { (n.max(1.0), xe) };
    let lon0 = 90.0 / ni * (modulo(m, ni) + x);
    // One of lon0, +90, +180, +270: the nearest the reference.
    let lon = (0..4)
        .map(|k| wrap_lon(modulo(lon0 + 90.0 * k as f64 + 180.0, 360.0) - 180.0))
        .min_by(|a, b| lon_gap(*a, ref_lon).total_cmp(&lon_gap(*b, ref_lon)))
        .unwrap_or(lon0);
    Some((lat, lon))
}

fn lon_gap(a: f64, b: f64) -> f64 {
    let d = modulo(a - b + 180.0, 360.0) - 180.0;
    d.abs()
}

/// Local decode of one message against a reference within half a zone.
pub fn local(c: Cpr, surface: bool, ref_lat: f64, ref_lon: f64) -> (f64, f64) {
    let d = dlat(c.odd, surface);
    let y = c.lat as f64 / SCALE;
    let j = (ref_lat / d).floor() + (modulo(ref_lat, d) / d - y + 0.5).floor();
    let lat = d * (j + y);
    let n = nl(lat) as f64 - c.odd as u8 as f64;
    let dl = span(surface) / n.max(1.0);
    let x = c.lon as f64 / SCALE;
    let m = (ref_lon / dl).floor() + (modulo(ref_lon, dl) / dl - x + 0.5).floor();
    (lat, wrap_lon(dl * (m + x)))
}

/// A position as a format's 17-bit numbers (the test transmitter's half).
pub fn encode(lat: f64, lon: f64, odd: bool, surface: bool) -> Cpr {
    let d = dlat(odd, surface);
    let yz = (SCALE * modulo(lat, d) / d + 0.5).floor();
    let rlat = d * (yz / SCALE + (lat / d).floor());
    let n = nl(rlat) as f64 - odd as u8 as f64;
    let dl = span(surface) / n.max(1.0);
    let xz = (SCALE * modulo(lon, dl) / dl + 0.5).floor();
    Cpr { odd, lat: (yz as u32) & 0x1_FFFF, lon: (xz as u32) & 0x1_FFFF }
}

/// Great-circle distance in km.
pub fn distance_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let r = PI / 180.0;
    let (p1, p2) = (lat1 * r, lat2 * r);
    let dp = p2 - p1;
    let dl = (lon2 - lon1) * r;
    let a = (dp / 2.0).sin().powi(2) + p1.cos() * p2.cos() * (dl / 2.0).sin().powi(2);
    2.0 * 6371.0 * a.sqrt().min(1.0).asin()
}
