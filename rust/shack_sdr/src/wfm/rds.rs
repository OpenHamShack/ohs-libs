//! RDS above the bit level (EN 62106, the RDS standard, which this follows
//! directly): blocks, their checkwords and offset words, block sync, burst
//! correction, groups, and the fields a listener sees, held back until they
//! are stable.
//!
//! - **Blocks** are 26 bits: 16 of information and a 10-bit checkword from
//!   the shortened cyclic code g(x) = x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1,
//!   with one of five offset words (A, B, C, C', D) added to say where in
//!   the group the block sits. The remainder of a received block modulo
//!   g(x) is then exactly its offset word when nothing is wrong, so sync
//!   and checking are one division.
//! - **Correction.** The code corrects any burst of up to 5 bits, but every
//!   correction is a chance to accept a wrong block, and a wrong PI or a
//!   wrong letter in PS is worse than a missing one. So bursts of up to
//!   `MAX_BURST` (2) bits are corrected, only in sync, the trade redsea
//!   (MIT, github.com/windytan/redsea, read 23/10/2026) makes; and only
//!   while at most `CORRECT_MAX_BAD` of the last 50 blocks failed. On a
//!   failing signal most "errors" are noise, and 52 of the 1024 syndromes
//!   are correctable, so correcting there accepted one garbage block in
//!   twenty (tests/wfm.rs `rds_block_error_rate_against_cnr_and_never_a_wrong_group`
//!   found it). And a correction must flip only bits the demodulator was
//!   least sure of (among the block's three weakest): one wrong symbol
//!   flips two adjacent data bits through the differential decoding, both
//!   weak, while a syndrome that merely looks like a burst points at bits
//!   that were heard clearly. Without that, one correction in a few hundred
//!   was wrong at 12 dB.
//! - **Sync** is two blocks whose offsets agree with their distance apart;
//!   it is dropped after `LOSE_AFTER` blocks in a row fail, so a bit slip
//!   is found again within about a quarter of a second.
//! - **Stable fields.** PS and RadioText arrive a segment at a time, and a
//!   field shown as it arrives flickers with every error and every change
//!   of text. A segment counts once it has been received twice with the
//!   same characters; when any segment changes, every other one needs one
//!   more confirmation, so an old half and a new half are never shown
//!   together. PS is published only when all four segments are confirmed,
//!   RadioText when every segment up to its end (a carriage return, or all
//!   sixteen) is. PI, PTY, TP and TA need two agreeing groups, an
//!   alternative frequency two sightings, and the clock a group whose four
//!   blocks all arrived clean (it comes once a minute, so it cannot wait
//!   for a second). Everything clears on `reset` (a retune), and when a
//!   new PI is confirmed.
//! - **PTY** is a number (0-31) on the European table; the Python face
//!   names it through the shack-sdr catalog (python/shack_sdr/wfm.py), as
//!   Rust carries no text. RBDS's North American table is not used.
//! - **Characters** use the RDS G0 table (EN 62106 Annex E), as redsea's
//!   `getRDSCharString` lays it out.

pub const POLY: u32 = 0x5B9;
/// Offset words A, B, C, C', D.
pub const OFFSETS: [u16; 5] = [0x0FC, 0x198, 0x168, 0x350, 0x1B4];
pub const A: usize = 0;
pub const B: usize = 1;
pub const C: usize = 2;
pub const CP: usize = 3;
pub const D: usize = 4;

pub const MAX_BURST: u32 = 2;
const LOSE_AFTER: u32 = 10;
const CORRECT_MAX_BAD: u32 = 10;

/// The remainder of a 26-bit word modulo g(x).
pub fn syndrome(word: u32) -> u16 {
    let mut r = word & 0x3FF_FFFF;
    for i in (10..26).rev() {
        if r & (1 << i) != 0 {
            r ^= POLY << (i - 10);
        }
    }
    r as u16
}

/// A block as sent: 16 information bits, then checkword plus offset.
pub fn encode_block(info: u16, offset: usize) -> u32 {
    let cw = syndrome((info as u32) << 10) ^ OFFSETS[offset];
    ((info as u32) << 10) | cw as u32
}

/// For each syndrome, the error burst that gives it and its length, for
/// bursts of up to 5 bits (all distinct: the code's promise, which a test
/// checks).
fn bursts() -> &'static [(u32, u8); 1024] {
    static T: std::sync::OnceLock<Box<[(u32, u8); 1024]>> = std::sync::OnceLock::new();
    T.get_or_init(|| {
        let mut t = Box::new([(0u32, 0u8); 1024]);
        for len in 1..=5u32 {
            for p in (1u32 << (len - 1))..(1u32 << len) {
                if p & 1 == 0 {
                    continue;
                }
                for shift in 0..=(26 - len) {
                    let e = p << shift;
                    let s = syndrome(e) as usize;
                    if t[s].1 == 0 {
                        t[s] = (e, len as u8);
                    }
                }
            }
        }
        t
    })
}

/// The information bits of `word` as a block with offset `offset`,
/// correcting a burst of up to `max_burst` bits. `Some((info, corrected))`.
pub fn check_block(word: u32, offset: usize, max_burst: u32) -> Option<(u16, bool)> {
    check_block_e(word, offset, max_burst).map(|(i, e)| (i, e != 0))
}

/// As `check_block`, with the error pattern corrected (0 for none).
pub fn check_block_e(word: u32, offset: usize, max_burst: u32) -> Option<(u16, u32)> {
    let s = syndrome(word) ^ OFFSETS[offset];
    if s == 0 {
        return Some(((word >> 10) as u16, 0));
    }
    let (e, len) = bursts()[s as usize];
    if len == 0 || len as u32 > max_burst {
        return None;
    }
    Some((((word ^ e) >> 10) as u16, e))
}

/// The RDS G0 character set (EN 62106 Annex E), laid out as redsea's
/// `getRDSCharString` (MIT, Oona Räisänen). Control codes become spaces
/// here; RadioText's carriage return is handled before this.
const G0: [&str; 256] = [
    " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", //
    " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", "\u{AD}", //
    " ", "!", "\"", "#", "¤", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/", //
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?", //
    "@", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", //
    "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "[", "\\", "]", "―", "_", //
    "‖", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", //
    "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "{", "|", "}", "¯", " ", //
    "á", "à", "é", "è", "í", "ì", "ó", "ò", "ú", "ù", "Ñ", "Ç", "Ş", "β", "¡", "Ĳ", //
    "â", "ä", "ê", "ë", "î", "ï", "ô", "ö", "û", "ü", "ñ", "ç", "ş", "ǧ", "ı", "ĳ", //
    "ª", "α", "©", "‰", "Ǧ", "ě", "ň", "ő", "π", "€", "£", "$", "←", "↑", "→", "↓", //
    "º", "¹", "²", "³", "±", "İ", "ń", "ű", "µ", "¿", "÷", "°", "¼", "½", "¾", "§", //
    "Á", "À", "É", "È", "Í", "Ì", "Ó", "Ò", "Ú", "Ù", "Ř", "Č", "Š", "Ž", "Ð", "Ŀ", //
    "Â", "Ä", "Ê", "Ë", "Î", "Ï", "Ô", "Ö", "Û", "Ü", "ř", "č", "š", "ž", "đ", "ŀ", //
    "Ã", "Å", "Æ", "Œ", "ŷ", "Ý", "Õ", "Ø", "Þ", "Ŋ", "Ŕ", "Ć", "Ś", "Ź", "Ŧ", "ð", //
    "ã", "å", "æ", "œ", "ŵ", "ý", "õ", "ø", "þ", "ŋ", "ŕ", "ć", "ś", "ź", "ŧ", " ", //
];

pub fn chars(bytes: &[u8]) -> String {
    bytes.iter().map(|&b| G0[b as usize]).collect()
}

/// The clock-time group's content: UTC and the local offset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ct {
    pub mjd: u32,
    pub hour: u8,
    pub minute: u8,
    /// Local time minus UTC, in half hours.
    pub offset_half_hours: i8,
}

/// Year, month, day of a Modified Julian Day (EN 62106 Annex G's formula).
pub fn mjd_to_date(mjd: u32) -> (i32, u32, u32) {
    let mjd = mjd as f64;
    let yp = ((mjd - 15_078.2) / 365.25).floor();
    let mp = ((mjd - 14_956.1 - (yp * 365.25).floor()) / 30.600_1).floor();
    let day = mjd - 14_956.0 - (yp * 365.25).floor() - (mp * 30.600_1).floor();
    let k = if mp == 14.0 || mp == 15.0 { 1.0 } else { 0.0 };
    ((yp + k + 1900.0) as i32, (mp - 1.0 - k * 12.0) as u32, day as u32)
}

/// The Modified Julian Day of a date (the same annex, the other way).
pub fn date_to_mjd(y: i32, m: u32, d: u32) -> u32 {
    let l = if m <= 2 { 1.0 } else { 0.0 };
    let y = (y - 1900) as f64;
    (14_956.0 + d as f64 + ((y - l) * 365.25).floor() + ((m as f64 + 1.0 + l * 12.0) * 30.600_1).floor()) as u32
}

impl Ct {
    /// Local time with its offset, ISO 8601: "2026-10-23T15:05:00+01:00".
    pub fn iso(&self) -> String {
        let off = self.offset_half_hours as i64 * 30;
        let total = self.mjd as i64 * 1440 + self.hour as i64 * 60 + self.minute as i64 + off;
        let (mjd, mins) = (total.div_euclid(1440), total.rem_euclid(1440));
        let (y, mo, d) = mjd_to_date(mjd as u32);
        let sign = if off < 0 { '-' } else { '+' };
        format!(
            "{y:04}-{mo:02}-{d:02}T{:02}:{:02}:00{sign}{:02}:{:02}",
            mins / 60,
            mins % 60,
            off.abs() / 60,
            off.abs() % 60
        )
    }
}

/// One group as received: its blocks' information, None where a block
/// failed. Block 3 holds C or C' (`c_prime` says which).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Group {
    pub blocks: [Option<u16>; 4],
    pub c_prime: bool,
    /// Which blocks needed correcting.
    pub corrected: [bool; 4],
}

/// Segments of a text field and how often each has been confirmed.
#[derive(Clone, Debug)]
struct Segmented {
    chars: Vec<Option<u8>>,
    count: Vec<u8>,
    per: usize,
}

impl Segmented {
    fn new(segments: usize, per: usize) -> Self {
        Segmented { chars: vec![None; segments * per], count: vec![0; segments], per }
    }

    fn segments(&self) -> usize {
        self.count.len()
    }

    /// A segment received; `chars` its characters.
    fn put(&mut self, seg: usize, chars: &[u8]) {
        let at = seg * self.per;
        let same = chars.iter().enumerate().all(|(i, c)| self.chars[at + i] == Some(*c));
        if same {
            self.count[seg] = self.count[seg].saturating_add(1);
        } else {
            for (i, c) in chars.iter().enumerate() {
                self.chars[at + i] = Some(*c);
            }
            // Every other segment needs one more look before an old half
            // can stand beside this new one.
            for (j, n) in self.count.iter_mut().enumerate() {
                if j != seg {
                    *n = (*n).min(1);
                }
            }
            self.count[seg] = 1;
        }
    }

    fn confirmed(&self, seg: usize) -> bool {
        self.count[seg] >= 2
    }
}

/// Everything decoded so far, as published (stable) values.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Fields {
    pub pi: Option<u16>,
    pub pty: Option<u8>,
    pub tp: Option<bool>,
    pub ta: Option<bool>,
    pub ps: Option<String>,
    pub rt: Option<String>,
    pub ct: Option<Ct>,
    /// Alternative frequencies heard, in Hz, sorted.
    pub af: Vec<u32>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Counts {
    /// Blocks received in sync: clean, corrected, failed.
    pub ok: u64,
    pub corrected: u64,
    pub bad: u64,
    pub groups: u64,
    pub syncs: u64,
}

/// Bits in, groups and fields out.
pub struct RdsDecoder {
    reg: u32,
    bits: u64,
    synced: bool,
    /// Sync search: (bit count, offset index) of recent valid blocks.
    found: Vec<(u64, usize)>,
    /// The bit count at which the next block ends, and its position (0-3).
    next_end: u64,
    next_pos: usize,
    fails_in_row: u32,
    /// The last 50 blocks' outcomes, a set bit for a failure.
    recent: u64,
    /// The last 32 bits' confidences, newest at `bits % 32`.
    conf: [f32; 32],
    cur: Group,
    pub max_burst: u32,
    pub counts: Counts,
    fields: Fields,
    rev: u64,
    pi_cand: Option<u16>,
    pty_cand: Option<u8>,
    tp_cand: Option<bool>,
    ta_cand: Option<bool>,
    af_seen: Vec<u32>,
    ps: Segmented,
    rt: Segmented,
    rt_ab: Option<bool>,
    rt_long: bool,
    /// Groups decoded, for whoever wants them (tests; the frame output).
    pub groups: Vec<Group>,
    pub keep_groups: bool,
}

impl Default for RdsDecoder {
    fn default() -> Self {
        Self::new()
    }
}

impl RdsDecoder {
    pub fn new() -> Self {
        RdsDecoder {
            reg: 0,
            bits: 0,
            synced: false,
            found: Vec::new(),
            next_end: 0,
            next_pos: 0,
            fails_in_row: 0,
            recent: 0,
            conf: [1.0; 32],
            cur: Group::default(),
            max_burst: MAX_BURST,
            counts: Counts::default(),
            fields: Fields::default(),
            rev: 0,
            pi_cand: None,
            pty_cand: None,
            tp_cand: None,
            ta_cand: None,
            af_seen: Vec::new(),
            ps: Segmented::new(4, 2),
            rt: Segmented::new(16, 4),
            rt_ab: None,
            rt_long: true,
            groups: Vec::new(),
            keep_groups: false,
        }
    }

    /// Forget everything: a retune.
    pub fn reset(&mut self) {
        let (mb, keep, counts) = (self.max_burst, self.keep_groups, self.counts);
        *self = RdsDecoder::new();
        self.max_burst = mb;
        self.keep_groups = keep;
        self.counts = Counts { syncs: counts.syncs, ..Counts::default() };
    }

    pub fn synced(&self) -> bool {
        self.synced
    }

    pub fn fields(&self) -> &Fields {
        &self.fields
    }

    /// Rises by one whenever a published field changes.
    pub fn rev(&self) -> u64 {
        self.rev
    }

    /// One (data, not differentially coded) bit, as sure as any other.
    pub fn push(&mut self, bit: bool) {
        self.push_soft(bit, 1.0);
    }

    /// The confidence of the bit received `ago` bits before the newest.
    fn conf_ago(&self, ago: u32) -> f32 {
        self.conf[((self.bits - 1 - ago as u64) % 32) as usize]
    }

    /// Whether correcting `e` flips only the block's weakest bits.
    fn plausible(&self, e: u32) -> bool {
        if e == 0 {
            return true;
        }
        let mut c: Vec<f32> = (0..26).map(|k| self.conf_ago(k)).collect();
        let worst_flipped = (0..26).filter(|k| e >> k & 1 != 0).map(|k| c[k as usize]).fold(0.0f32, f32::max);
        c.sort_by(|a, b| a.total_cmp(b));
        worst_flipped <= c[2]
    }

    /// One data bit and how sure the demodulator was of it (any scale).
    pub fn push_soft(&mut self, bit: bool, conf: f32) {
        self.reg = ((self.reg << 1) | bit as u32) & 0x3FF_FFFF;
        self.conf[(self.bits % 32) as usize] = conf;
        self.bits += 1;
        if !self.synced {
            self.search();
            return;
        }
        if self.bits < self.next_end {
            return;
        }
        let pos = self.next_pos;
        let word = self.reg;
        let burst = if (self.recent & ((1 << 50) - 1)).count_ones() <= CORRECT_MAX_BAD { self.max_burst } else { 0 };
        let try_offset = |k: usize| check_block_e(word, k, burst).filter(|(_, e)| self.plausible(*e));
        let got = if pos == 2 {
            try_offset(C).map(|r| (r, false)).or_else(|| try_offset(CP).map(|r| (r, true)))
        } else {
            try_offset([A, B, C, D][pos]).map(|r| (r, false))
        };
        self.recent = (self.recent << 1) | got.is_none() as u64;
        match got {
            Some(((info, e), cp)) => {
                let corrected = e != 0;
                if corrected {
                    self.counts.corrected += 1;
                } else {
                    self.counts.ok += 1;
                }
                self.fails_in_row = 0;
                self.cur.blocks[pos] = Some(info);
                self.cur.corrected[pos] = corrected;
                if pos == 2 {
                    self.cur.c_prime = cp;
                }
            }
            None => {
                self.counts.bad += 1;
                self.fails_in_row += 1;
            }
        }
        if pos == 3 {
            let g = std::mem::take(&mut self.cur);
            self.group(g);
        }
        self.next_pos = (pos + 1) % 4;
        self.next_end += 26;
        if self.fails_in_row >= LOSE_AFTER {
            self.synced = false;
            self.found.clear();
            self.cur = Group::default();
        }
    }

    fn search(&mut self) {
        if self.bits < 26 {
            return;
        }
        let s = syndrome(self.reg);
        let Some(k) = OFFSETS.iter().position(|&o| o == s) else {
            return;
        };
        let pos = [0, 1, 2, 2, 3][k];
        self.found.retain(|(b, _)| self.bits - b <= 26 * 8);
        for &(b, kp) in &self.found {
            let dist = self.bits - b;
            if dist % 26 != 0 {
                continue;
            }
            let ppos = [0, 1, 2, 2, 3][kp];
            if (ppos + (dist / 26) as usize) % 4 == pos {
                self.synced = true;
                self.counts.syncs += 1;
                self.fails_in_row = 0;
                self.cur = Group::default();
                if let Some((info, _)) = check_block(self.reg, k, 0) {
                    self.cur.blocks[pos] = Some(info);
                    self.cur.c_prime = k == CP;
                }
                self.counts.ok += 1;
                if pos == 3 {
                    let g = std::mem::take(&mut self.cur);
                    self.group(g);
                }
                self.next_pos = (pos + 1) % 4;
                self.next_end = self.bits + 26;
                self.found.clear();
                return;
            }
        }
        self.found.push((self.bits, k));
    }

    fn changed(&mut self) {
        self.rev += 1;
    }

    fn group(&mut self, g: Group) {
        if g.blocks.iter().all(|b| b.is_none()) {
            return;
        }
        self.counts.groups += 1;
        if self.keep_groups {
            self.groups.push(g);
        }
        let pi = g.blocks[0].or(if g.c_prime { g.blocks[2] } else { None });
        if let Some(pi) = pi {
            if self.pi_cand == Some(pi) && self.fields.pi != Some(pi) {
                // A new station on the same frequency (or the first): its
                // fields are not the old one's.
                if self.fields.pi.is_some() {
                    self.forget_station();
                }
                self.fields.pi = Some(pi);
                self.changed();
            }
            self.pi_cand = Some(pi);
        }
        let Some(b) = g.blocks[1] else {
            return;
        };
        let gtype = (b >> 12) as u8;
        let version_b = b & 0x0800 != 0;
        let tp = b & 0x0400 != 0;
        let pty = ((b >> 5) & 0x1F) as u8;
        if self.pty_cand == Some(pty) && self.fields.pty != Some(pty) {
            self.fields.pty = Some(pty);
            self.changed();
        }
        self.pty_cand = Some(pty);
        if self.tp_cand == Some(tp) && self.fields.tp != Some(tp) {
            self.fields.tp = Some(tp);
            self.changed();
        }
        self.tp_cand = Some(tp);
        match (gtype, version_b) {
            (0, _) => self.group0(b, g, version_b),
            (2, _) => self.group2(b, g, version_b),
            (4, false) => self.group4a(b, g),
            _ => {}
        }
    }

    fn forget_station(&mut self) {
        let keep = (self.max_burst, self.keep_groups, self.counts, std::mem::take(&mut self.groups), self.rev);
        *self = RdsDecoder { synced: self.synced, next_end: self.next_end, next_pos: self.next_pos, reg: self.reg, bits: self.bits, ..RdsDecoder::new() };
        (self.max_burst, self.keep_groups, self.counts, self.groups, self.rev) = keep;
    }

    fn group0(&mut self, b: u16, g: Group, version_b: bool) {
        let ta = b & 0x0010 != 0;
        if self.ta_cand == Some(ta) && self.fields.ta != Some(ta) {
            self.fields.ta = Some(ta);
            self.changed();
        }
        self.ta_cand = Some(ta);
        if !version_b && !g.corrected[1] && !g.corrected[2] {
            if let Some(c) = g.blocks[2] {
                let mut af = self.fields.af.clone();
                for code in [(c >> 8) as u8, c as u8] {
                    if (1..=204).contains(&code) {
                        let hz = 87_500_000 + code as u32 * 100_000;
                        if self.af_seen.contains(&hz) {
                            if !af.contains(&hz) {
                                af.push(hz);
                            }
                        } else if self.af_seen.len() < 64 {
                            self.af_seen.push(hz);
                        }
                    }
                }
                af.sort_unstable();
                if af != self.fields.af {
                    self.fields.af = af;
                    self.changed();
                }
            }
        }
        if let Some(d) = g.blocks[3] {
            let seg = (b & 3) as usize;
            self.ps.put(seg, &[(d >> 8) as u8, d as u8]);
            if (0..4).all(|s| self.ps.confirmed(s)) {
                let bytes: Vec<u8> = self.ps.chars.iter().map(|c| c.unwrap_or(b' ')).collect();
                let ps = chars(&bytes);
                if self.fields.ps.as_deref() != Some(ps.as_str()) {
                    self.fields.ps = Some(ps);
                    self.changed();
                }
            }
        }
    }

    fn group2(&mut self, b: u16, g: Group, version_b: bool) {
        let ab = b & 0x0010 != 0;
        let long = !version_b;
        if self.rt_ab != Some(ab) || self.rt_long != long {
            // A new text (or the other version's): start collecting afresh,
            // and keep showing the old one until the new one is whole.
            self.rt = if long { Segmented::new(16, 4) } else { Segmented::new(16, 2) };
            self.rt_ab = Some(ab);
            self.rt_long = long;
        }
        let seg = (b & 0x0F) as usize;
        let got: Option<Vec<u8>> = if long {
            match (g.blocks[2], g.blocks[3]) {
                (Some(c), Some(d)) => Some(vec![(c >> 8) as u8, c as u8, (d >> 8) as u8, d as u8]),
                _ => None,
            }
        } else {
            g.blocks[3].map(|d| vec![(d >> 8) as u8, d as u8])
        };
        let Some(got) = got else {
            return;
        };
        self.rt.put(seg, &got);
        self.publish_rt();
    }

    fn publish_rt(&mut self) {
        let per = self.rt.per;
        // The end: the first carriage return among the characters held.
        let end = self.rt.chars.iter().position(|c| *c == Some(0x0D));
        let last_seg = match end {
            Some(i) => i / per,
            None => self.rt.segments() - 1,
        };
        let mut whole = (0..=last_seg).all(|s| self.rt.confirmed(s));
        if !whole && end.is_none() {
            // A station that sends fewer segments and no carriage return:
            // if everything up to the highest segment ever heard has been
            // confirmed three times over, that is all there is.
            let top = (0..self.rt.segments()).rev().find(|&s| self.rt.count[s] > 0);
            if let Some(top) = top {
                if (0..=top).all(|s| self.rt.count[s] >= 3) {
                    whole = true;
                }
            }
        }
        if !whole {
            return;
        }
        let n = end.unwrap_or_else(|| {
            let top = (0..self.rt.segments()).rev().find(|&s| self.rt.count[s] > 0).unwrap_or(0);
            (top + 1) * per
        });
        let bytes: Vec<u8> = self.rt.chars[..n].iter().map(|c| c.unwrap_or(b' ')).collect();
        let rt = chars(&bytes).trim_end().to_string();
        if self.fields.rt.as_deref() != Some(rt.as_str()) {
            self.fields.rt = Some(rt);
            self.changed();
        }
    }

    fn group4a(&mut self, b: u16, g: Group) {
        let (Some(c), Some(d)) = (g.blocks[2], g.blocks[3]) else {
            return;
        };
        if g.blocks[0].is_none() || g.corrected.iter().any(|&c| c) {
            return;
        }
        let mjd = (((b & 3) as u32) << 15) | ((c >> 1) as u32);
        let hour = (((c & 1) << 4) | (d >> 12)) as u8;
        let minute = ((d >> 6) & 0x3F) as u8;
        let off = (d & 0x1F) as i8;
        let off = if d & 0x20 != 0 { -off } else { off };
        if hour > 23 || minute > 59 || off.abs() > 28 || mjd < 40_000 {
            return;
        }
        let ct = Ct { mjd, hour, minute, offset_half_hours: off };
        if self.fields.ct != Some(ct) {
            self.fields.ct = Some(ct);
            self.changed();
        }
    }
}

// ------------------------------------------------------------------ encoding

/// A station's RDS as a transmitter would send it, for tests: groups 0A
/// (PS, TA, AF), 2A (RadioText) and 4A (clock), in a fixed rotation.
#[derive(Clone, Debug)]
pub struct Station {
    pub pi: u16,
    pub pty: u8,
    pub tp: bool,
    pub ta: bool,
    pub ps: [u8; 8],
    pub rt: Vec<u8>,
    pub rt_ab: bool,
    pub af: Vec<u32>,
    pub ct: Option<Ct>,
}

impl Station {
    pub fn new(pi: u16, ps: &str, pty: u8) -> Self {
        let mut p = [b' '; 8];
        for (i, c) in ps.bytes().take(8).enumerate() {
            p[i] = c;
        }
        Station { pi, pty, tp: false, ta: false, ps: p, rt: Vec::new(), rt_ab: false, af: Vec::new(), ct: None }
    }

    fn block_b(&self, gtype: u16, version_b: bool, low5: u16) -> u16 {
        (gtype << 12) | ((version_b as u16) << 11) | ((self.tp as u16) << 10) | ((self.pty as u16 & 0x1F) << 5) | (low5 & 0x1F)
    }

    pub fn group0a(&self, seg: usize) -> [u16; 4] {
        let b = self.block_b(0, false, ((self.ta as u16) << 4) | (1 << 3) | seg as u16);
        let code = |i: usize| -> u16 {
            self.af.get(i).map(|&hz| ((hz.saturating_sub(87_500_000)) / 100_000) as u16).unwrap_or(205)
        };
        // AF method A: the count first (224 + n), then the list, two a group.
        let c = if self.af.is_empty() {
            (224 << 8) | 205
        } else {
            let k = seg % self.af.len().div_ceil(2).max(1);
            if k == 0 {
                ((224 + self.af.len() as u16) << 8) | code(0)
            } else {
                (code(2 * k - 1) << 8) | code(2 * k)
            }
        };
        let d = ((self.ps[2 * seg] as u16) << 8) | self.ps[2 * seg + 1] as u16;
        [self.pi, b, c, d]
    }

    /// RadioText's segments, padded with a carriage return as the standard asks.
    pub fn rt_segments(&self) -> usize {
        if self.rt.is_empty() {
            0
        } else {
            (self.rt.len() + 1).min(64).div_ceil(4)
        }
    }

    pub fn group2a(&self, seg: usize) -> [u16; 4] {
        let mut text = self.rt.clone();
        if text.len() < 64 {
            text.push(0x0D);
        }
        text.resize(64, b' ');
        let ch = |i: usize| text[4 * seg + i] as u16;
        let b = self.block_b(2, false, ((self.rt_ab as u16) << 4) | seg as u16);
        [self.pi, b, (ch(0) << 8) | ch(1), (ch(2) << 8) | ch(3)]
    }

    pub fn group4a(&self) -> Option<[u16; 4]> {
        let ct = self.ct?;
        let b = self.block_b(4, false, ((ct.mjd >> 15) & 3) as u16);
        let c = (((ct.mjd & 0x7FFF) as u16) << 1) | ((ct.hour as u16 >> 4) & 1);
        let off = ct.offset_half_hours;
        let d = ((ct.hour as u16 & 0xF) << 12) | ((ct.minute as u16) << 6) | (((off < 0) as u16) << 5) | (off.unsigned_abs() as u16 & 0x1F);
        Some([self.pi, b, c, d])
    }

    /// The `n`-th group of the rotation: 0A for each PS segment, then two
    /// RadioText segments, repeated; a clock group slipped in every 25th,
    /// so it never stands in for the same segment twice.
    pub fn group(&self, n: usize) -> [u16; 4] {
        if n % 25 == 24 {
            if let Some(g) = self.group4a() {
                return g;
            }
        }
        let n = n - n / 25;
        let cycle = n / 6;
        let k = n % 6;
        let nrt = self.rt_segments();
        if k < 4 || nrt == 0 {
            self.group0a(k % 4)
        } else {
            self.group2a((2 * cycle + (k - 4)) % nrt)
        }
    }
}

/// A group's 104 bits, as sent (before differential coding).
pub fn group_bits(g: [u16; 4]) -> Vec<bool> {
    let mut out = Vec::with_capacity(104);
    for (i, info) in g.iter().enumerate() {
        let w = encode_block(*info, [A, B, C, D][i]);
        for k in (0..26).rev() {
            out.push(w >> k & 1 != 0);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_burst_up_to_five_bits_has_its_own_syndrome() {
        let t = bursts();
        let n = t.iter().filter(|e| e.1 > 0).count();
        // 26 + 25 + 2*24 + 4*23 + 8*22 bursts, each with a syndrome of its own.
        assert_eq!(n, 26 + 25 + 48 + 92 + 176);
    }

    #[test]
    fn blocks_check_and_bursts_correct() {
        for (k, info) in [(A, 0xC201u16), (B, 0x0408), (C, 0x1234), (CP, 0xC201), (D, 0x4142)] {
            let w = encode_block(info, k);
            assert_eq!(syndrome(w), OFFSETS[k]);
            assert_eq!(check_block(w, k, 0), Some((info, false)));
            assert_eq!(check_block(w ^ (0b11 << 13), k, 2), Some((info, true)));
            assert_eq!(check_block(w ^ (0b101 << 13), k, 2), None);
            assert_eq!(check_block(w ^ (0b101 << 13), k, 5), Some((info, true)));
        }
    }

    #[test]
    fn the_clock_round_trips_through_mjd() {
        // EN 62106 Annex G's own example: 1993-09-06 is MJD 49 236.
        assert_eq!(date_to_mjd(1993, 9, 6), 49_236);
        assert_eq!(mjd_to_date(49_236), (1993, 9, 6));
        let mjd = date_to_mjd(2026, 10, 23);
        assert_eq!(mjd_to_date(mjd), (2026, 10, 23));
        let ct = Ct { mjd, hour: 23, minute: 30, offset_half_hours: 2 };
        assert_eq!(ct.iso(), "2026-10-24T00:30:00+01:00");
        let ct = Ct { mjd: date_to_mjd(2026, 1, 1), hour: 0, minute: 15, offset_half_hours: -10 };
        assert_eq!(ct.iso(), "2025-12-31T19:15:00-05:00");
    }

    fn feed(dec: &mut RdsDecoder, st: &Station, groups: std::ops::Range<usize>) {
        for n in groups {
            for b in group_bits(st.group(n)) {
                dec.push(b);
            }
        }
    }

    #[test]
    fn fields_are_published_only_when_stable_and_clear_on_reset() {
        let mut st = Station::new(0xC201, "TEST FM ", 10);
        st.rt = b"Hello from the test transmitter".to_vec();
        st.af = vec![89_100_000, 91_300_000, 93_500_000];
        st.ct = Some(Ct { mjd: date_to_mjd(2026, 10, 23), hour: 14, minute: 5, offset_half_hours: 2 });
        st.tp = true;
        let mut dec = RdsDecoder::new();
        // Start mid-block, as a receiver does.
        for _ in 0..13 {
            dec.push(true);
        }
        feed(&mut dec, &st, 0..6);
        // One pass of PS is not enough.
        assert_eq!(dec.fields().ps, None);
        feed(&mut dec, &st, 6..100);
        let f = dec.fields();
        assert_eq!(f.pi, Some(0xC201));
        assert_eq!(f.ps.as_deref(), Some("TEST FM "));
        assert_eq!(f.rt.as_deref(), Some("Hello from the test transmitter"));
        assert_eq!(f.pty, Some(10));
        assert_eq!((f.tp, f.ta), (Some(true), Some(false)));
        assert_eq!(f.af, vec![89_100_000, 91_300_000, 93_500_000]);
        assert_eq!(f.ct.unwrap().iso(), "2026-10-23T15:05:00+01:00");
        dec.reset();
        assert_eq!(dec.fields(), &Fields::default());
    }

    #[test]
    fn a_changed_ps_never_shows_half_old_half_new() {
        let mut st = Station::new(0xC201, "AAAAAAAA", 1);
        let mut dec = RdsDecoder::new();
        let mut seen = Vec::new();
        let mut n = 0;
        for (text, groups) in [("AAAAAAAA", 40), ("BBBBBBBB", 40), ("CCCCCCCC", 40)] {
            st.ps.copy_from_slice(text.as_bytes());
            // Change mid-rotation, at an odd group.
            for _ in 0..groups {
                for b in group_bits(st.group(n)) {
                    dec.push(b);
                }
                n += 1;
                if let Some(ps) = &dec.fields().ps {
                    if seen.last() != Some(ps) {
                        seen.push(ps.clone());
                    }
                }
            }
            n += 1;
        }
        assert_eq!(seen, vec!["AAAAAAAA", "BBBBBBBB", "CCCCCCCC"]);
    }
}
