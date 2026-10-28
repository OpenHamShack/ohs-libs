//! Link control (TS 102 361-2 §7.1, TS 102 361-1 §9.3): who is calling whom,
//! carried whole in a voice call's header and terminator (BPTC(196,96) and
//! RS(12,9)), and in fragments across each voice superframe's bursts B-E
//! (the embedded LC: BPTC(128,72) and a 5-bit checksum) so a receiver that
//! missed the header still learns the call ("late entry"). The embedded LC
//! also carries talker alias: a name or callsign in four LCs, header and
//! three blocks, which this module puts back together.

use super::bptc::{decode196, decode_embedded, encode196, encode_embedded};
use super::fec::{checksum5, rs129_decode, rs129_parity};

/// Full LC opcodes (FLCO) this decoder reads.
pub const FLCO_GROUP: u8 = 0;
pub const FLCO_PRIVATE: u8 = 3;
pub const FLCO_ALIAS_HEADER: u8 = 4;
pub const FLCO_ALIAS_BLOCK1: u8 = 5;
pub const FLCO_ALIAS_BLOCK3: u8 = 7;
pub const FLCO_GPS: u8 = 8;

/// The RS parity mask that says which burst a full LC is in.
pub const MASK_HEADER: [u8; 3] = [0x96, 0x96, 0x96];
pub const MASK_TERMINATOR: [u8; 3] = [0x99, 0x99, 0x99];

/// A link control's nine octets.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Lc(pub [u8; 9]);

impl Lc {
    /// A voice call: group (talkgroup) or private (unit to unit).
    pub fn voice(group: bool, src: u32, dst: u32, options: ServiceOptions) -> Lc {
        let mut b = [0u8; 9];
        b[0] = if group { FLCO_GROUP } else { FLCO_PRIVATE };
        b[2] = options.byte();
        b[3..6].copy_from_slice(&dst.to_be_bytes()[1..]);
        b[6..9].copy_from_slice(&src.to_be_bytes()[1..]);
        Lc(b)
    }
    /// Protect flag: the LC itself is protected (encrypted).
    pub fn protected(&self) -> bool {
        self.0[0] & 0x80 != 0
    }
    pub fn flco(&self) -> u8 {
        self.0[0] & 0x3F
    }
    pub fn fid(&self) -> u8 {
        self.0[1]
    }
    /// A voice call's LC: group or private.
    pub fn is_voice(&self) -> bool {
        !self.protected() && matches!(self.flco(), FLCO_GROUP | FLCO_PRIVATE)
    }
    pub fn options(&self) -> ServiceOptions {
        ServiceOptions::from(self.0[2])
    }
    pub fn dst(&self) -> u32 {
        u32::from_be_bytes([0, self.0[3], self.0[4], self.0[5]])
    }
    pub fn src(&self) -> u32 {
        u32::from_be_bytes([0, self.0[6], self.0[7], self.0[8]])
    }
}

/// A voice LC's service options octet.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ServiceOptions {
    pub emergency: bool,
    /// Privacy: the voice is encrypted. Reported, never decoded.
    pub privacy: bool,
    pub broadcast: bool,
    pub ovcm: bool,
    pub priority: u8,
}

impl ServiceOptions {
    pub fn from(b: u8) -> Self {
        ServiceOptions { emergency: b & 0x80 != 0, privacy: b & 0x40 != 0, broadcast: b & 0x08 != 0, ovcm: b & 0x04 != 0, priority: b & 3 }
    }
    pub fn byte(&self) -> u8 {
        (self.emergency as u8) << 7 | (self.privacy as u8) << 6 | (self.broadcast as u8) << 3 | (self.ovcm as u8) << 2 | (self.priority & 3)
    }
}

fn bits(bytes: &[u8]) -> Vec<u8> {
    bytes.iter().flat_map(|&b| (0..8).rev().map(move |i| (b >> i) & 1)).collect()
}

fn bytes(bits: &[u8]) -> Vec<u8> {
    bits.chunks(8).map(|c| c.iter().fold(0, |a, &b| (a << 1) | b)).collect()
}

/// A full LC as the 196 info bits of a header or terminator.
pub fn encode_full(lc: &Lc, mask: [u8; 3]) -> [u8; 196] {
    let p = rs129_parity(&lc.0);
    let mut w = [0u8; 12];
    w[..9].copy_from_slice(&lc.0);
    for i in 0..3 {
        w[9 + i] = p[i] ^ mask[i];
    }
    let d: [u8; 96] = bits(&w).try_into().expect("96 bits");
    encode196(&d)
}

/// The full LC from a header's or terminator's info bits, with the bits
/// and bytes corrected; None when RS cannot vouch for it.
pub fn decode_full(info: &[u8; 196], mask: [u8; 3]) -> Option<(Lc, u32)> {
    let (d, r) = decode196(info);
    let mut w: [u8; 12] = bytes(&d).try_into().expect("12 octets");
    for i in 0..3 {
        w[9 + i] ^= mask[i];
    }
    let fixed = rs129_decode(&mut w)?;
    Some((Lc(w[..9].try_into().expect("9")), r.corrected + fixed))
}

/// An LC as four 32-bit embedded fragments, with their LCSS (1, 3, 3, 2).
pub fn encode_embedded_lc(lc: &Lc) -> [([u8; 32], u8); 4] {
    let b: [u8; 72] = bits(&lc.0).try_into().expect("72 bits");
    let m = encode_embedded(&b, checksum5(&lc.0));
    let lcss = [1u8, 3, 3, 2];
    std::array::from_fn(|i| (m[32 * i..32 * i + 32].try_into().expect("32"), lcss[i]))
}

/// Puts an embedded LC back together from fragments as they arrive.
#[derive(Clone, Debug, Default)]
pub struct EmbeddedLc {
    raw: Vec<u8>,
}

impl EmbeddedLc {
    pub fn reset(&mut self) {
        self.raw.clear();
    }

    /// Add one burst's fragment and its LCSS. When a last fragment completes
    /// four in order, the LC if its code and checksum agree.
    pub fn push(&mut self, frag: &[u8; 32], lcss: u8) -> Option<(Lc, u32)> {
        match lcss {
            1 => {
                self.raw.clear();
                self.raw.extend_from_slice(frag);
            }
            3 if matches!(self.raw.len(), 32 | 64) => self.raw.extend_from_slice(frag),
            2 if self.raw.len() == 96 => {
                self.raw.extend_from_slice(frag);
                let all: [u8; 128] = std::mem::take(&mut self.raw).try_into().expect("128");
                let (lcb, sum, fixed) = decode_embedded(&all)?;
                let lc = Lc(bytes(&lcb).try_into().expect("9"));
                return (checksum5(&lc.0) == sum).then_some((lc, fixed));
            }
            _ => self.raw.clear(),
        }
        None
    }
}

/// Talker alias encodings (the alias header's data format).
const TA_7BIT: u8 = 0;
const TA_ISO8: u8 = 1;
const TA_UTF8: u8 = 2;
const TA_UTF16: u8 = 3;

/// Talker alias, reassembled. The header says how long the alias is and
/// how it is coded, and holds its first 49 bits; blocks 1-3 hold 56 each.
/// A radio may send only the blocks it needs; the alias is known as soon
/// as enough have arrived.
#[derive(Clone, Debug, Default)]
pub struct TalkerAlias {
    format: u8,
    len: usize,
    parts: [Option<Vec<u8>>; 4],
}

impl TalkerAlias {
    pub fn reset(&mut self) {
        *self = TalkerAlias::default();
    }

    /// Take an alias LC (FLCO 4-7); the alias when it is complete.
    pub fn push(&mut self, lc: &Lc) -> Option<String> {
        let f = lc.flco();
        if !(FLCO_ALIAS_HEADER..=FLCO_ALIAS_BLOCK3).contains(&f) {
            return None;
        }
        let all = bits(&lc.0[2..]);
        if f == FLCO_ALIAS_HEADER {
            let (format, len) = (lc.0[2] >> 6, ((lc.0[2] >> 1) & 0x1F) as usize);
            // A different header is a different alias; blocks heard before
            // any header are kept for the one that arrives.
            if self.parts[0].is_some() && (format, len) != (self.format, self.len) {
                self.parts = Default::default();
            }
            self.format = format;
            self.len = len;
            // 7-bit text starts in the header octet's last bit, so the
            // header holds 49 bits; 8- and 16-bit text starts at the next
            // octet, and that bit is unused. Read the 7-bit way, GB7IR's
            // aliases (28/10/2026) came out every character shifted one bit
            // ("&" for "M").
            self.parts[0] = Some(all[if format == TA_7BIT { 7 } else { 8 }..].to_vec());
        } else {
            self.parts[(f - FLCO_ALIAS_HEADER) as usize] = Some(all);
        }
        self.text()
    }

    /// The bits a full alias of this length and format needs.
    fn needed(&self) -> usize {
        match self.format {
            TA_7BIT => 7 * self.len,
            TA_UTF16 => 16 * self.len,
            _ => 8 * self.len,
        }
    }

    fn text(&self) -> Option<String> {
        self.parts[0].as_ref()?;
        let mut b = Vec::new();
        for p in &self.parts {
            if b.len() >= self.needed() {
                break;
            }
            b.extend_from_slice(p.as_ref()?);
        }
        if b.len() < self.needed() || self.len == 0 {
            return None;
        }
        b.truncate(self.needed());
        let s = match self.format {
            TA_7BIT => b.chunks(7).map(|c| c.iter().fold(0u8, |a, &x| (a << 1) | x) as char).collect(),
            TA_ISO8 => bytes(&b).into_iter().map(|c| c as char).collect(),
            TA_UTF8 => String::from_utf8_lossy(&bytes(&b)).into_owned(),
            _ => {
                let u: Vec<u16> = bytes(&b).chunks(2).map(|p| u16::from_be_bytes([p[0], p[1]])).collect();
                String::from_utf16_lossy(&u)
            }
        };
        Some(s.trim_end_matches(['\0', ' ']).to_string())
    }
}

/// The alias LCs a radio sends for `alias`: 7-bit if it is all ASCII, else
/// UTF-8 (the test transmitter's choice; real radios differ).
pub fn alias_lcs(alias: &str) -> Vec<Lc> {
    let ascii = alias.is_ascii();
    let (format, len, mut b) = if ascii {
        let b: Vec<u8> = alias.bytes().flat_map(|c| (0..7).rev().map(move |i| (c >> i) & 1)).collect();
        (TA_7BIT, alias.len(), b)
    } else {
        (TA_UTF8, alias.len(), bits(alias.as_bytes()))
    };
    // 7-bit text starts in the header octet's last bit; 8- and 16-bit text
    // at the next octet (TalkerAlias::push).
    let head = if ascii { 49 } else { 48 };
    let total = head + 3 * 56;
    assert!(b.len() <= total && len < 32, "an alias holds at most 217 bits");
    let blocks = if b.len() <= head { 0 } else { (b.len() - head).div_ceil(56) };
    b.resize(head + 56 * blocks, 0);
    let mut out = Vec::new();
    let mut h = [0u8; 9];
    h[0] = FLCO_ALIAS_HEADER;
    if ascii {
        h[2] = (format << 6) | ((len as u8) << 1) | b[0];
        h[3..].copy_from_slice(&bytes(&b[1..49]));
    } else {
        h[2] = (format << 6) | ((len as u8) << 1);
        h[3..].copy_from_slice(&bytes(&b[..48]));
    }
    out.push(Lc(h));
    for k in 0..blocks {
        let mut l = [0u8; 9];
        l[0] = FLCO_ALIAS_BLOCK1 + k as u8;
        l[2..].copy_from_slice(&bytes(&b[head + 56 * k..head + 56 * (k + 1)]));
        out.push(Lc(l));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_full_lc_round_trips_through_bptc_and_rs_with_its_mask() {
        let lc = Lc::voice(true, 2_351_234, 2350, ServiceOptions::default());
        let info = encode_full(&lc, MASK_HEADER);
        assert_eq!(decode_full(&info, MASK_HEADER), Some((lc, 0)));
        // The terminator's mask is not the header's: an LC read with the
        // wrong one fails RS, which is how a header is told from a terminator.
        assert_eq!(decode_full(&info, MASK_TERMINATOR), None);
        assert_eq!((lc.src(), lc.dst(), lc.flco()), (2_351_234, 2350, FLCO_GROUP));
    }

    #[test]
    fn embedded_fragments_rebuild_the_lc_only_in_order() {
        let lc = Lc::voice(false, 1234567, 7654321, ServiceOptions { privacy: true, ..Default::default() });
        let frags = encode_embedded_lc(&lc);
        let mut e = EmbeddedLc::default();
        let mut got = None;
        for (f, lcss) in &frags {
            got = e.push(f, *lcss);
        }
        assert_eq!(got, Some((lc, 0)));
        assert!(got.unwrap().0.options().privacy);
        // A missing fragment loses that LC, not the next.
        let mut e = EmbeddedLc::default();
        for (f, lcss) in frags.iter().skip(1) {
            assert_eq!(e.push(f, *lcss), None);
        }
    }

    #[test]
    fn talker_alias_reassembles_in_any_order_and_any_coding() {
        for alias in ["GM4BRI", "GM4BRI Brian", "GM4BRI Brian Ayrshire", "Ünïcödé 名前"] {
            let lcs = alias_lcs(alias);
            let mut ta = TalkerAlias::default();
            let mut got = None;
            for lc in lcs.iter().rev() {
                got = ta.push(lc).or(got);
            }
            assert_eq!(got.as_deref(), Some(alias), "{alias}");
        }
        // The header alone is not enough for a long alias.
        let mut ta = TalkerAlias::default();
        assert_eq!(ta.push(&alias_lcs("GM4BRI Brian Ayrshire")[0]), None);
    }
}
