//! The Fast Information Channel (EN 300 401 §5.2, §6, §8.1): each CIF's
//! 2304 soft bits back to three FIBs, and the FIGs in them parsed into
//! what an ensemble is.
//!
//! - **Configuration (MCI):** the ensemble's id (FIG 0/0), its
//!   sub-channels (0/1), its services and their components (0/2), packet
//!   components (0/3) and the global component ids (0/8). Only the
//!   current configuration is kept (C/N = 0); "next" waits until current.
//! - **Labels:** FIG 1/0, 1/1, 1/4 (component), 1/5 (data service), and
//!   FIG 2's extended UTF-8 or UCS-2 labels, which win over FIG 1's when
//!   both are heard (§5.2.2.3.4: a receiver that can show every character
//!   shows FIG 2's, and ours hands on Unicode).
//! - **Service information:** language (0/5), the ensemble's country,
//!   local time offset and table (0/9), date and time (0/10), user
//!   applications (0/13: which component carries SlideShow or SPI),
//!   programme type (0/17), announcement support and switching (0/18,
//!   0/19, listed only: nothing switches), and service following: linkage
//!   sets (0/6), frequency information (0/21) and other ensembles'
//!   services (0/24). Those three use SIV signalling (C/N is a version,
//!   not current/next), and their lists are kept as heard; nothing here
//!   retunes. Their change event indication (an empty list) is not acted
//!   on: block 11B (Ayr) sends one for its linkage sets several times a
//!   second, and clearing on each would make every list flicker; a set
//!   is rebuilt when its database starts again (C/N 0).

use super::coding::{self, Soft, Viterbi};
use super::tables::{self, Protection, FIB_BITS, FIC_INFO_BITS};
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq)]
pub struct Subchannel {
    pub id: u8,
    /// Start address in CUs.
    pub start: u16,
    pub protection: Protection,
}

/// `subch` of a packet-mode component, which FIG 0/2 does not give (FIG
/// 0/3 does, by its SCId).
pub const NO_SUBCH: u8 = 0xff;

#[derive(Clone, Debug, PartialEq)]
pub struct Component {
    /// 0: MSC stream audio, 1: stream data, 3: packet data.
    pub tmid: u8,
    /// Audio service component type (0 is DAB, MP2; 63 DAB+), or for
    /// stream data the DSCTy.
    pub ascty: u8,
    /// The sub-channel of a stream component; NO_SUBCH for packet mode.
    pub subch: u8,
    /// A packet-mode component's SCId (its FIG 0/3 entry says where it is).
    pub scid: Option<u16>,
    pub primary: bool,
}

/// A packet-mode service component (FIG 0/3).
#[derive(Clone, Debug, PartialEq)]
pub struct PacketComponent {
    pub subch: u8,
    pub address: u16,
    pub dscty: u8,
    /// Data groups carry it (DG flag 0, as MOT always does).
    pub data_groups: bool,
}

/// One user application of a component (FIG 0/13).
#[derive(Clone, Debug, PartialEq)]
pub struct UserApp {
    pub ty: u16,
    /// The data field as sent: for an application in an audio component's
    /// X-PAD its first byte holds the X-PAD application type.
    pub data: Vec<u8>,
}

/// A programme type (FIG 0/17).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pty {
    pub code: u8,
    /// S/D: the type follows the items within the programme.
    pub dynamic: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Service {
    pub sid: u32,
    pub label: Option<String>,
    pub short_label: Option<String>,
    /// FIG 2's label, preferred when heard.
    pub ext_label: Option<String>,
    pub ext_short_label: Option<String>,
    pub components: Vec<Component>,
    pub pty: Option<Pty>,
    /// Announcement types that may interrupt it, and its clusters (0/18).
    pub asu: u16,
    pub clusters: Vec<u8>,
    /// User applications by SCIdS (0 is the primary component).
    pub apps: BTreeMap<u8, Vec<UserApp>>,
    /// Component labels by SCIdS: FIG 1/4's and FIG 2/4's (preferred).
    pub component_labels: BTreeMap<u8, String>,
    pub component_ext_labels: BTreeMap<u8, String>,
    /// The ECC when it differs from the ensemble's (0/9's extended field).
    pub ecc: Option<u8>,
}

impl Service {
    /// The primary audio component (the first audio one if none says so).
    pub fn audio(&self) -> Option<&Component> {
        let audio = || self.components.iter().filter(|c| c.tmid == 0);
        audio().find(|c| c.primary).or_else(|| audio().next())
    }

    /// The label to show: FIG 2's when heard, else FIG 1's.
    pub fn best_label(&self) -> Option<&String> {
        self.ext_label.as_ref().or(self.label.as_ref())
    }

    pub fn best_short_label(&self) -> Option<&String> {
        if self.ext_label.is_some() {
            self.ext_short_label.as_ref().or(self.short_label.as_ref())
        } else {
            self.short_label.as_ref()
        }
    }

    /// A component's label (SCIdS 1-15): FIG 2's when heard, else FIG 1's.
    pub fn component_label(&self, scids: u8) -> Option<&String> {
        self.component_ext_labels.get(&scids).or_else(|| self.component_labels.get(&scids))
    }

    /// Does any of its components signal this user application?
    pub fn has_app(&self, ty: u16) -> bool {
        self.apps.values().flatten().any(|a| a.ty == ty)
    }
}

/// A component as FIG 0/8 places it: by sub-channel or by SCId.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ComponentRef {
    Subch(u8),
    Scid(u16),
}

/// An announcement now on (FIG 0/19).
#[derive(Clone, Debug, PartialEq)]
pub struct Switching {
    pub flags: u16,
    pub subch: u8,
    pub new: bool,
}

/// One identifier in a linkage set (FIG 0/6).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LinkId {
    /// IdLQ: 0 a DAB SId, 1 an RDS PI, 3 DRM/AMSS.
    pub kind: u8,
    pub ecc: Option<u8>,
    pub id: u32,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Linkage {
    pub active: bool,
    pub hard: bool,
    pub international: bool,
    /// The key service: a DAB SId in this ensemble.
    pub key: Option<u32>,
    pub ids: Vec<LinkId>,
}

/// Frequency information for one identifier (FIG 0/21): `rm` 0 a DAB
/// ensemble (the id is its EId), 6 DRM, 8 FM with RDS (the id is its PI),
/// 14 AMSS; frequencies in kHz.
#[derive(Clone, Debug, PartialEq)]
pub struct FreqInfo {
    pub id: u16,
    pub rm: u8,
    pub continuity: bool,
    pub other_ensemble: bool,
    pub khz: Vec<u32>,
}

/// The ensemble clock (FIG 0/10).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DabTime {
    pub mjd: u32,
    pub hour: u8,
    pub minute: u8,
    /// None for the short form (legacy: minutes only).
    pub second: Option<u8>,
}

/// A FIG 2 label being put together from its segments.
#[derive(Clone, Debug, Default, PartialEq)]
struct ExtLabel {
    toggle: bool,
    segs: [Option<Vec<u8>>; 8],
}

/// The most of each list kept, so a broken or hostile FIC cannot grow
/// one without bound (the real ones are a few dozen entries at most).
const MAX_ENTRIES: usize = 256;

/// Everything the FIC has told us.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Ensemble {
    pub eid: Option<u16>,
    pub label: Option<String>,
    pub short_label: Option<String>,
    pub ext_label: Option<String>,
    pub ext_short_label: Option<String>,
    pub subchannels: BTreeMap<u8, Subchannel>,
    pub services: BTreeMap<u32, Service>,
    /// The latest CIF count (0-4999) FIG 0/0 gave.
    pub cif_count: Option<u16>,
    /// Packet-mode components by SCId (FIG 0/3).
    pub packet: BTreeMap<u16, PacketComponent>,
    /// (SId, SCIdS) to the component (FIG 0/8).
    pub global: BTreeMap<(u32, u8), ComponentRef>,
    /// Languages (FIG 0/5) by sub-channel and by SCId.
    pub lang_subch: BTreeMap<u8, u8>,
    pub lang_scid: BTreeMap<u16, u8>,
    /// FIG 0/9: the ensemble's ECC, local time offset (half hours) and
    /// international table.
    pub ecc: Option<u8>,
    pub lto: Option<i8>,
    pub inter_table: Option<u8>,
    pub time: Option<DabTime>,
    /// Announcements on now, by cluster (FIG 0/19; all-zero flags end one).
    pub switching: BTreeMap<u8, Switching>,
    /// Linkage sets by (LSN, hard, international, P/D).
    pub links: BTreeMap<(u16, bool, bool, bool), Linkage>,
    /// Frequency information by (id, R&M, OE).
    pub freqs: BTreeMap<(u16, u8, bool), FreqInfo>,
    /// Other ensembles that carry a service (FIG 0/24), by SId.
    pub oe_services: BTreeMap<u32, Vec<u16>>,
    ext_labels: BTreeMap<(u8, u32), ExtLabel>,
}

/// The short label: the characters the flag field marks (§5.2.2.2).
fn short_label(label: &str, flags: u16) -> Option<String> {
    let s: String = label.chars().enumerate().filter(|(i, _)| *i < 16 && flags >> (15 - i) & 1 == 1).map(|(_, c)| c).collect();
    (!s.is_empty()).then(|| s.trim_end().to_string())
}

fn be(b: &[u8]) -> u32 {
    b.iter().fold(0u32, |a, &x| a << 8 | x as u32)
}

impl Ensemble {
    /// Parse one FIB's data field (30 bytes; its CRC already checked).
    pub fn parse_fib(&mut self, fib: &[u8]) {
        let mut i = 0;
        while i < fib.len().min(30) {
            let h = fib[i];
            if h == 0xff {
                break; // end marker
            }
            let (ty, len) = (h >> 5, (h & 0x1f) as usize);
            let end = i + 1 + len;
            if len == 0 || end > 30 {
                break;
            }
            let d = &fib[i + 1..end];
            match ty {
                0 => self.fig0(d),
                1 => self.fig1(d),
                2 => self.fig2(d),
                _ => {} // FIG 6 (conditional access): not ours to read
            }
            i = end;
        }
    }

    fn service(&mut self, sid: u32) -> Option<&mut Service> {
        if !self.services.contains_key(&sid) && self.services.len() >= MAX_ENTRIES {
            return None;
        }
        Some(self.services.entry(sid).or_insert_with(|| Service { sid, ..Default::default() }))
    }

    /// The component (SId, SCIdS) names: SCIdS 0 is the primary one; any
    /// other is placed by FIG 0/8.
    pub fn component(&self, sid: u32, scids: u8) -> Option<&Component> {
        let s = self.services.get(&sid)?;
        if scids == 0 {
            return s.components.iter().find(|c| c.primary).or_else(|| s.components.first());
        }
        match self.global.get(&(sid, scids))? {
            ComponentRef::Subch(sc) => s.components.iter().find(|c| c.subch == *sc && c.scid.is_none()),
            ComponentRef::Scid(id) => s.components.iter().find(|c| c.scid == Some(*id)),
        }
    }

    /// Where an application of a service is carried: in the X-PAD of its
    /// audio (`None`) or in a packet-mode component (its FIG 0/3 entry).
    pub fn app_carrier(&self, sid: u32, ty: u16) -> Option<Option<PacketComponent>> {
        let s = self.services.get(&sid)?;
        for (scids, apps) in &s.apps {
            if !apps.iter().any(|a| a.ty == ty) {
                continue;
            }
            match self.component(sid, *scids) {
                Some(c) if c.tmid == 0 => return Some(None),
                Some(Component { scid: Some(id), .. }) => {
                    if let Some(p) = self.packet.get(id) {
                        return Some(Some(p.clone()));
                    }
                }
                _ => {}
            }
        }
        None
    }

    /// The ensemble's SPI service, when one signals it: (its SId, where).
    pub fn spi(&self) -> Option<(u32, PacketComponent)> {
        self.services.keys().find_map(|&sid| match self.app_carrier(sid, super::codes::UA_SPI)? {
            Some(p) if p.data_groups => Some((sid, p)),
            _ => None,
        })
    }

    /// The language of a component (FIG 0/5), by where it is carried.
    pub fn language_of(&self, c: &Component) -> Option<u8> {
        match c.scid {
            Some(id) => self.lang_scid.get(&id).copied(),
            None => self.lang_subch.get(&c.subch).copied(),
        }
    }

    /// The ensemble clock as UTC seconds (whole minutes for the short form).
    pub fn utc(&self) -> Option<f64> {
        self.time.map(|t| super::codes::mjd_utc(t.mjd, t.hour as u32, t.minute as u32, t.second.unwrap_or(0) as u32))
    }

    fn fig0(&mut self, d: &[u8]) {
        if d.is_empty() {
            return;
        }
        let cn = d[0] & 0x80 != 0;
        let oe = d[0] & 0x40 != 0;
        let pd = d[0] & 0x20 != 0;
        let ext = d[0] & 0x1f;
        let b = &d[1..];
        let sid_len = if pd { 4 } else { 2 };
        match ext {
            0 if b.len() >= 4 => {
                self.eid = Some(u16::from_be_bytes([b[0], b[1]]));
                let hi = (b[2] & 0x1f) as u16;
                self.cif_count = Some(hi * 250 + b[3] as u16);
            }
            1 if !cn => {
                let mut k = 0;
                while k + 3 <= b.len() {
                    let id = b[k] >> 2;
                    let start = ((b[k] as u16 & 3) << 8) | b[k + 1] as u16;
                    let long = b[k + 2] & 0x80 != 0;
                    let protection = if long {
                        if k + 4 > b.len() {
                            break;
                        }
                        let option = (b[k + 2] >> 4) & 7;
                        let level = ((b[k + 2] >> 2) & 3) + 1;
                        let size = ((b[k + 2] as u16 & 3) << 8) | b[k + 3] as u16;
                        k += 4;
                        Protection::Eep { option, level, size }
                    } else {
                        let index = b[k + 2] & 0x3f;
                        let table_switch = b[k + 2] & 0x40 != 0;
                        k += 3;
                        if table_switch {
                            continue; // reserved
                        }
                        Protection::Uep { index }
                    };
                    self.subchannels.insert(id, Subchannel { id, start, protection });
                }
            }
            2 if !cn && !oe => {
                let mut k = 0;
                while k + sid_len < b.len() {
                    let sid = be(&b[k..k + sid_len]);
                    let ncomp = (b[k + sid_len] & 0x0f) as usize;
                    k += sid_len + 1;
                    let mut comps = Vec::new();
                    for _ in 0..ncomp {
                        if k + 2 > b.len() {
                            return;
                        }
                        let (x, y) = (b[k], b[k + 1]);
                        let tmid = x >> 6;
                        let primary = y & 2 != 0;
                        match tmid {
                            0 | 1 => comps.push(Component { tmid, ascty: x & 0x3f, subch: y >> 2, scid: None, primary }),
                            3 => {
                                let scid = (u16::from_be_bytes([x, y]) >> 2) & 0x0fff;
                                comps.push(Component { tmid, ascty: 0, subch: NO_SUBCH, scid: Some(scid), primary });
                            }
                            _ => {}
                        }
                        k += 2;
                    }
                    if let Some(s) = self.service(sid) {
                        s.components = comps;
                    }
                }
            }
            3 if !cn => {
                let mut k = 0;
                while k + 5 <= b.len() {
                    let scid = u16::from_be_bytes([b[k], b[k + 1]]) >> 4;
                    let caorg = b[k + 1] & 1 != 0;
                    let dg = b[k + 2] & 0x80 == 0;
                    let dscty = b[k + 2] & 0x3f;
                    let subch = b[k + 3] >> 2;
                    let address = u16::from_be_bytes([b[k + 3], b[k + 4]]) & 0x3ff;
                    k += 5 + if caorg { 2 } else { 0 };
                    if self.packet.len() < MAX_ENTRIES || self.packet.contains_key(&scid) {
                        self.packet.insert(scid, PacketComponent { subch, address, dscty, data_groups: dg });
                    }
                }
            }
            5 => {
                let mut k = 0;
                while k + 2 <= b.len() {
                    if b[k] & 0x80 == 0 {
                        self.lang_subch.insert(b[k] & 0x3f, b[k + 1]);
                        k += 2;
                    } else {
                        if k + 3 > b.len() {
                            break;
                        }
                        let scid = u16::from_be_bytes([b[k], b[k + 1]]) & 0x0fff;
                        if self.lang_scid.len() < MAX_ENTRIES {
                            self.lang_scid.insert(scid, b[k + 2]);
                        }
                        k += 3;
                    }
                }
            }
            6 => self.fig0_6(cn, pd, b),
            8 if !cn => {
                let mut k = 0;
                while k + sid_len + 2 <= b.len() {
                    let sid = be(&b[k..k + sid_len]);
                    let x = b[k + sid_len];
                    let (ext_flag, scids) = (x & 0x80 != 0, x & 0x0f);
                    k += sid_len + 1;
                    let at = if b[k] & 0x80 == 0 {
                        k += 1;
                        ComponentRef::Subch(b[k - 1] & 0x3f)
                    } else {
                        if k + 2 > b.len() {
                            break;
                        }
                        k += 2;
                        ComponentRef::Scid(u16::from_be_bytes([b[k - 2], b[k - 1]]) & 0x0fff)
                    };
                    if ext_flag {
                        k += 1;
                    }
                    if self.global.len() < MAX_ENTRIES || self.global.contains_key(&(sid, scids)) {
                        self.global.insert((sid, scids), at);
                    }
                }
            }
            9 if b.len() >= 3 => {
                let lto = (b[0] & 0x1f) as i8;
                self.lto = Some(if b[0] & 0x20 != 0 { -lto } else { lto });
                self.ecc = Some(b[1]);
                self.inter_table = Some(b[2]);
                // The extended field: services whose ECC differs.
                if b[0] & 0x80 != 0 {
                    let mut k = 3;
                    while k + 2 <= b.len() {
                        let n = (b[k] >> 6) as usize;
                        let ecc = b[k + 1];
                        k += 2;
                        for _ in 0..n {
                            if k + 2 > b.len() {
                                return;
                            }
                            let sid = u16::from_be_bytes([b[k], b[k + 1]]) as u32;
                            if let Some(s) = self.service(sid) {
                                s.ecc = Some(ecc);
                            }
                            k += 2;
                        }
                    }
                }
            }
            10 if b.len() >= 4 => {
                let v = be(&b[..4]);
                let mjd = (v >> 14) & 0x1ffff;
                let long = v & 0x800 != 0;
                let (hour, minute) = (((v >> 6) & 0x1f) as u8, (v & 0x3f) as u8);
                let second = (long && b.len() >= 6).then(|| b[4] >> 2);
                if hour < 24 && minute < 60 && second.is_none_or(|s| s < 61) {
                    self.time = Some(DabTime { mjd, hour, minute, second });
                }
            }
            13 => {
                let mut k = 0;
                while k + sid_len + 1 <= b.len() {
                    let sid = be(&b[k..k + sid_len]);
                    let scids = b[k + sid_len] >> 4;
                    let n = (b[k + sid_len] & 0x0f) as usize;
                    k += sid_len + 1;
                    let mut apps = Vec::new();
                    for _ in 0..n {
                        if k + 2 > b.len() {
                            return;
                        }
                        let ty = u16::from_be_bytes([b[k], b[k + 1]]) >> 5;
                        let len = (b[k + 1] & 0x1f) as usize;
                        k += 2;
                        if k + len > b.len() {
                            return;
                        }
                        apps.push(UserApp { ty, data: b[k..k + len].to_vec() });
                        k += len;
                    }
                    if let Some(s) = self.service(sid) {
                        if s.apps.len() < 16 || s.apps.contains_key(&scids) {
                            s.apps.insert(scids, apps);
                        }
                    }
                }
            }
            17 => {
                let mut k = 0;
                while k + 4 <= b.len() {
                    let sid = u16::from_be_bytes([b[k], b[k + 1]]) as u32;
                    let flags = b[k + 2];
                    // Before EN 300 401 V2 the two Rfu1 bits were the L and
                    // CC flags: a language byte before the code and a
                    // complementary code after it. Old multiplexers still
                    // send them, so step over both.
                    let lang = flags & 0x20 != 0;
                    let cc = flags & 0x10 != 0;
                    let at = k + 3 + lang as usize;
                    if at >= b.len() {
                        break;
                    }
                    let code = b[at] & 0x1f;
                    k = at + 1 + cc as usize;
                    if let Some(s) = self.service(sid) {
                        s.pty = Some(Pty { code, dynamic: flags & 0x80 != 0 });
                    }
                }
            }
            18 => {
                let mut k = 0;
                while k + 5 <= b.len() {
                    let sid = u16::from_be_bytes([b[k], b[k + 1]]) as u32;
                    let asu = u16::from_be_bytes([b[k + 2], b[k + 3]]);
                    let n = (b[k + 4] & 7) as usize;
                    k += 5;
                    let clusters = b[k..(k + n).min(b.len())].to_vec();
                    k += n;
                    if let Some(s) = self.service(sid) {
                        s.asu = asu;
                        s.clusters = clusters;
                    }
                }
            }
            19 => {
                let mut k = 0;
                while k + 4 <= b.len() {
                    let cluster = b[k];
                    let flags = u16::from_be_bytes([b[k + 1], b[k + 2]]);
                    let (new, region) = (b[k + 3] & 0x80 != 0, b[k + 3] & 0x40 != 0);
                    let subch = b[k + 3] & 0x3f;
                    k += 4 + region as usize;
                    if flags == 0 {
                        self.switching.remove(&cluster);
                    } else if self.switching.len() < MAX_ENTRIES || self.switching.contains_key(&cluster) {
                        self.switching.insert(cluster, Switching { flags, subch, new });
                    }
                }
            }
            21 => self.fig0_21(oe, b),
            24 => {
                let mut k = 0;
                while k + sid_len + 1 <= b.len() {
                    let sid = be(&b[k..k + sid_len]);
                    let n = (b[k + sid_len] & 0x0f) as usize;
                    k += sid_len + 1;
                    let mut eids = Vec::new();
                    for _ in 0..n {
                        if k + 2 > b.len() {
                            return;
                        }
                        eids.push(u16::from_be_bytes([b[k], b[k + 1]]));
                        k += 2;
                    }
                    if eids.is_empty() {
                        // A change event (module docs).
                    } else if self.oe_services.len() < MAX_ENTRIES || self.oe_services.contains_key(&sid) {
                        self.oe_services.insert(sid, eids);
                    }
                }
            }
            _ => {}
        }
    }

    /// FIG 0/6, service linking. The first identifier of a part sent with
    /// C/N = 0 (the database's start) is the key service, a DAB SId here.
    fn fig0_6(&mut self, cn: bool, pd: bool, b: &[u8]) {
        let mut k = 0;
        while k + 2 <= b.len() {
            let w = u16::from_be_bytes([b[k], b[k + 1]]);
            let (has_list, active, hard, ils) = (w & 0x8000 != 0, w & 0x4000 != 0, w & 0x2000 != 0, w & 0x1000 != 0);
            let lsn = w & 0x0fff;
            k += 2;
            let key = (lsn, hard, ils, pd);
            if !has_list {
                continue; // a change event (module docs)
            }
            if k >= b.len() {
                return;
            }
            let idlq = (b[k] >> 5) & 3;
            let n = (b[k] & 0x0f) as usize;
            k += 1;
            let mut ids = Vec::new();
            for _ in 0..n {
                let id = if pd {
                    if k + 4 > b.len() {
                        return;
                    }
                    k += 4;
                    LinkId { kind: idlq, ecc: None, id: be(&b[k - 4..k]) }
                } else if ils {
                    if k + 3 > b.len() {
                        return;
                    }
                    k += 3;
                    LinkId { kind: idlq, ecc: Some(b[k - 3]), id: be(&b[k - 2..k]) }
                } else {
                    if k + 2 > b.len() {
                        return;
                    }
                    k += 2;
                    LinkId { kind: idlq, ecc: None, id: be(&b[k - 2..k]) }
                };
                ids.push(id);
            }
            if !self.links.contains_key(&key) && self.links.len() >= MAX_ENTRIES {
                continue;
            }
            let set = self.links.entry(key).or_default();
            set.active = active;
            set.hard = hard;
            set.international = ils;
            let mut ids = ids.into_iter();
            if !cn {
                // The key service is first, always a DAB SId.
                set.key = ids.next().map(|i| i.id);
                set.ids.clear();
            }
            for i in ids {
                if set.ids.len() < 64 && !set.ids.contains(&i) {
                    set.ids.push(i);
                }
            }
        }
    }

    /// FIG 0/21, frequency information.
    fn fig0_21(&mut self, oe: bool, b: &[u8]) {
        let mut k = 0;
        while k + 2 <= b.len() {
            let len = (b[k + 1] & 0x1f) as usize;
            k += 2;
            let end = (k + len).min(b.len());
            while k + 3 <= end {
                let id = u16::from_be_bytes([b[k], b[k + 1]]);
                let rm = b[k + 2] >> 4;
                let continuity = b[k + 2] & 0x08 != 0;
                let n = (b[k + 2] & 7) as usize;
                k += 3;
                let list = &b[k..(k + n).min(end)];
                k += n;
                let key = (id, rm, oe);
                if n == 0 {
                    continue; // a change event (module docs)
                }
                let khz: Vec<u32> = match rm {
                    0 => list.chunks_exact(3).map(|c| (be(c) & 0x7ffff) * 16).collect(),
                    8 => list.iter().filter(|&&f| (1..=204).contains(&f)).map(|&f| 87_500 + f as u32 * 100).collect(),
                    6 => list.get(1..).unwrap_or(&[]).chunks_exact(2).map(|c| {
                        let v = u16::from_be_bytes([c[0], c[1]]);
                        (v & 0x7fff) as u32 * if v & 0x8000 != 0 { 10 } else { 1 }
                    }).collect(),
                    14 => list.get(1..).unwrap_or(&[]).chunks_exact(2).map(|c| (u16::from_be_bytes([c[0], c[1]]) & 0x7fff) as u32).collect(),
                    _ => continue,
                };
                if !self.freqs.contains_key(&key) && self.freqs.len() >= MAX_ENTRIES {
                    continue;
                }
                let fi = self.freqs.entry(key).or_insert_with(|| FreqInfo { id, rm, continuity, other_ensemble: oe, khz: Vec::new() });
                fi.continuity = continuity;
                for f in khz {
                    if f > 0 && fi.khz.len() < 32 && !fi.khz.contains(&f) {
                        fi.khz.push(f);
                    }
                }
            }
            k = end;
        }
    }

    fn fig1(&mut self, d: &[u8]) {
        if d.is_empty() {
            return;
        }
        let charset = d[0] >> 4;
        let oe = d[0] & 0x08 != 0;
        let ext = d[0] & 7;
        let b = &d[1..];
        if oe {
            return;
        }
        // The identifier: EId, SId (16 or 32 bits), or for a component
        // label a byte with P/D and SCIdS before the SId.
        let (id_len, scids, sid_at) = match ext {
            0 | 1 => (2, None, 0),
            5 => (4, None, 0),
            4 if !b.is_empty() => (if b[0] & 0x80 != 0 { 5 } else { 3 }, Some(b[0] & 0x0f), 1),
            _ => return,
        };
        if b.len() < id_len + 18 {
            return;
        }
        let id = be(&b[sid_at..id_len]);
        let label = tables::label_text(&b[id_len..id_len + 16], charset);
        let flags = u16::from_be_bytes([b[id_len + 16], b[id_len + 17]]);
        let short = short_label(&label, flags);
        match (ext, scids) {
            (0, _) => {
                if self.eid.is_none() || self.eid == Some(id as u16) {
                    self.label = Some(label);
                    self.short_label = short;
                }
            }
            (4, Some(scids)) => {
                if let Some(s) = self.service(id) {
                    if s.component_labels.len() < 16 || s.component_labels.contains_key(&scids) {
                        s.component_labels.insert(scids, label);
                    }
                }
            }
            _ => {
                if let Some(s) = self.service(id) {
                    s.label = Some(label);
                    s.short_label = short;
                }
            }
        }
    }

    /// FIG 2: an extended label's segment (§5.2.2.3).
    fn fig2(&mut self, d: &[u8]) {
        if d.len() < 2 {
            return;
        }
        let toggle = d[0] & 0x80 != 0;
        let seg = ((d[0] >> 4) & 7) as usize;
        let ext = d[0] & 7;
        let b = &d[1..];
        let (id_len, id, scids) = match ext {
            0 | 1 if b.len() >= 2 => (2, be(&b[..2]), None),
            5 if b.len() >= 4 => (4, be(&b[..4]), None),
            4 if !b.is_empty() => {
                let n = if b[0] & 0x80 != 0 { 5 } else { 3 };
                if b.len() < n {
                    return;
                }
                (n, be(&b[1..n]), Some(b[0] & 0x0f))
            }
            _ => return,
        };
        let key = (ext | scids.map(|s| s << 3).unwrap_or(0), id);
        if !self.ext_labels.contains_key(&key) && self.ext_labels.len() >= MAX_ENTRIES {
            return;
        }
        let e = self.ext_labels.entry(key).or_default();
        if e.toggle != toggle {
            *e = ExtLabel { toggle, ..Default::default() };
        }
        e.segs[seg] = Some(b[id_len..].to_vec());
        // Segment 0 starts with the label data field's header: encoding,
        // the segment count, and the short label's character flags.
        let Some(first) = e.segs[0].as_ref() else { return };
        if first.len() < 3 {
            return;
        }
        let count = ((first[0] >> 4) & 7) as usize;
        if !(0..=count).all(|i| e.segs[i].is_some()) {
            return;
        }
        let ucs2 = first[0] & 0x80 != 0;
        let flags = u16::from_be_bytes([first[1], first[2]]);
        let bytes: Vec<u8> = (0..=count).flat_map(|i| {
            let s = e.segs[i].as_ref().unwrap();
            if i == 0 { s[3..].to_vec() } else { s.clone() }
        }).collect();
        let label = tables::label_text(&bytes, if ucs2 { 6 } else { 15 });
        let short = short_label(&label, flags);
        match (ext, scids) {
            (0, _) => {
                if self.eid.is_none() || self.eid == Some(id as u16) {
                    self.ext_label = Some(label);
                    self.ext_short_label = short;
                }
            }
            (4, Some(scids)) => {
                if let Some(s) = self.service(id) {
                    if s.component_ext_labels.len() < 16 || s.component_ext_labels.contains_key(&scids) {
                        s.component_ext_labels.insert(scids, label);
                    }
                }
            }
            _ => {
                if let Some(s) = self.service(id) {
                    s.ext_label = Some(label);
                    s.ext_short_label = short;
                }
            }
        }
    }
}

/// Decodes the FIC's CIF blocks; counts what passes its CRC.
pub struct FicDecoder {
    map: Vec<usize>,
    prbs: Vec<u8>,
    viterbi: Viterbi,
    pub fibs_ok: u64,
    pub fibs_bad: u64,
}

impl Default for FicDecoder {
    fn default() -> Self {
        Self::new()
    }
}

impl FicDecoder {
    pub fn new() -> Self {
        FicDecoder {
            map: coding::puncture_map(&tables::fic_profile()),
            prbs: coding::prbs(FIC_INFO_BITS),
            viterbi: Viterbi::new(),
            fibs_ok: 0,
            fibs_bad: 0,
        }
    }

    /// One CIF's 2304 soft bits: the FIBs whose CRC holds, each 30 bytes.
    pub fn decode(&mut self, soft: &[Soft]) -> Vec<Vec<u8>> {
        let mut bits = self.viterbi.decode(soft, &self.map, FIC_INFO_BITS);
        for (b, p) in bits.iter_mut().zip(&self.prbs) {
            *b ^= p;
        }
        let bytes = coding::pack(&bits);
        let mut out = Vec::new();
        for fib in bytes.chunks(FIB_BITS / 8) {
            if coding::crc16_ok(fib) {
                self.fibs_ok += 1;
                out.push(fib[..30].to_vec());
            } else {
                self.fibs_bad += 1;
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dab::tx::fig;

    /// One FIB's data field holding these FIGs.
    fn parse(e: &mut Ensemble, figs: &[Vec<u8>]) {
        let mut b: Vec<u8> = figs.concat();
        assert!(b.len() <= 30, "a FIB holds 30 bytes");
        if b.len() < 30 {
            b.push(0xff);
        }
        b.resize(30, 0);
        e.parse_fib(&b);
    }

    /// The test ensemble's basics: C221 with an audio component on
    /// sub-channel 1, and a data service E1C00001 with a packet one (SCId 0x123).
    fn basics() -> Ensemble {
        let mut e = Ensemble::default();
        parse(&mut e, &[fig(0, vec![0x00, 0xc1, 0xce, 0, 0]), fig(0, vec![0x02, 0xc2, 0x21, 0x01, 0x00, 0x06])]);
        parse(&mut e, &[fig(0, vec![0x22, 0xe1, 0xc0, 0x00, 0x01, 0x01, 0xc4, 0x8e]), fig(0, vec![0x03, 0x12, 0x30, 60, 0x0c, 0x01])]);
        e
    }

    #[test]
    fn programme_type_language_and_component_labels() {
        let mut e = basics();
        // FIG 0/17: C221, dynamic, PTy 10. An old multiplexer's form with
        // the L and CC flags: a language byte before the code, a CC after.
        parse(&mut e, &[fig(0, vec![0x11, 0xc2, 0x21, 0x80, 0x0a]), fig(0, vec![0x11, 0xc2, 0x22, 0x30, 0x09, 0x05, 0x01])]);
        assert_eq!(e.services[&0xc221].pty, Some(Pty { code: 10, dynamic: true }));
        assert_eq!(e.services[&0xc222].pty, Some(Pty { code: 5, dynamic: false }));
        // FIG 0/5 short form: sub-channel 1 is English (0x09).
        parse(&mut e, &[fig(0, vec![0x05, 0x01, 0x09])]);
        let c = e.services[&0xc221].audio().unwrap().clone();
        assert_eq!(e.language_of(&c), Some(0x09));
        // FIG 1/4: C221's component 2.
        let mut d = vec![0x04, 0x02, 0xc2, 0x21];
        d.extend(b"Shack Sport     ");
        d.extend([0xff, 0x00]);
        parse(&mut e, &[fig(1, d)]);
        assert_eq!(e.services[&0xc221].component_label(2).map(String::as_str), Some("Shack Sport"));
    }

    #[test]
    fn country_time_offset_and_the_clock() {
        let mut e = basics();
        // FIG 0/9: LTO +1 h (two half hours), ECC E1, table 1.
        parse(&mut e, &[fig(0, vec![0x09, 0x02, 0xe1, 0x01])]);
        assert_eq!((e.ecc, e.lto, e.inter_table), (Some(0xe1), Some(2), Some(1)));
        // FIG 0/10, long form: MJD 61336 (2026-10-23) 12:34:56.
        let v: u32 = 61_336 << 14 | 1 << 11 | 12 << 6 | 34;
        let mut d = vec![0x0a];
        d.extend(v.to_be_bytes());
        d.extend([56 << 2, 0]);
        parse(&mut e, &[fig(0, d)]);
        assert_eq!(crate::dab::codes::iso_utc(e.utc().unwrap()), "2026-10-23T12:34:56Z");
    }

    #[test]
    fn user_applications_say_where_slideshow_and_spi_are() {
        let mut e = basics();
        // FIG 0/13: C221's audio carries SlideShow in X-PAD (app type 12, DG, DSCTy 60).
        parse(&mut e, &[fig(0, vec![0x0d, 0xc2, 0x21, 0x01, 0x00, 0x42, 0x0c, 0x3c])]);
        // ...and the data service is SPI (basic profile).
        parse(&mut e, &[fig(0, vec![0x2d, 0xe1, 0xc0, 0x00, 0x01, 0x01, 0x00, 0xe1, 0x01])]);
        assert!(e.services[&0xc221].has_app(crate::dab::codes::UA_SLIDESHOW));
        assert_eq!(e.app_carrier(0xc221, crate::dab::codes::UA_SLIDESHOW), Some(None));
        let (sid, pc) = e.spi().expect("SPI");
        assert_eq!((sid, pc.subch, pc.address, pc.dscty, pc.data_groups), (0xe1c0_0001, 3, 1, 60, true));
    }

    #[test]
    fn announcements_are_listed_and_end_with_zero_flags() {
        let mut e = basics();
        // FIG 0/18: C221 may be interrupted by traffic and news, cluster 5.
        parse(&mut e, &[fig(0, vec![0x12, 0xc2, 0x21, 0x00, 0x12, 0x01, 0x05])]);
        assert_eq!((e.services[&0xc221].asu, e.services[&0xc221].clusters.clone()), (0x12, vec![5]));
        // FIG 0/19: a traffic announcement on sub-channel 4 in cluster 5, then its end.
        parse(&mut e, &[fig(0, vec![0x13, 0x05, 0x00, 0x02, 0x84])]);
        assert_eq!(e.switching.get(&5), Some(&Switching { flags: 2, subch: 4, new: true }));
        parse(&mut e, &[fig(0, vec![0x13, 0x05, 0x00, 0x00, 0x04])]);
        assert!(e.switching.is_empty());
    }

    #[test]
    fn extended_labels_in_two_segments_win_over_fig_1() {
        let mut e = basics();
        let mut d = vec![0x01, 0xc2, 0x21];
        d.extend(b"Shack MP2 Tone  ");
        d.extend([0xff, 0x00]);
        parse(&mut e, &[fig(1, d)]);
        // FIG 2/1, UTF-8 "Radio Zürich Süd" (18 bytes) in two segments.
        let label = "Radio Zürich Süd".as_bytes();
        let mut s0 = vec![0x01, 0xc2, 0x21, 0x10, 0xf8, 0x00];
        s0.extend(&label[..10]);
        let mut s1 = vec![0x11, 0xc2, 0x21];
        s1.extend(&label[10..]);
        parse(&mut e, &[fig(2, s0)]);
        assert_eq!(e.services[&0xc221].best_label().map(String::as_str), Some("Shack MP2 Tone"), "half a label is not shown");
        parse(&mut e, &[fig(2, s1)]);
        let s = &e.services[&0xc221];
        assert_eq!(s.best_label().map(String::as_str), Some("Radio Zürich Süd"));
        assert_eq!(s.best_short_label().map(String::as_str), Some("Radio"));
    }

    #[test]
    fn service_following_lists_links_frequencies_and_other_ensembles() {
        let mut e = basics();
        // FIG 0/6: a hard, active, national link set 0x011 whose key is
        // C221, with RDS PI C201 (IdLQ 1).
        parse(&mut e, &[fig(0, vec![0x06, 0xe0, 0x11, 0x22, 0xc2, 0x21, 0xc2, 0x01])]);
        let set = e.links.values().next().unwrap();
        assert_eq!((set.key, set.hard, set.active), (Some(0xc221), true, true));
        assert_eq!(set.ids, vec![LinkId { kind: 1, ecc: None, id: 0xc201 }]);
        // FIG 0/21: PI C201 on FM 97.6 and 104.9 MHz; ensemble C1D0 on 12B.
        parse(&mut e, &[fig(0, vec![0x15, 0x00, 0x05, 0xc2, 0x01, 0x82, 101, 174])]);
        let f = 225_648_000u32 / 16_000;
        parse(&mut e, &[fig(0, vec![0x55, 0x00, 0x06, 0xc1, 0xd0, 0x03, 0x00 | (f >> 16) as u8, (f >> 8) as u8, f as u8])]);
        assert_eq!(e.freqs[&(0xc201, 8, false)].khz, vec![97_600, 104_900]);
        assert_eq!(e.freqs[&(0xc1d0, 0, true)].khz, vec![225_648]);
        // FIG 0/24: C221 is also in ensemble C1D0.
        parse(&mut e, &[fig(0, vec![0x58, 0xc2, 0x21, 0x01, 0xc1, 0xd0])]);
        assert_eq!(e.oe_services[&0xc221], vec![0xc1d0]);
    }
}
