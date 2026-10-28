//! Aircraft as the decoder believes in them: every good message merged by
//! address, and nothing shown until it has earned it.
//!
//! The AIS decoder's lesson (openhamshack GUIDE §5): a CRC alone let
//! fifteen ships that never existed onto the map from real air, because
//! enough noise goes through a decoder that a check passing once in
//! millions still passes. ADS-B is worse placed - a busy sky gives a
//! receiver thousands of candidate frames a second, and the address
//! formats have no check of their own at all - so an aircraft is shown
//! only when all of these hold:
//!
//! - **Its address is confirmed:** at least two DF17/18 with a parity
//!   remainder of exactly zero, uncorrected (`CONFIRM_DF17`). A corrected
//!   message updates an aircraft already confirmed; it never confirms one.
//! - **Its position comes from a global CPR fix,** an even and an odd
//!   message no more than `PAIR_S` (10 s) apart, or `SURFACE_PAIR_S` on the
//!   ground, and is **seen twice:** the next position, decoded locally
//!   against the first, agrees with it at a speed the aircraft could fly.
//!   A first fix the next one contradicts is thrown away and the aircraft
//!   starts again from a fresh pair.
//! - **It is where an aircraft heard here can be:** within `maxRangeKm`
//!   (500 km) of the station, when the station is known.
//! - **Nothing it says is impossible:** a position that would need more
//!   than `MAX_KN` (1000 kn) from the last is dropped (three in a row and
//!   the position starts again from a pair), as is a speed over 1000 kn
//!   or an altitude outside `ALT_FT`.
//!
//! The address formats (DF4/5/20/21) reach an aircraft only through
//! `confirmed`: their parity *is* the address, so any frame at all names
//! some aircraft, and only one already confirmed on DF17 is believed.

use super::cpr::{self, Cpr};
use super::msg::{Body, Es, Msg};
use crate::decoder::{Fields, Value};
use std::collections::HashMap;

/// Clean DF17/18s before an address is believed.
pub const CONFIRM_DF17: u32 = 2;
/// An even and odd airborne message further apart than this make no fix.
pub const PAIR_S: f64 = 10.0;
/// On the ground aircraft move slowly, and squitter positions less often.
pub const SURFACE_PAIR_S: f64 = 25.0;
pub const MAX_KN: f64 = 1000.0;
/// What CPR's own resolution and a late-timed position may add to a jump.
const JUMP_SLACK_KM: f64 = 0.5;
/// A position older than this is no reference for a local decode.
const LOCAL_REF_S: f64 = 60.0;
/// An address unheard on DF17 for this long recovers nothing.
const CONFIRM_KEEP_S: f64 = 120.0;
/// An aircraft unheard this long is forgotten here (the app keeps its own
/// table for as long as its setting says).
pub const FORGET_S: f64 = 300.0;
pub const ALT_FT: (i32, i32) = (-2000, 65000);
const KM_PER_KN_S: f64 = 1.852 / 3600.0;

/// Non-ICAO addresses (DF18 with an anonymous or TIS-B address) are kept
/// apart from ICAO ones by this bit.
pub const NON_ICAO: u32 = 1 << 24;

#[derive(Clone, Debug)]
pub struct Config {
    pub station: Option<(f64, f64)>,
    pub max_range_km: f64,
    /// At most one update per aircraft this often (sample time).
    pub every_s: f64,
}

impl Default for Config {
    fn default() -> Self {
        Config { station: None, max_range_km: 500.0, every_s: 1.0 }
    }
}

#[derive(Clone, Debug, Default)]
struct Aircraft {
    addr: u32,
    first: f64,
    last: f64,
    messages: u64,
    clean_df17: u32,
    last_df17: f64,
    /// Mean power of recent messages (linear, dB full scale when shown).
    power: f64,
    callsign: Option<String>,
    category: Option<String>,
    squawk: Option<String>,
    emergency: Option<u32>,
    alt_baro: Option<i32>,
    alt_geo: Option<i32>,
    geo_minus_baro: Option<i32>,
    gs: Option<f64>,
    track: Option<f64>,
    heading: Option<f64>,
    airspeed: Option<f64>,
    tas: bool,
    vrate: Option<i32>,
    vrate_baro: bool,
    on_ground: Option<bool>,
    version: Option<u32>,
    source: &'static str,
    even: Option<(Cpr, f64, bool)>,
    odd: Option<(Cpr, f64, bool)>,
    pos: Option<(f64, f64, f64)>,
    /// Positions that agree, from the global fix on: 2 is "seen twice".
    fixes: u32,
    bad: u32,
    shown: bool,
    dirty: bool,
    emitted: f64,
}

/// Why something was not believed, counted in the decoder's status.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Rejected {
    pub range: u64,
    pub jump: u64,
    pub speed: u64,
    pub altitude: u64,
    pub first_fix: u64,
}

#[derive(Default)]
pub struct Tracker {
    pub cfg: Config,
    ac: HashMap<u32, Aircraft>,
    pub rejected: Rejected,
}

pub fn addr_text(a: u32) -> String {
    if a & NON_ICAO != 0 {
        format!("~{:06X}", a & 0xFF_FFFF)
    } else {
        format!("{a:06X}")
    }
}

fn emergency_code(e: u32) -> &'static str {
    match e {
        0 => "none",
        1 => "general",
        2 => "lifeguard",
        3 => "minfuel",
        4 => "nordo",
        5 => "unlawful",
        6 => "downed",
        _ => "reserved",
    }
}

impl Tracker {
    pub fn new(cfg: Config) -> Self {
        Tracker { cfg, ac: HashMap::new(), rejected: Rejected::default() }
    }

    pub fn clear(&mut self) {
        self.ac.clear();
    }

    /// The aircraft shown now.
    pub fn shown(&self) -> usize {
        self.ac.values().filter(|a| a.shown).count()
    }

    /// May an address-parity message naming `addr` be believed at `t`?
    pub fn confirmed(&self, addr: u32, t: f64) -> bool {
        self.ac.get(&addr).is_some_and(|a| a.clean_df17 >= CONFIRM_DF17 && t - a.last_df17 <= CONFIRM_KEEP_S)
    }

    /// A message the decoder believes, heard at `t` with `power` (linear).
    /// `clean`: a DF17/18 with a zero remainder, not corrected. The
    /// decoder has already checked the parity, and for the address formats
    /// called `confirmed`.
    pub fn heard(&mut self, m: &Msg, t: f64, power: f64, clean: bool) {
        let addr = match (&m.body, m.df) {
            // DF18's control field: 0 an ICAO address, 6 ADS-R of one; the
            // rest are anonymous or TIS-B addresses, kept apart.
            (Body::Es { ca, .. }, 18) if *ca != 0 && *ca != 6 => m.icao | NON_ICAO,
            _ => m.icao,
        };
        let is_es = matches!(m.body, Body::Es { .. });
        if !is_es && !self.ac.contains_key(&addr) {
            return;
        }
        let a = self.ac.entry(addr).or_insert_with(|| Aircraft { addr, first: t, ..Default::default() });
        a.last = t;
        a.messages += 1;
        a.power = if a.messages == 1 { power } else { a.power * 0.8 + power * 0.2 };
        a.dirty = true;
        if let Some(g) = m.on_ground() {
            a.on_ground = Some(g);
        }
        match &m.body {
            Body::AllCall { .. } => {}
            Body::Altitude { alt_ft, .. } => {
                if let Some(ft) = alt_ft.filter(|f| (ALT_FT.0..=ALT_FT.1).contains(f)) {
                    a.alt_baro = Some(ft);
                }
            }
            Body::Identity { squawk, .. } => a.squawk = Some(squawk.clone()),
            Body::Es { ca, es, .. } => {
                if clean {
                    a.clean_df17 = a.clean_df17.saturating_add(1);
                }
                a.last_df17 = t;
                a.source = match (m.df, ca) {
                    (18, 6) => "adsr",
                    (18, 2) | (18, 3) | (18, 5) => "tisb",
                    _ => "adsb",
                };
                Self::es(&self.cfg, &mut self.rejected, a, es, t);
            }
        }
        let shown = a.clean_df17 >= CONFIRM_DF17 && a.fixes >= 2;
        if shown && !a.shown {
            a.shown = true;
            // The first update goes out at once, whatever the pacing.
            a.emitted = f64::NEG_INFINITY;
        }
    }

    fn es(cfg: &Config, rej: &mut Rejected, a: &mut Aircraft, es: &Es, t: f64) {
        match es {
            Es::Ident { category, callsign } => {
                if !callsign.is_empty() {
                    a.callsign = Some(callsign.clone());
                }
                if category.is_some() {
                    a.category = category.clone();
                }
            }
            Es::Airborne { alt_ft, geo, cpr, .. } => {
                if let Some(ft) = *alt_ft {
                    if (ALT_FT.0..=ALT_FT.1).contains(&ft) {
                        if *geo {
                            a.alt_geo = Some(ft);
                        } else {
                            a.alt_baro = Some(ft);
                            if let Some(d) = a.geo_minus_baro {
                                a.alt_geo = Some(ft + d);
                            }
                        }
                    } else {
                        rej.altitude += 1;
                    }
                }
                Self::position(cfg, rej, a, *cpr, t, false);
            }
            Es::Surface { speed_kn, track, cpr } => {
                if speed_kn.is_some() {
                    a.gs = *speed_kn;
                }
                if track.is_some() {
                    a.track = *track;
                }
                a.vrate = None;
                Self::position(cfg, rej, a, *cpr, t, true);
            }
            Es::Velocity(v) => {
                if v.gs_kn.is_some_and(|s| s > MAX_KN) || v.airspeed_kn.is_some_and(|s| s > MAX_KN) {
                    rej.speed += 1;
                    return;
                }
                if v.gs_kn.is_some() {
                    a.gs = v.gs_kn;
                    a.track = v.track;
                }
                if v.airspeed_kn.is_some() {
                    a.airspeed = v.airspeed_kn;
                    a.tas = v.tas;
                }
                if v.heading.is_some() {
                    a.heading = v.heading;
                }
                a.vrate = v.vrate_fpm;
                a.vrate_baro = v.vrate_baro;
                a.geo_minus_baro = v.geo_minus_baro_ft;
                if let (Some(b), Some(d)) = (a.alt_baro, v.geo_minus_baro_ft) {
                    a.alt_geo = Some(b + d);
                }
            }
            Es::Status { emergency, squawk } => {
                a.emergency = Some(*emergency);
                a.squawk = Some(squawk.clone());
            }
            Es::OpStatus { version, .. } => a.version = Some(*version),
            Es::Other => {}
        }
    }

    fn in_range(cfg: &Config, lat: f64, lon: f64) -> bool {
        match cfg.station {
            Some((sl, so)) => cpr::distance_km(sl, so, lat, lon) <= cfg.max_range_km,
            None => true,
        }
    }

    fn position(cfg: &Config, rej: &mut Rejected, a: &mut Aircraft, c: Cpr, t: f64, surface: bool) {
        // A message of the other kind (airborne after surface) pairs with
        // nothing from before.
        let slot = (c, t, surface);
        if c.odd {
            a.odd = Some(slot);
        } else {
            a.even = Some(slot);
        }
        let fresh_ref = a.pos.filter(|p| t - p.2 <= LOCAL_REF_S);
        let new = if let Some((plat, plon, _)) = fresh_ref {
            Some(cpr::local(c, surface, plat, plon))
        } else {
            let (Some(e), Some(o)) = (a.even, a.odd) else { return };
            let window = if surface { SURFACE_PAIR_S } else { PAIR_S };
            if e.2 != surface || o.2 != surface || (e.1 - o.1).abs() > window {
                return;
            }
            if surface {
                // Four quadrants fit a surface pair: the station picks.
                let Some((sl, so)) = cfg.station else { return };
                cpr::global_surface(e.0, o.0, c.odd, sl, so)
            } else {
                cpr::global_airborne(e.0, o.0, c.odd)
            }
        };
        let Some((lat, lon)) = new else { return };
        if !Self::in_range(cfg, lat, lon) {
            rej.range += 1;
            if fresh_ref.is_none() {
                // A global fix out of range: the pair was bad, not the
                // next message.
                a.even = None;
                a.odd = None;
            }
            return;
        }
        match fresh_ref {
            None => {
                a.pos = Some((lat, lon, t));
                a.fixes = 1;
                a.bad = 0;
            }
            Some((plat, plon, pt)) => {
                let km = cpr::distance_km(plat, plon, lat, lon);
                let limit = MAX_KN * KM_PER_KN_S * (t - pt).max(0.0) + JUMP_SLACK_KM;
                if km > limit {
                    rej.jump += 1;
                    a.bad += 1;
                    if a.fixes < 2 {
                        // The first fix and the next disagree: neither is
                        // believed; start again from a fresh pair.
                        rej.first_fix += 1;
                        a.pos = None;
                        a.fixes = 0;
                        a.even = None;
                        a.odd = None;
                        a.bad = 0;
                    } else if a.bad >= 3 {
                        a.pos = None;
                        a.bad = 0;
                    }
                    return;
                }
                a.pos = Some((lat, lon, t));
                a.fixes = a.fixes.saturating_add(1);
                a.bad = 0;
            }
        }
    }

    /// The updates due by sample time `now`: each shown aircraft that
    /// changed, at most once per `every_s`. Forgets aircraft unheard for
    /// FORGET_S.
    pub fn due(&mut self, now: f64) -> Vec<(f64, Fields)> {
        self.ac.retain(|_, a| now - a.last <= FORGET_S);
        let every = self.cfg.every_s;
        let mut out = Vec::new();
        for a in self.ac.values_mut() {
            if a.shown && a.dirty && now - a.emitted >= every {
                a.dirty = false;
                a.emitted = now;
                out.push((a.last, fields(a)));
            }
        }
        out.sort_by(|x, y| x.0.total_cmp(&y.0));
        out
    }
}

fn num<T: Into<f64>>(v: Option<T>) -> Value {
    v.map_or(Value::Null, |x| Value::Num(x.into()))
}

fn round(v: Option<f64>, k: f64) -> Value {
    v.map_or(Value::Null, |x| Value::Num((x * k).round() / k))
}

fn text(v: &Option<String>) -> Value {
    v.as_ref().map_or(Value::Null, |s| Value::Text(s.clone()))
}

fn fields(a: &Aircraft) -> Fields {
    let (lat, lon, pt) = match a.pos {
        Some((la, lo, t)) => (Some(la), Some(lo), Some(t)),
        None => (None, None, None),
    };
    // A squawk's emergency codes say what TC 28 would, for an aircraft
    // that sends no TC 28.
    let emergency = a.emergency.map(emergency_code).or(match a.squawk.as_deref() {
        Some("7500") => Some("unlawful"),
        Some("7600") => Some("nordo"),
        Some("7700") => Some("general"),
        Some(_) => Some("none"),
        None => None,
    });
    vec![
        ("icao".into(), Value::Text(addr_text(a.addr))),
        ("source".into(), Value::Text(a.source.to_string())),
        ("callsign".into(), text(&a.callsign)),
        ("category".into(), text(&a.category)),
        ("squawk".into(), text(&a.squawk)),
        ("emergency".into(), emergency.map_or(Value::Null, |e| Value::Text(e.into()))),
        ("lat".into(), round(lat, 1e5)),
        ("lon".into(), round(lon, 1e5)),
        ("posT".into(), num(pt)),
        ("altBaro".into(), num(a.alt_baro)),
        ("altGeo".into(), num(a.alt_geo)),
        ("gs".into(), round(a.gs, 10.0)),
        ("track".into(), round(a.track, 10.0)),
        ("heading".into(), round(a.heading, 10.0)),
        ("airspeed".into(), round(a.airspeed, 1.0)),
        ("airspeedType".into(), if a.airspeed.is_some() { Value::Text(if a.tas { "tas" } else { "ias" }.into()) } else { Value::Null }),
        ("vrate".into(), num(a.vrate)),
        ("vrateSource".into(), if a.vrate.is_some() { Value::Text(if a.vrate_baro { "baro" } else { "geo" }.into()) } else { Value::Null }),
        ("onGround".into(), a.on_ground.map_or(Value::Null, Value::Bool)),
        ("adsbVersion".into(), num(a.version)),
        ("messages".into(), Value::Num(a.messages as f64)),
        ("rssiDb".into(), Value::Num((10.0 * a.power.max(1e-20).log10() * 10.0).round() / 10.0)),
        ("firstT".into(), Value::Num(a.first)),
    ]
}
