//! SPI, Service and Programme Information (ETSI TS 102 818, carried in
//! binary as TS 102 371): station logos and the programme guide, from the
//! MOT carousel an ensemble's SPI data service broadcasts. Nothing is
//! fetched: an SPI file's links to the internet are never followed, and a
//! logo is only ever one the carousel itself carries.
//!
//! **Why decode the service information file rather than guess from file
//! names.** In the binary encoding a logo is a `multimedia` element (url,
//! width, height) under the `service` it belongs to, whose `bearer` id
//! carries the SId: about 150 lines of tag-length-value walking here,
//! where a name-based guess would depend on each broadcaster's habits
//! (TS 102 371 §6.4.3 says a ContentName "contains no useful information
//! other than a unique identifier"). So the file decides. Only when a
//! carousel has no readable SI file do we fall back to a picture whose
//! ContentName names a service's SId in hex (as several multiplexers'
//! tools name them), sized from the picture's own header.
//!
//! **Which logo.** SPI offers 32x32, 112x32, 128x128, 320x240 and 600x600.
//! Two are kept per service: `square` (128x128 preferred, else the square
//! nearest it) and `wide` (320x240 preferred, else the nearest landscape
//! one): a list wants the first, a TV's now-playing the second. The
//! choice waits for the carousel's directory, which lists every object:
//! with an SI file in it, until that file is read, and then for the best
//! picture it names; without one, until every picture named for the
//! service has come. So a 32x32 that happens to come first is never
//! handed over only to be replaced.
//!
//! **Programme information** files (one per service and day) give the
//! schedule: each programme's start (UTC), duration, name (long, else
//! medium, else short) and short description; the decoder works out now
//! and next from the ensemble's clock.
//!
//! Only what is above is read; genres, links, group information, the
//! "advanced" profile's compressed files and on-demand entries are not.

use super::mot::{self, MotObject, CT_APPLICATION};
use std::collections::BTreeMap;

/// Element tags (TS 102 371 annex D) we use.
const T_EPG: u8 = 0x02;
const T_SI: u8 = 0x03;
const T_TOKENS: u8 = 0x04;
const T_SHORT_NAME: u8 = 0x10;
const T_MEDIUM_NAME: u8 = 0x11;
const T_LONG_NAME: u8 = 0x12;
const T_MEDIA: u8 = 0x13;
const T_LOCATION: u8 = 0x19;
const T_SHORT_DESC: u8 = 0x1a;
const T_PROGRAMME: u8 = 0x1c;
const T_SCHEDULE: u8 = 0x21;
const T_SCOPE: u8 = 0x24;
const T_SERVICE_SCOPE: u8 = 0x25;
const T_ENSEMBLE: u8 = 0x26;
const T_SERVICE: u8 = 0x28;
const T_BEARER: u8 = 0x29;
const T_MULTIMEDIA: u8 = 0x2b;
const T_TIME: u8 = 0x2c;
const T_LOC_BEARER: u8 = 0x2d;
const CDATA: u8 = 0x01;
/// The MOT parameter giving a programme information file's service.
pub const P_SCOPE_ID: u8 = 0x27;

/// One element or attribute: its tag and its bytes.
#[derive(Clone, Debug, PartialEq)]
pub struct Node<'a> {
    pub tag: u8,
    pub data: &'a [u8],
}

/// The elements, attributes and CDATA in `b`, in order. Stops at the first
/// that runs past the end.
pub fn nodes(b: &[u8]) -> Vec<Node<'_>> {
    let mut out = Vec::new();
    let mut k = 0;
    while k + 2 <= b.len() {
        let tag = b[k];
        let (len, head) = match b[k + 1] {
            0xfe if k + 4 <= b.len() => (u16::from_be_bytes([b[k + 2], b[k + 3]]) as usize, 4),
            0xff if k + 5 <= b.len() => (u32::from_be_bytes([0, b[k + 2], b[k + 3], b[k + 4]]) as usize, 5),
            0xfe | 0xff => break,
            n => (n as usize, 2),
        };
        if k + head + len > b.len() {
            break;
        }
        out.push(Node { tag, data: &b[k + head..k + head + len] });
        k += head + len;
    }
    out
}

/// The string token table (§4.9): tag to string.
fn tokens(b: &[u8]) -> BTreeMap<u8, Vec<u8>> {
    let mut t = BTreeMap::new();
    let mut k = 0;
    while k + 2 <= b.len() {
        let n = b[k + 1] as usize;
        if k + 2 + n > b.len() {
            break;
        }
        t.insert(b[k], b[k + 2..k + 2 + n].to_vec());
        k += 2 + n;
    }
    t
}

/// A string with its tokens expanded (UTF-8).
fn text(b: &[u8], tok: &BTreeMap<u8, Vec<u8>>) -> String {
    let mut out = Vec::with_capacity(b.len());
    for &c in b {
        match tok.get(&c) {
            Some(s) if c < 0x14 => out.extend_from_slice(s),
            _ => out.push(c),
        }
    }
    String::from_utf8_lossy(&out).trim().to_string()
}

/// An element's CDATA child as text.
fn cdata(b: &[u8], tok: &BTreeMap<u8, Vec<u8>>) -> Option<String> {
    nodes(b).into_iter().find(|n| n.tag == CDATA).map(|n| text(n.data, tok)).filter(|s| !s.is_empty())
}

/// The SId of a DAB bearerURI (§4.7.6.1): flags, ECC, EId, then 16 or 32
/// bits of SId. Returns (EId, SId).
pub fn bearer_sid(b: &[u8]) -> Option<(u16, u32)> {
    if b.len() < 6 {
        return None;
    }
    let long = b[0] & 0x10 != 0;
    let eid = u16::from_be_bytes([b[2], b[3]]);
    let sid = if long {
        u32::from_be_bytes([*b.get(4)?, *b.get(5)?, *b.get(6)?, *b.get(7)?])
    } else {
        u16::from_be_bytes([b[4], b[5]]) as u32
    };
    Some((eid, sid))
}

/// A timePoint (§4.7.4) as UTC seconds.
pub fn time_point(b: &[u8]) -> Option<f64> {
    if b.len() < 4 {
        return None;
    }
    let v = u32::from_be_bytes([b[0], b[1], b[2], b[3]]);
    let mjd = (v >> 14) & 0x1ffff;
    let long = v & 0x800 != 0;
    let (h, m) = ((v >> 6) & 0x1f, v & 0x3f);
    let s = if long { (*b.get(4)? >> 2) as u32 } else { 0 };
    Some(super::codes::mjd_utc(mjd, h, m, s))
}

/// A logo the service information names for a service.
#[derive(Clone, Debug, PartialEq)]
pub struct LogoRef {
    pub url: String,
    pub width: u32,
    pub height: u32,
}

/// The logos per SId that a service information file lists.
pub fn parse_si(b: &[u8]) -> Option<BTreeMap<u32, Vec<LogoRef>>> {
    let top = nodes(b);
    let si = top.iter().find(|n| n.tag == T_SI)?;
    let mut tok = BTreeMap::new();
    let mut out: BTreeMap<u32, Vec<LogoRef>> = BTreeMap::new();
    let mut services: Vec<&[u8]> = Vec::new();
    let inner = nodes(si.data);
    for n in &inner {
        match n.tag {
            T_TOKENS => tok = tokens(n.data),
            // DAB nests services in the ensemble element (§4.17); DRM's sit
            // directly under serviceInformation.
            T_ENSEMBLE => services.extend(nodes(n.data).into_iter().filter(|c| c.tag == T_SERVICE).map(|c| c.data)),
            T_SERVICE => services.push(n.data),
            _ => {}
        }
    }
    for s in services {
        let kids = nodes(s);
        let sids: Vec<u32> = kids
            .iter()
            .filter(|c| c.tag == T_BEARER)
            .filter_map(|c| nodes(c.data).into_iter().find(|a| a.tag == 0x80).and_then(|a| bearer_sid(a.data)))
            .map(|x| x.1)
            .collect();
        let mut logos = Vec::new();
        for m in kids.iter().filter(|c| c.tag == T_MEDIA) {
            for mm in nodes(m.data).into_iter().filter(|c| c.tag == T_MULTIMEDIA) {
                let mut l = LogoRef { url: String::new(), width: 0, height: 0 };
                for a in nodes(mm.data) {
                    match a.tag {
                        0x82 => l.url = text(a.data, &tok),
                        0x84 if a.data.len() >= 2 => l.width = u16::from_be_bytes([a.data[0], a.data[1]]) as u32,
                        0x85 if a.data.len() >= 2 => l.height = u16::from_be_bytes([a.data[0], a.data[1]]) as u32,
                        // The two fixed logo types carry no size (annex F).
                        0x83 => match a.data.first() {
                            Some(4) => (l.width, l.height) = (32, 32),
                            Some(6) => (l.width, l.height) = (112, 32),
                            _ => {}
                        },
                        _ => {}
                    }
                }
                if !l.url.is_empty() {
                    logos.push(l);
                }
            }
        }
        for sid in sids {
            let e = out.entry(sid).or_default();
            for l in &logos {
                if e.len() < 16 && !e.contains(l) {
                    e.push(l.clone());
                }
            }
        }
    }
    Some(out)
}

/// One programme of a schedule.
#[derive(Clone, Debug, PartialEq)]
pub struct Programme {
    pub start: f64,
    pub duration_s: u32,
    pub name: String,
    pub description: Option<String>,
}

/// The programmes per SId in a programme information file. `scope` is the
/// service its MOT header's ScopeID names, for programmes that name none.
pub fn parse_pi(b: &[u8], scope: Option<u32>) -> Option<BTreeMap<u32, Vec<Programme>>> {
    let top = nodes(b);
    let epg = top.iter().find(|n| n.tag == T_EPG)?;
    let mut tok = BTreeMap::new();
    let mut out: BTreeMap<u32, Vec<Programme>> = BTreeMap::new();
    for n in nodes(epg.data) {
        match n.tag {
            T_TOKENS => tok = tokens(n.data),
            T_SCHEDULE => {
                let parts = nodes(n.data);
                // The schedule's scope names its service when the MOT
                // parameter did not.
                let scope = scope.or_else(|| {
                    parts.iter().filter(|p| p.tag == T_SCOPE).flat_map(|p| nodes(p.data)).find(|p| p.tag == T_SERVICE_SCOPE)
                        .and_then(|ss| nodes(ss.data).into_iter().find(|a| a.tag == 0x80))
                        .and_then(|a| bearer_sid(a.data)).map(|x| x.1)
                });
                for p in parts.iter().filter(|p| p.tag == T_PROGRAMME) {
                    let kids = nodes(p.data);
                    let name = [T_LONG_NAME, T_MEDIUM_NAME, T_SHORT_NAME]
                        .iter()
                        .find_map(|t| kids.iter().find(|c| c.tag == *t).and_then(|c| cdata(c.data, &tok)));
                    let description = kids
                        .iter()
                        .filter(|c| c.tag == T_MEDIA)
                        .flat_map(|c| nodes(c.data))
                        .find(|c| c.tag == T_SHORT_DESC)
                        .and_then(|c| cdata(c.data, &tok));
                    for loc in kids.iter().filter(|c| c.tag == T_LOCATION) {
                        let lk = nodes(loc.data);
                        let mut sids: Vec<u32> = lk
                            .iter()
                            .filter(|c| c.tag == T_LOC_BEARER)
                            .filter_map(|c| nodes(c.data).into_iter().find(|a| a.tag == 0x80).and_then(|a| bearer_sid(a.data)))
                            .map(|x| x.1)
                            .collect();
                        if sids.is_empty() {
                            sids.extend(scope);
                        }
                        for tm in lk.iter().filter(|c| c.tag == T_TIME) {
                            let attrs = nodes(tm.data);
                            let Some(start) = attrs.iter().find(|a| a.tag == 0x80).and_then(|a| time_point(a.data)) else { continue };
                            let duration_s = attrs
                                .iter()
                                .find(|a| a.tag == 0x81 && a.data.len() >= 2)
                                .map(|a| u16::from_be_bytes([a.data[0], a.data[1]]) as u32)
                                .unwrap_or(0);
                            for &sid in &sids {
                                let list = out.entry(sid).or_default();
                                if list.len() < 400 {
                                    list.push(Programme { start, duration_s, name: name.clone().unwrap_or_default(), description: description.clone() });
                                }
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    Some(out)
}

/// What the SPI decoder found: a logo, or a service's schedule changed.
#[derive(Clone, Debug, PartialEq)]
pub enum SpiEvent {
    Logo { sid: u32, variant: &'static str, mime: &'static str, name: String, width: u32, height: u32, bytes: Vec<u8> },
    Schedule { sid: u32 },
}

/// A picture held until the service information says whose it is.
struct Picture {
    mime: &'static str,
    width: u32,
    height: u32,
    bytes: Vec<u8>,
}

/// Pictures and programme lists held at most.
const MAX_PICTURES: usize = 64;
const MAX_PICTURE_BYTES: usize = 3 << 20;
const MAX_SERVICES: usize = 64;

/// Joins the carousel's objects into logos and schedules.
#[derive(Default)]
pub struct Spi {
    /// Pictures by ContentName (normalised).
    pictures: BTreeMap<String, Picture>,
    si: Option<BTreeMap<u32, Vec<LogoRef>>>,
    /// The logo handed over per (SId, variant): its name, so a repeat is not.
    sent: BTreeMap<(u32, &'static str), String>,
    pub schedules: BTreeMap<u32, Vec<Programme>>,
    /// The directory's objects: (name, a picture, the SI file).
    listed: Option<Vec<(String, bool, bool)>>,
    pub si_files: u64,
    pub pi_files: u64,
}

/// A ContentName or url as compared: no scheme or host, no leading
/// slashes, any case.
fn norm(s: &str) -> String {
    let s = s.split_once("://").map(|x| x.1.split_once('/').map(|y| y.1).unwrap_or("")).unwrap_or(s);
    s.trim_start_matches('/').to_ascii_lowercase()
}

impl Spi {
    pub fn new() -> Self {
        Self::default()
    }

    /// One whole object from the carousel; `services` are the ensemble's
    /// SIds (for the fallback by name), `dir` the carousel's directory.
    pub fn push(&mut self, obj: &MotObject, services: &[u32], dir: Option<&BTreeMap<u16, mot::Header>>) -> Vec<SpiEvent> {
        self.listed = dir.map(|d| {
            d.values()
                .filter_map(|h| {
                    let name = norm(&h.content_name()?);
                    Some((name, mot::image_mime(h).is_some(), h.content_type == CT_APPLICATION && h.subtype == 0))
                })
                .collect()
        });
        let h = &obj.header;
        if let Some(mime) = mot::image_mime(h) {
            let Some((width, height)) = mot::image_size(&obj.body) else { return Vec::new() };
            let Some(name) = h.content_name() else { return Vec::new() };
            let held: usize = self.pictures.values().map(|p| p.bytes.len()).sum();
            if self.pictures.len() >= MAX_PICTURES || held + obj.body.len() > MAX_PICTURE_BYTES {
                return self.choose(services);
            }
            self.pictures.insert(norm(&name), Picture { mime, width, height, bytes: obj.body.clone() });
            return self.choose(services);
        }
        if h.content_type != CT_APPLICATION {
            return Vec::new();
        }
        match h.subtype {
            0 => {
                if let Some(si) = parse_si(&obj.body) {
                    self.si_files += 1;
                    self.si = Some(si);
                }
                self.choose(services)
            }
            1 => {
                let scope = h.param(P_SCOPE_ID).and_then(bearer_sid).map(|x| x.1);
                let Some(pi) = parse_pi(&obj.body, scope) else { return Vec::new() };
                self.pi_files += 1;
                let mut out = Vec::new();
                for (sid, progs) in pi {
                    if !self.schedules.contains_key(&sid) && self.schedules.len() >= MAX_SERVICES {
                        continue;
                    }
                    let list = self.schedules.entry(sid).or_default();
                    let before = list.clone();
                    for p in progs {
                        // A programme already known by its start is replaced.
                        list.retain(|q| q.start != p.start);
                        list.push(p);
                    }
                    list.sort_by(|a, b| a.start.total_cmp(&b.start));
                    let n = list.len();
                    if n > 400 {
                        list.drain(..n - 400);
                    }
                    if *list != before {
                        out.push(SpiEvent::Schedule { sid });
                    }
                }
                out
            }
            _ => Vec::new(),
        }
    }

    /// Is a picture of this name in the carousel (or, with no directory
    /// yet, held)?
    fn carried(&self, name: &str) -> bool {
        match &self.listed {
            Some(l) => l.iter().any(|x| x.1 && x.0 == name),
            None => self.pictures.contains_key(name),
        }
    }

    /// Each service's candidates, (name, width, height), once the choice
    /// can be made (module docs); None for a service still to wait for.
    fn candidates(&self, services: &[u32]) -> BTreeMap<u32, Option<Vec<(String, u32, u32)>>> {
        let mut out = BTreeMap::new();
        let si_listed = self.listed.as_ref().is_some_and(|l| l.iter().any(|x| x.2));
        if let Some(si) = &self.si {
            for (sid, logos) in si {
                let c: Vec<(String, u32, u32)> =
                    logos.iter().map(|l| (norm(&l.url), l.width, l.height)).filter(|c| self.carried(&c.0)).collect();
                out.insert(*sid, Some(c));
            }
            return out;
        }
        if si_listed || self.listed.is_none() {
            return out; // the SI file is coming, or the directory is not here yet
        }
        let names: Vec<&String> = self.listed.iter().flatten().filter(|x| x.1).map(|x| &x.0).collect();
        for &sid in services {
            let hex = format!("{:04x}", sid);
            let mine: Vec<&String> = names
                .iter()
                .copied()
                .filter(|name| {
                    // The SId as a whole token: not inside a longer hex run.
                    name.match_indices(&hex).any(|(at, _)| {
                        let before = name[..at].chars().last();
                        let after = name[at + hex.len()..].chars().next();
                        !before.is_some_and(|c| c.is_ascii_hexdigit()) && !after.is_some_and(|c| c.is_ascii_hexdigit())
                    })
                })
                .collect();
            if mine.is_empty() {
                continue;
            }
            // Every one of them here: choose by their own sizes.
            let here: Option<Vec<(String, u32, u32)>> =
                mine.iter().map(|n| self.pictures.get(*n).map(|p| ((*n).clone(), p.width, p.height))).collect();
            out.insert(sid, here);
        }
        out
    }

    /// Hand over each service's best logo of each variant, when it is new.
    fn choose(&mut self, services: &[u32]) -> Vec<SpiEvent> {
        let mut out = Vec::new();
        for (sid, cands) in self.candidates(services) {
            let Some(cands) = cands else { continue };
            for (variant, want, square) in [("square", (128i64, 128i64), true), ("wide", (320, 240), false)] {
                let best = cands
                    .iter()
                    .filter(|(_, w, h)| if square { w == h } else { w > h })
                    .min_by_key(|(_, w, h)| (*w as i64 - want.0).abs() + (*h as i64 - want.1).abs());
                let Some((name, _, _)) = best else { continue };
                // The best one named, once it is here.
                let Some(p) = self.pictures.get(name) else { continue };
                if self.sent.get(&(sid, variant)) == Some(name) {
                    continue;
                }
                self.sent.insert((sid, variant), name.clone());
                out.push(SpiEvent::Logo {
                    sid,
                    variant,
                    mime: p.mime,
                    name: name.clone(),
                    width: p.width,
                    height: p.height,
                    bytes: p.bytes.clone(),
                });
            }
        }
        out
    }

    /// Now and next for a service at `t` (UTC seconds).
    pub fn now_next(&self, sid: u32, t: f64) -> (Option<&Programme>, Option<&Programme>) {
        let Some(list) = self.schedules.get(&sid) else { return (None, None) };
        let now = list.iter().rev().find(|p| p.start <= t && (p.duration_s == 0 || t < p.start + p.duration_s as f64));
        let next = list.iter().find(|p| p.start > t);
        (now, next)
    }

    /// Forget programmes that ended more than a day before `t`.
    pub fn prune(&mut self, t: f64) {
        for list in self.schedules.values_mut() {
            list.retain(|p| p.start + p.duration_s as f64 > t - 86_400.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dab::mot::{Limits, MotDecoder, CT_IMAGE, IMAGE_PNG};
    use crate::dab::packet::{packetise, PacketReader};
    use crate::dab::tx::{content_name, mot_directory_mode, mot_header, mot_param, spi_pi_file, spi_si_file, spi_time, spi_bearer, test_png};

    const ECC: u8 = 0xe1;
    const EID: u16 = 0xc1ce;

    /// An SPI carousel's objects: (transport id, header, body).
    fn carousel(si: bool) -> Vec<(u16, Vec<u8>, Vec<u8>)> {
        let mut objs = Vec::new();
        let logos = [("c221_32x32.png", 32, 32), ("c221_128x128.png", 128, 128), ("c221_320x240.png", 320, 240), ("c3a7_128x128.png", 128, 128)];
        for (i, (name, w, h)) in logos.iter().enumerate() {
            let body = test_png(*w, *h, 200);
            objs.push((10 + i as u16, mot_header(body.len(), CT_IMAGE, IMAGE_PNG, &[content_name(name)]), body));
        }
        if si {
            let body = spi_si_file(
                ECC,
                EID,
                &[
                    (0xc221, "Shack MP2 Tone", vec![("c221_32x32.png", 32, 32), ("c221_128x128.png", 128, 128), ("c221_320x240.png", 320, 240)]),
                    // C3A7's SI names a picture the carousel does not carry.
                    (0xc3a7, "Shack DAB+", vec![("/logos/other_128.png", 128, 128)]),
                ],
            );
            objs.push((20, mot_header(body.len(), 7, 0, &[content_name("SI.dat"), mot_param(P_SCOPE_ID, &[ECC, 0xc1, 0xce])]), body));
        }
        objs
    }

    /// The carousel through packets on address 1, a packet reader and MOT.
    fn receive(objs: &[(u16, Vec<u8>, Vec<u8>)]) -> Vec<SpiEvent> {
        let groups = mot_directory_mode(1, objs, 200);
        let mut ci = 0;
        let lf: Vec<u8> = groups.iter().flat_map(|g| packetise(g, 1, 96, &mut ci)).flatten().collect();
        let mut reader = PacketReader::new(1);
        let mut mot = MotDecoder::new(Limits::SPI);
        let mut spi = Spi::new();
        let mut out = Vec::new();
        // In logical frames of 96 bytes (32 kbit/s), as a sub-channel brings them.
        for frame in lf.chunks(96) {
            for g in reader.push_frame(frame) {
                for o in mot.push_bytes(&g, 0.0) {
                    out.extend(spi.push(&o, &[0xc221, 0xc3a7], mot.directory()));
                }
            }
        }
        out
    }

    fn logos(ev: &[SpiEvent]) -> Vec<(u32, &'static str, u32, u32)> {
        ev.iter()
            .filter_map(|e| match e {
                SpiEvent::Logo { sid, variant, width, height, bytes, mime, .. } => {
                    assert_eq!(*mime, "image/png");
                    assert_eq!(crate::dab::mot::image_size(bytes), Some((*width, *height)));
                    Some((*sid, *variant, *width, *height))
                }
                _ => None,
            })
            .collect()
    }

    #[test]
    fn a_logo_from_a_packet_mode_mot_directory_is_mapped_to_its_sid() {
        let got = logos(&receive(&carousel(true)));
        // The SI file decides: C221's square is the 128x128 (not the 32x32),
        // its wide one the 320x240; C3A7's file names no picture we have,
        // so the one only its name points to is not taken.
        assert_eq!(got, vec![(0xc221, "square", 128, 128), (0xc221, "wide", 320, 240)]);
    }

    #[test]
    fn without_a_service_information_file_logos_are_taken_by_name() {
        let got = logos(&receive(&carousel(false)));
        assert!(got.contains(&(0xc221, "square", 128, 128)), "{got:?}");
        assert!(got.contains(&(0xc3a7, "square", 128, 128)), "{got:?}");
        assert!(!got.iter().any(|g| g.0 == 0xc221 && g.2 == 32), "the 32x32 is never the choice");
    }

    #[test]
    fn programme_information_gives_now_and_next() {
        // MJD 61336 is 2026-10-23.
        let pi = spi_pi_file(
            ECC,
            EID,
            0xc221,
            &[
                ("Breakfast", spi_time(61_336, 6, 0, 0), 3 * 3600, Some("Wake up with us")),
                ("Mid Morning", spi_time(61_336, 9, 0, 0), 3 * 3600, None),
            ],
        );
        let header = mot_header(pi.len(), 7, 1, &[content_name("C221_20261023_PI.dat"), mot_param(P_SCOPE_ID, &spi_bearer(ECC, EID, 0xc221))]);
        let ev = receive(&[(30, header, pi)]);
        assert!(ev.contains(&SpiEvent::Schedule { sid: 0xc221 }));
        let mut spi = Spi::new();
        let obj = crate::dab::mot::MotObject {
            transport_id: 30,
            header: crate::dab::mot::Header::parse(&mot_header(0, 7, 1, &[])).unwrap().0,
            body: Vec::new(),
        };
        assert!(spi.push(&obj, &[], None).is_empty(), "an empty file is nothing");
        // Through the parser directly, now at 07:30 UTC.
        let progs = parse_pi(&spi_pi_file(ECC, EID, 0xc221, &[
            ("Breakfast", spi_time(61_336, 6, 0, 0), 10_800, Some("Wake up with us")),
            ("Mid Morning", spi_time(61_336, 9, 0, 0), 10_800, None),
        ]), None).unwrap();
        spi.schedules = progs;
        let t = crate::dab::codes::mjd_utc(61_336, 7, 30, 0);
        let (now, next) = spi.now_next(0xc221, t);
        assert_eq!(now.map(|p| (p.name.as_str(), p.description.as_deref())), Some(("Breakfast", Some("Wake up with us"))));
        assert_eq!(next.map(|p| (p.name.as_str(), p.duration_s)), Some(("Mid Morning", 10_800)));
        assert_eq!(crate::dab::codes::iso_utc(next.unwrap().start), "2026-10-23T09:00:00Z");
    }
}
