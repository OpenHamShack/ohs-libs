//! AIS's link layer (ITU-R M.1371-5 Annex 2 §3.2.2): HDLC framing with bit
//! stuffing, the HDLC frame check sequence, NRZI, and the deframer the
//! receiver runs on each slicer's bits.
//!
//! **Bit order.** The message definitions number their bits from the most
//! significant end (a 6-bit message ID first, MSB first), but HDLC sends
//! every octet least significant bit first. So the payload is cut into
//! octets as the tables read, and each octet goes on air reversed; the FCS
//! is the usual HDLC/X.25 CRC (reflected 0x1021, preset 0xFFFF, inverted),
//! sent low octet first, which the receiver checks by the CRC over data
//! and FCS together leaving 0xF0B8. Getting this wrong shows at once
//! against any real sentence: the type-1 test in tests/ais.rs holds the
//! bits to a published one.

/// The HDLC flag, 01111110, as sent (it reads the same either way round).
pub const FLAG: u8 = 0x7E;
/// The CRC over a frame and its own FCS, when the frame is good.
pub const GOOD_RESIDUE: u16 = 0xF0B8;
/// The longest frame AIS sends: five slots of 256 bits.
pub const MAX_FRAME_BITS: usize = 5 * 256;

/// The HDLC frame check sequence's running CRC, over octets as sent (LSB
/// first): reflected polynomial 0x8408, preset 0xFFFF; the FCS is its
/// complement.
pub fn crc16(bytes: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    for &b in bytes {
        crc ^= b as u16;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0x8408 } else { crc >> 1 };
        }
    }
    crc
}

/// The FCS to append to `bytes` (the complement of the CRC, low octet first).
pub fn fcs(bytes: &[u8]) -> [u8; 2] {
    let c = !crc16(bytes);
    [c as u8, (c >> 8) as u8]
}

/// A message's bits (MSB-first, as the tables number them) as the octets
/// HDLC carries, each reversed so its first bit goes on air first. A
/// message whose length is not a whole octet is padded with zeros at the
/// end, as the standard says (§3.3.7: "fill bits").
pub fn pack(bits: &[u8]) -> Vec<u8> {
    bits.chunks(8)
        .map(|c| {
            let mut v = 0u8;
            for (i, &b) in c.iter().enumerate() {
                // Bit i of the table goes on air i-th: bit i of the octet.
                v |= (b & 1) << i;
            }
            v
        })
        .collect()
}

/// The reverse of `pack`: octets as HDLC carried them to the message's bits.
pub fn unpack(bytes: &[u8]) -> Vec<u8> {
    bytes.iter().flat_map(|&v| (0..8).map(move |i| (v >> i) & 1)).collect()
}

/// The on-air bits of `octets` (LSB first), with a 0 stuffed after every
/// five 1s so no flag can appear inside a frame.
pub fn stuff(octets: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(octets.len() * 9);
    let mut ones = 0;
    for &v in octets {
        for i in 0..8 {
            let b = (v >> i) & 1;
            out.push(b);
            if b == 1 {
                ones += 1;
                if ones == 5 {
                    out.push(0);
                    ones = 0;
                }
            } else {
                ones = 0;
            }
        }
    }
    out
}

/// NRZI as AIS uses it: a 0 is a change of level, a 1 none. `level` is the
/// level before the first bit; the output is one level (0 or 1) per bit.
pub fn nrzi_encode(bits: &[u8], mut level: u8) -> Vec<u8> {
    bits.iter()
        .map(|&b| {
            if b == 0 {
                level ^= 1;
            }
            level
        })
        .collect()
}

/// What the deframer made of a frame between two flags.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Framed {
    /// The octets as carried (FCS removed), the CRC good.
    Good(Vec<u8>),
    /// A frame opened after a training sequence that did not end good: its
    /// CRC failed, it was not whole octets, or it aborted (seven 1s).
    /// Counted, never shown.
    Damaged,
}

/// The receiver's HDLC deframer for one slicer: fed one NRZI level a bit,
/// it undoes NRZI, finds flags, removes stuffed bits, and hands back each
/// frame of whole octets with its CRC checked.
///
/// A frame counts only after a **training sequence**: the opening flag must
/// follow at least `TRAINING_BITS` of the 0101... pattern every AIS burst
/// sends first (M.1371 §3.2.2.6). Noise makes a flag every 256 bits or so
/// on each slicer, and the CRC alone lets one random frame in 65,536
/// through: across ten slicers that was a false message every few minutes
/// of an empty channel. Six bits of training cost a real burst nothing (it
/// sends 24) and noise another 1 in 64.
#[derive(Clone, Debug)]
pub struct Deframer {
    level: u8,
    /// The last decoded bits, newest in bit 0.
    recent: u32,
    ones: u32,
    frame: Option<Vec<u8>>,
}

/// How many bits of 0101... must come just before an opening flag.
pub const TRAINING_BITS: u32 = 6;

impl Default for Deframer {
    fn default() -> Self {
        Self::new()
    }
}

impl Deframer {
    pub fn new() -> Self {
        Deframer { level: 0, recent: 0, ones: 0, frame: None }
    }

    pub fn reset(&mut self) {
        *self = Deframer::new();
    }

    /// Whether a frame is open (between an opening flag and the next).
    pub fn in_frame(&self) -> bool {
        self.frame.is_some()
    }

    /// Feed one NRZI level. Returns what a closing flag (or an abort) ended, and whether
    /// this bit completed a flag at all (`true`: the caller freezes its
    /// slicing threshold from the training just heard).
    pub fn push(&mut self, level: u8) -> (Option<Framed>, bool) {
        let bit = if level == self.level { 1 } else { 0 };
        self.level = level;
        // The training check looks at the bits before the flag, so it reads
        // `recent` before this bit joins it.
        let before = self.recent;
        self.recent = (self.recent << 1) | bit as u32;
        if bit == 1 {
            self.ones += 1;
            if self.ones > 6 {
                // Seven 1s: an abort, or noise.
                if self.frame.take().is_some() {
                    return (Some(Framed::Damaged), false);
                }
            } else if let Some(f) = self.frame.as_mut() {
                f.push(1);
                if f.len() > MAX_FRAME_BITS + 16 + 7 {
                    self.frame = None;
                    return (Some(Framed::Damaged), false);
                }
            }
            return (None, false);
        }
        let ones = std::mem::replace(&mut self.ones, 0);
        if ones == 5 {
            // A stuffed 0: dropped.
            return (None, false);
        }
        if ones == 6 {
            // A flag. Its leading 0 and six 1s went into the frame; take them out.
            let done = self.frame.take().and_then(|mut f| {
                f.truncate(f.len().saturating_sub(7));
                close(&f)
            });
            // `before` holds the flag's 0 and six 1s in its low seven bits;
            // the training is what came before them.
            let training = before >> 7;
            let want = (1u32 << TRAINING_BITS) - 1;
            // ...0101 with the newest bit a 1: 0b010101 for six bits.
            let pattern = 0x5555_5555u32 & want;
            self.frame = if training & want == pattern { Some(Vec::with_capacity(512)) } else { None };
            return (done, true);
        }
        if let Some(f) = self.frame.as_mut() {
            f.push(0);
        }
        (None, false)
    }
}

/// A closed frame's bits: whole octets (LSB first) with a good CRC, or a
/// damaged one.
fn close(bits: &[u8]) -> Option<Framed> {
    // A frame opened after a training sequence that is not whole octets
    // lost or gained a bit (a damaged bit next to a stuffed one): as damaged
    // as a bad CRC, and counted with them.
    if bits.len() % 8 != 0 || bits.len() < 40 + 16 || bits.len() > MAX_FRAME_BITS + 16 {
        return Some(Framed::Damaged);
    }
    let bytes: Vec<u8> =
        bits.chunks(8).map(|c| c.iter().enumerate().fold(0u8, |v, (i, &b)| v | (b << i))).collect();
    if crc16(&bytes) != GOOD_RESIDUE {
        return Some(Framed::Damaged);
    }
    Some(Framed::Good(bytes[..bytes.len() - 2].to_vec()))
}

/// The whole burst's bits as the transmitter keys them, before NRZI:
/// `ramp` bits of power ramp (sent as 0s), 24 bits of training, the flag,
/// the stuffed octets and their FCS, the flag, and `tail` bits (the
/// buffer, sent as 0s).
pub fn burst_bits(octets: &[u8], ramp: usize, tail: usize) -> Vec<u8> {
    let mut b = vec![0u8; ramp];
    // 010101..., starting with a 0 (§3.2.2.6).
    b.extend((0..24).map(|i| (i & 1) as u8));
    let flag: Vec<u8> = (0..8).map(|i| (FLAG >> i) & 1).collect();
    b.extend_from_slice(&flag);
    let mut body = octets.to_vec();
    body.extend_from_slice(&fcs(octets));
    b.extend(stuff(&body));
    b.extend_from_slice(&flag);
    b.extend(std::iter::repeat_n(0u8, tail));
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fcs_is_hdlcs() {
        // The X.25 check: "123456789" gives FCS 0x906E (CRC-16/IBM-SDLC).
        let c = !crc16(b"123456789");
        assert_eq!(c, 0x906E);
        let mut m = b"123456789".to_vec();
        m.extend_from_slice(&fcs(b"123456789"));
        assert_eq!(crc16(&m), GOOD_RESIDUE);
    }

    #[test]
    fn stuffing_puts_a_zero_after_five_ones_and_no_flag_survives() {
        let s = stuff(&[0xFF, 0x7E, 0xFF]);
        for w in s.windows(6) {
            assert_ne!(w, [1, 1, 1, 1, 1, 1]);
        }
        assert_eq!(&s[..6], &[1, 1, 1, 1, 1, 0]);
    }

    fn frame_through(octets: &[u8]) -> Vec<Framed> {
        let levels = nrzi_encode(&burst_bits(octets, 8, 24), 0);
        let mut d = Deframer::new();
        levels.into_iter().filter_map(|l| d.push(l).0).collect()
    }

    #[test]
    fn a_burst_comes_back_whole_whatever_its_octets() {
        for octets in [vec![0xFFu8; 21], vec![0x00; 21], vec![0x7E; 21], (0..53).map(|i| (i * 37) as u8).collect()] {
            assert_eq!(frame_through(&octets), vec![Framed::Good(octets.clone())]);
        }
    }

    #[test]
    fn a_flag_without_training_opens_nothing() {
        let mut bits: Vec<u8> = vec![1, 1, 0, 0, 1, 1, 0, 0];
        let flag: Vec<u8> = (0..8).map(|i| (FLAG >> i) & 1).collect();
        bits.extend_from_slice(&flag);
        let mut body = vec![0x11u8; 21];
        body.extend_from_slice(&fcs(&body.clone()));
        bits.extend(stuff(&body));
        bits.extend_from_slice(&flag);
        let mut d = Deframer::new();
        let got: Vec<_> = nrzi_encode(&bits, 0).into_iter().filter_map(|l| d.push(l).0).collect();
        assert!(got.is_empty(), "{got:?}");
    }

    #[test]
    fn a_damaged_bit_fails_the_crc() {
        let octets = vec![0x5Au8; 21];
        let mut bits = burst_bits(&octets, 8, 24);
        bits[8 + 24 + 8 + 50] ^= 1;
        let mut d = Deframer::new();
        let got: Vec<_> = nrzi_encode(&bits, 0).into_iter().filter_map(|l| d.push(l).0).collect();
        assert!(!got.iter().any(|f| matches!(f, Framed::Good(_))), "{got:?}");
    }

    #[test]
    fn pack_and_unpack_are_each_others_reverse() {
        let bits: Vec<u8> = (0..168).map(|i| ((i * 7 + 3) % 5 == 0) as u8).collect();
        assert_eq!(unpack(&pack(&bits)), bits);
        // The table's first bit is the first sent: bit 0 of the first octet.
        assert_eq!(pack(&[1, 0, 0, 0, 0, 0, 0, 0]), vec![1]);
    }
}
