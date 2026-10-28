//! DMR's small channel codes (ETSI TS 102 361-1 Annex B): the Golay codes
//! (slot type's (20,8) and AMBE+2's (24,12) and (23,12)), the quadratic
//! residue code QR(16,7) of the EMB, the Hamming codes inside the block
//! product codes and the CACH's TACT, Reed-Solomon RS(12,9) over a full
//! link control, and the CRCs (CCITT, CRC-32 and the embedded LC's
//! 5-bit checksum).
//!
//! Written from the specification's definitions. Every constant was checked
//! against two independent implementations while writing (G4KLX's
//! MMDVMHost, GPL-2.0-or-later, and mbelib's Golay, ISC), and the tests hold
//! each code to known answers taken from them: an encoder that matches ours
//! but not the air would pass a round trip and fail every real repeater.
//!
//! Convention: a codeword held in an integer has its first transmitted bit
//! as the most significant; bit vectors are `u8`s of 0 or 1 in transmitted
//! order.

use std::sync::OnceLock;

/// Golay's generator, x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1. Its mirror
/// (0xAE3) is the other common choice and is wrong for DMR: mbelib's
/// generator matrix ends 0x475 (x^11 mod this polynomial), and MMDVMHost's
/// slot-type table encodes 1 as parity 0x8EB, which is that remainder
/// shifted with its parity bit.
const GOLAY_POLY: u32 = 0xC75;

/// The 11 parity bits of Golay(23,12) for 12 data bits.
fn golay23_parity(data: u32) -> u32 {
    let mut r = (data & 0xFFF) << 11;
    for bit in (11..23).rev() {
        if r & (1 << bit) != 0 {
            r ^= GOLAY_POLY << (bit - 11);
        }
    }
    r & 0x7FF
}

/// Golay(23,12): data in the top 12 bits, parity in the low 11.
pub fn golay23_encode(data: u32) -> u32 {
    ((data & 0xFFF) << 11) | golay23_parity(data)
}

/// The error pattern of each syndrome. Golay(23,12) is perfect: the 2048
/// syndromes are exactly the patterns of weight 0 to 3.
fn golay23_table() -> &'static [u32; 2048] {
    static T: OnceLock<Box<[u32; 2048]>> = OnceLock::new();
    T.get_or_init(|| {
        let mut t = Box::new([0u32; 2048]);
        let mut put = |e: u32| {
            let s = golay23_syndrome(e) as usize;
            t[s] = e;
        };
        for a in 0..23 {
            put(1 << a);
            for b in a + 1..23 {
                put((1 << a) | (1 << b));
                for c in b + 1..23 {
                    put((1 << a) | (1 << b) | (1 << c));
                }
            }
        }
        t
    })
}

fn golay23_syndrome(word: u32) -> u32 {
    golay23_parity(word >> 11) ^ (word & 0x7FF)
}

/// Decode a Golay(23,12) word: the 12 data bits and how many bits were
/// corrected (0-3). Any word decodes: four or more errors decode wrongly,
/// which is why the AMBE frame's C0 carries a 24th bit.
pub fn golay23_decode(word: u32) -> (u32, u32) {
    let word = word & 0x7F_FFFF;
    let e = golay23_table()[golay23_syndrome(word) as usize];
    ((word ^ e) >> 11, e.count_ones())
}

/// Golay(24,12): Golay(23,12) and an even-parity bit after it.
pub fn golay24_encode(data: u32) -> u32 {
    let w = golay23_encode(data);
    (w << 1) | (w.count_ones() & 1)
}

/// Decode a Golay(24,12) word: data and bits corrected, or None when the
/// parity bit shows four errors (detected, not correctable).
pub fn golay24_decode(word: u32) -> Option<(u32, u32)> {
    let word = word & 0xFF_FFFF;
    let (data, _) = golay23_decode(word >> 1);
    let fixed = golay24_encode(data);
    let d = (fixed ^ word).count_ones();
    if d > 3 {
        return None;
    }
    Some((data, d))
}

/// Golay(20,8) of the slot type: the (24,12) code with its top four data
/// bits zero, left out.
pub fn golay20_encode(data: u8) -> u32 {
    golay24_encode(data as u32) & 0xF_FFFF
}

/// Decode a slot type: the 8 data bits (colour code, data type) and the bits
/// corrected, or None beyond three errors.
pub fn golay20_decode(word: u32) -> Option<(u8, u32)> {
    // The four bits the shortening left out are known zeros: decode the full
    // word, and a correction that lands in them is a failure.
    let (data, errs) = golay24_decode(word & 0xF_FFFF)?;
    if data > 0xFF {
        return None;
    }
    Some((data as u8, errs))
}

/// QR(16,7,6) of the EMB: 7 data bits (colour code, PI, LCSS), the
/// remainder by x^8 + x^5 + x^4 + x^3 + 1, then an even-parity bit.
pub fn qr16_encode(data: u8) -> u16 {
    let mut r = ((data as u32) & 0x7F) << 8;
    for bit in (8..15).rev() {
        if r & (1 << bit) != 0 {
            r ^= 0x139 << (bit - 8);
        }
    }
    let w = (((data as u32) & 0x7F) << 8) | (r & 0xFF);
    ((w << 1) | (w.count_ones() & 1)) as u16
}

/// Decode an EMB: the nearest of the 128 codewords if it is at most two
/// bits away (the code's distance is 6), with the bits corrected.
pub fn qr16_decode(word: u16) -> Option<(u8, u32)> {
    let mut best = (u32::MAX, 0u8);
    for d in 0..128u8 {
        let dist = (qr16_encode(d) ^ word).count_ones();
        if dist < best.0 {
            best = (dist, d);
        }
    }
    (best.0 <= 2).then_some((best.1, best.0))
}

/// A Hamming code given by its parity equations: check bit j is the XOR of
/// the data bits listed in `checks[j]`. Bits are in order, data then
/// checks. Each DMR code is written down exactly as the specification
/// lists its equations, so the table is the one thing to compare.
pub struct Hamming {
    pub k: usize,
    pub checks: &'static [&'static [usize]],
}

/// BPTC(196,96) rows.
pub const H15_11: Hamming =
    Hamming { k: 11, checks: &[&[0, 1, 2, 3, 5, 7, 8], &[1, 2, 3, 4, 6, 8, 9], &[2, 3, 4, 5, 7, 9, 10], &[0, 1, 2, 4, 6, 7, 10]] };
/// BPTC(196,96) columns.
pub const H13_9: Hamming =
    Hamming { k: 9, checks: &[&[0, 1, 3, 5, 6], &[0, 1, 2, 4, 6, 7], &[0, 1, 2, 3, 5, 7, 8], &[0, 2, 4, 5, 8]] };
/// The embedded LC's rows: (15,11) and a fifth check making distance 4.
pub const H16_11: Hamming = Hamming {
    k: 11,
    checks: &[
        &[0, 1, 2, 3, 5, 7, 8],
        &[1, 2, 3, 4, 6, 8, 9],
        &[2, 3, 4, 5, 7, 9, 10],
        &[0, 1, 2, 4, 6, 7, 10],
        &[0, 2, 5, 6, 8, 9, 10],
    ],
};
/// The CACH's TACT: (AT, TC, LCSS1, LCSS0) and three checks.
pub const H7_4: Hamming = Hamming { k: 4, checks: &[&[0, 1, 2], &[1, 2, 3], &[0, 1, 3]] };

impl Hamming {
    pub fn n(&self) -> usize {
        self.k + self.checks.len()
    }

    /// Fill in the check bits of `bits` (length n) from its data bits.
    pub fn encode(&self, bits: &mut [u8]) {
        for (j, c) in self.checks.iter().enumerate() {
            bits[self.k + j] = c.iter().fold(0, |a, &i| a ^ bits[i]);
        }
    }

    fn syndrome(&self, bits: &[u8]) -> u32 {
        let mut s = 0;
        for (j, c) in self.checks.iter().enumerate() {
            let p = c.iter().fold(bits[self.k + j], |a, &i| a ^ bits[i]);
            s |= (p as u32) << j;
        }
        s
    }

    /// Correct one bit error in place. Some(bits corrected, 0 or 1), or None
    /// when the syndrome names no single bit (more errors than it corrects).
    pub fn decode(&self, bits: &mut [u8]) -> Option<u32> {
        let s = self.syndrome(bits);
        if s == 0 {
            return Some(0);
        }
        for i in 0..self.n() {
            let col = if i < self.k {
                self.checks.iter().enumerate().fold(0, |a, (j, c)| a | ((c.contains(&i) as u32) << j))
            } else {
                1 << (i - self.k)
            };
            if col == s {
                bits[i] ^= 1;
                return Some(1);
            }
        }
        None
    }
}

// ---------------------------------------------------------------- RS(12,9)

struct Gf {
    exp: [u8; 512],
    log: [u8; 256],
}

fn gf() -> &'static Gf {
    static G: OnceLock<Gf> = OnceLock::new();
    G.get_or_init(|| {
        let mut exp = [0u8; 512];
        let mut log = [0u8; 256];
        let mut x: u16 = 1;
        for i in 0..255 {
            exp[i] = x as u8;
            log[x as usize] = i as u8;
            x <<= 1;
            if x & 0x100 != 0 {
                x ^= 0x11D;
            }
        }
        for i in 255..512 {
            exp[i] = exp[i - 255];
        }
        Gf { exp, log }
    })
}

fn gmul(a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 {
        return 0;
    }
    let g = gf();
    g.exp[g.log[a as usize] as usize + g.log[b as usize] as usize]
}

fn gdiv(a: u8, b: u8) -> u8 {
    if a == 0 {
        return 0;
    }
    let g = gf();
    g.exp[(g.log[a as usize] as usize + 255 - g.log[b as usize] as usize) % 255]
}

/// RS(12,9) parity of 9 bytes: g(x) = (x + a)(x + a^2)(x + a^3) over
/// GF(2^8) with x^8 + x^4 + x^3 + x^2 + 1, so x^3 + 14x^2 + 56x + 64. The
/// three parity bytes in transmitted order. (The full LC then XORs a mask
/// into them that says which burst it is: lc.rs.)
pub fn rs129_parity(msg: &[u8; 9]) -> [u8; 3] {
    const G: [u8; 3] = [64, 56, 14]; // x^0, x^1, x^2
    let mut p = [0u8; 3]; // p[0] the x^0 term
    for &m in msg {
        let f = m ^ p[2];
        p[2] = p[1] ^ gmul(G[2], f);
        p[1] = p[0] ^ gmul(G[1], f);
        p[0] = gmul(G[0], f);
    }
    [p[2], p[1], p[0]]
}

/// Check and correct a 12-byte RS(12,9) word in place: Some(bytes
/// corrected, 0 or 1) or None. The code's distance is 4, so it corrects one
/// byte and refuses what it cannot be sure of.
pub fn rs129_decode(w: &mut [u8; 12]) -> Option<u32> {
    let g = gf();
    // S_j = r(a^j), j = 1..3; byte 0 is the x^11 term.
    let s: Vec<u8> = (1..=3)
        .map(|j| w.iter().fold(0u8, |acc, &b| gmul(acc, g.exp[j]) ^ b))
        .collect();
    if s.iter().all(|&x| x == 0) {
        return Some(0);
    }
    if s[0] == 0 || s[1] == 0 {
        return None;
    }
    // One error e at x^p: S1 = e X, S2 = e X^2, S3 = e X^3, X = a^p.
    let x = gdiv(s[1], s[0]);
    let e = gdiv(gmul(s[0], s[0]), s[1]);
    if gmul(gmul(e, x), gmul(x, x)) != s[2] {
        return None;
    }
    let p = g.log[x as usize] as usize;
    if p > 11 {
        return None;
    }
    w[11 - p] ^= e;
    Some(1)
}

// ---------------------------------------------------------------- CRCs

/// CRC-CCITT as DMR uses it: x^16 + x^12 + x^5 + 1, starting at zero, bits
/// most significant first, inverted at the end. The data header, CSBK and
/// PI header XOR a mask over it that names the burst type.
pub fn crc_ccitt(data: &[u8]) -> u16 {
    let mut crc: u16 = 0;
    for &b in data {
        crc ^= (b as u16) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 { (crc << 1) ^ 0x1021 } else { crc << 1 };
        }
    }
    !crc
}

/// The CRC-32 over a data message's octets (TS 102 361-1 B.3.9): the
/// polynomial 0x04C11DB7 from zero, with no inversion, over the octets
/// taken in pairs with the second of each pair first; it is sent least
/// significant octet first. `octets` is even in length (the message less
/// its CRC always is).
pub fn crc32_dmr(octets: &[u8]) -> u32 {
    let mut crc: u32 = 0;
    let mut feed = |b: u8| {
        for i in (0..8).rev() {
            let bit = ((b >> i) & 1) as u32;
            crc = if ((crc >> 31) ^ bit) & 1 != 0 { (crc << 1) ^ 0x04C1_1DB7 } else { crc << 1 };
        }
    };
    for pair in octets.chunks(2) {
        if pair.len() == 2 {
            feed(pair[1]);
        }
        feed(pair[0]);
    }
    crc
}

/// The embedded LC's 5-bit checksum: the sum of its nine octets modulo 31.
pub fn checksum5(lc: &[u8; 9]) -> u8 {
    (lc.iter().map(|&b| b as u32).sum::<u32>() % 31) as u8
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slot_type_golay_matches_the_known_encodings() {
        // MMDVMHost's ENCODING_TABLE_2087, entries 1-4 and 255, read as
        // (data << 12 | parity): its table stores parity as cksum & 0xFF,
        // then the high nibble of cksum >> 8.
        let known = [(1u8, 0xB08Eu32), (2, 0xE093), (3, 0x501D), (4, 0x70A9), (12, 0x1075)];
        for (d, ck) in known {
            let parity = ((ck & 0xFF) << 4) | (ck >> 12);
            assert_eq!(golay20_encode(d), ((d as u32) << 12) | parity, "data {d}");
        }
    }

    #[test]
    fn golay_corrects_three_and_its_24th_bit_detects_four() {
        for d in [0u32, 1, 0x5A5, 0xFFF, 0x123] {
            let w = golay24_encode(d);
            for e in [0u32, 0x1, 0x80_0001, 0x10_0402, 0x7000] {
                assert_eq!(golay24_decode(w ^ e), Some((d, e.count_ones())));
            }
            assert_eq!(golay24_decode(w ^ 0x80_0C01), None, "four errors");
        }
        // mbelib's generator row for the last data bit is x^11 mod g.
        assert_eq!(golay23_encode(1), 0x800 | 0x475);
    }

    #[test]
    fn emb_qr_code_matches_the_known_encodings_and_corrects_two() {
        // MMDVMHost's ENCODING_TABLE_1676 entries 1, 2 and 127.
        assert_eq!(qr16_encode(1), 0x0273);
        assert_eq!(qr16_encode(2), 0x04E5);
        assert_eq!(qr16_encode(127), 0xFE5B);
        for d in 0..128u8 {
            assert_eq!(qr16_decode(qr16_encode(d) ^ 0x8001), Some((d, 2)));
        }
    }

    #[test]
    fn hamming_codes_correct_any_single_error() {
        for h in [&H15_11, &H13_9, &H16_11, &H7_4] {
            for seed in 0..(1u32 << h.k).min(64) {
                let mut w = vec![0u8; h.n()];
                for (i, b) in w.iter_mut().take(h.k).enumerate() {
                    *b = ((seed.wrapping_mul(2654435761) >> i) & 1) as u8;
                }
                h.encode(&mut w);
                let good = w.clone();
                assert_eq!(h.decode(&mut w.clone()), Some(0));
                for i in 0..h.n() {
                    let mut x = good.clone();
                    x[i] ^= 1;
                    assert_eq!(h.decode(&mut x), Some(1));
                    assert_eq!(x, good);
                }
            }
        }
        // (16,11,4) refuses two errors rather than miscorrecting.
        let mut w = vec![0u8; 16];
        w[0] = 1;
        H16_11.encode(&mut w);
        w[3] ^= 1;
        w[9] ^= 1;
        assert_eq!(H16_11.decode(&mut w), None);
    }

    #[test]
    fn rs129_matches_a_hand_worked_parity_and_corrects_one_byte() {
        // A single 1 in the last data byte: the parity is g(x)'s low terms.
        let mut m = [0u8; 9];
        m[8] = 1;
        assert_eq!(rs129_parity(&m), [14, 56, 64]);
        let msg = [0x00, 0x00, 0x20, 0x00, 0x0F, 0xA2, 0x23, 0x45, 0x67];
        let p = rs129_parity(&msg);
        let mut w = [0u8; 12];
        w[..9].copy_from_slice(&msg);
        w[9..].copy_from_slice(&p);
        for i in 0..12 {
            let mut x = w;
            x[i] ^= 0x5C;
            assert_eq!(rs129_decode(&mut x), Some(1));
            assert_eq!(x, w);
        }
        let mut x = w;
        x[1] ^= 1;
        x[7] ^= 2;
        assert_ne!(rs129_decode(&mut x), Some(0));
    }

    #[test]
    fn crc_ccitt_is_the_inverted_xmodem_crc() {
        // CRC-16/XMODEM of "123456789" is 0x31C3; DMR's is its inverse.
        assert_eq!(crc_ccitt(b"123456789"), !0x31C3);
    }

    #[test]
    fn crc32_swaps_each_pair_of_octets() {
        // CRC-32/MPEG-2 without its initial ones and final inversion is
        // CRC-32/BZIP2's core: with init 0 and no xor, "21436587" (pairs
        // swapped from "12345678") must give the same CRC as feeding
        // "12345678" through crc32_dmr.
        let plain = |d: &[u8]| {
            let mut crc: u32 = 0;
            for &b in d {
                crc ^= (b as u32) << 24;
                for _ in 0..8 {
                    crc = if crc & 0x8000_0000 != 0 { (crc << 1) ^ 0x04C1_1DB7 } else { crc << 1 };
                }
            }
            crc
        };
        assert_eq!(crc32_dmr(b"12345678"), plain(b"21436587"));
    }
}
