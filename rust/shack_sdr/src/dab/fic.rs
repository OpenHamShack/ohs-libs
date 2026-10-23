//! The Fast Information Channel (EN 300 401 §5.2, §6, §8.1): each CIF's
//! 2304 soft bits back to three FIBs, and the FIGs in them parsed into
//! what an ensemble is: its identifier and label (FIG 0/0, 1/0), its
//! sub-channels (0/1), its services and their audio components (0/2) and
//! their labels and short labels (1/1). Only the current configuration is
//! kept (C/N = 0); "next" announcements wait until they are current.

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

#[derive(Clone, Debug, PartialEq)]
pub struct Component {
    /// 0: MSC stream audio, 1: stream data, 3: packet data.
    pub tmid: u8,
    /// Audio service component type: 0 is DAB (MP2), 63 DAB+ (HE-AAC).
    pub ascty: u8,
    pub subch: u8,
    pub primary: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Service {
    pub sid: u32,
    pub label: Option<String>,
    pub short_label: Option<String>,
    pub components: Vec<Component>,
}

impl Service {
    /// The primary audio component (the first audio one if none says so).
    pub fn audio(&self) -> Option<&Component> {
        let audio = || self.components.iter().filter(|c| c.tmid == 0);
        audio().find(|c| c.primary).or_else(|| audio().next())
    }
}

/// Everything the FIC has told us.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Ensemble {
    pub eid: Option<u16>,
    pub label: Option<String>,
    pub short_label: Option<String>,
    pub subchannels: BTreeMap<u8, Subchannel>,
    pub services: BTreeMap<u32, Service>,
    /// The latest CIF count (0-4999) FIG 0/0 gave.
    pub cif_count: Option<u16>,
}

/// The short label: the characters the flag field marks (§5.2.2.2).
fn short_label(label: &str, flags: u16) -> Option<String> {
    let s: String = label.chars().enumerate().filter(|(i, _)| *i < 16 && flags >> (15 - i) & 1 == 1).map(|(_, c)| c).collect();
    (!s.is_empty()).then(|| s.trim_end().to_string())
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
                _ => {} // FIG 2 (extended labels), 6 (CA): not used yet
            }
            i = end;
        }
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
                let sid_len = if pd { 4 } else { 2 };
                let mut k = 0;
                while k + sid_len < b.len() {
                    let sid = b[k..k + sid_len].iter().fold(0u32, |a, &x| a << 8 | x as u32);
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
                        if tmid == 0 || tmid == 1 {
                            comps.push(Component { tmid, ascty: x & 0x3f, subch: y >> 2, primary });
                        }
                        k += 2;
                    }
                    let s = self.services.entry(sid).or_insert_with(|| Service { sid, ..Default::default() });
                    s.components = comps;
                }
            }
            _ => {}
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
        let id_len = match ext {
            0 | 1 => 2,
            5 => 4,
            _ => return,
        };
        if b.len() < id_len + 18 {
            return;
        }
        let id = b[..id_len].iter().fold(0u32, |a, &x| a << 8 | x as u32);
        let label = tables::label_text(&b[id_len..id_len + 16], charset);
        let flags = u16::from_be_bytes([b[id_len + 16], b[id_len + 17]]);
        let short = short_label(&label, flags);
        match ext {
            0 => {
                if self.eid.is_none() || self.eid == Some(id as u16) {
                    self.label = Some(label);
                    self.short_label = short;
                }
            }
            _ => {
                let s = self.services.entry(id).or_insert_with(|| Service { sid: id, ..Default::default() });
                s.label = Some(label);
                s.short_label = short;
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
