//! DAB's channel coding, both ways: the energy-dispersal PRBS (§10), the
//! CRCs (FIB and DAB+ AU: §5.2.1 and TS 102 563 §5.2; DAB+'s Fire code),
//! and the convolutional code (§11.1): the encoder (for the transmitter
//! model in tests) and a soft-decision Viterbi decoder with puncturing.
//! Written from EN 300 401; nothing here is ported.

use super::tables::{self, Profile};

/// §10.1: the PRBS of x^9 + x^5 + 1 from all ones, `n` bits.
pub fn prbs(n: usize) -> Vec<u8> {
    let mut r: u16 = 0x1ff;
    (0..n)
        .map(|_| {
            let b = ((r >> 8) ^ (r >> 4)) as u8 & 1;
            r = ((r << 1) | b as u16) & 0x1ff;
            b
        })
        .collect()
}

/// The PRBS packed into bytes, first bit in the high end.
pub fn prbs_bytes(n: usize) -> Vec<u8> {
    prbs(n * 8).chunks(8).map(|c| c.iter().fold(0u8, |a, &b| a << 1 | b)).collect()
}

/// CRC-16 of x^16 + x^12 + x^5 + 1 from all ones, complemented: a FIB's
/// (§5.2.1, annex E) and a DAB+ AU's (TS 102 563 §5.2).
pub fn crc16_ccitt(data: &[u8]) -> u16 {
    let mut c: u16 = 0xffff;
    for &b in data {
        c ^= (b as u16) << 8;
        for _ in 0..8 {
            c = if c & 0x8000 != 0 { (c << 1) ^ 0x1021 } else { c << 1 };
        }
    }
    !c
}

/// Does `data` end in its own (complemented) CCITT CRC?
pub fn crc16_ok(data: &[u8]) -> bool {
    let n = data.len();
    n >= 2 && crc16_ccitt(&data[..n - 2]) == u16::from_be_bytes([data[n - 2], data[n - 1]])
}

/// DAB+'s header Fire code (TS 102 563 §5.2): x^16 + x^14 + x^13 + x^12 +
/// x^11 + x^5 + x^3 + x^2 + x + 1 from zero, not complemented.
pub fn firecode(data: &[u8]) -> u16 {
    let mut c: u16 = 0;
    for &b in data {
        for k in (0..8).rev() {
            let fb = ((c >> 15) ^ (b as u16 >> k)) & 1;
            c <<= 1;
            if fb != 0 {
                c ^= 0x782f;
            }
        }
    }
    c
}

/// The mother code's four outputs for register value `reg` (a_i in bit 6,
/// a_i-6 in bit 0): generators 133, 171, 145, 133 octal (§11.1.1), x0 in
/// bit 3 of the result.
fn outputs(reg: u32) -> u8 {
    let p = |g: u32| ((reg & g).count_ones() & 1) as u8;
    p(0o133) << 3 | p(0o171) << 2 | p(0o145) << 1 | p(0o133)
}

/// Bits to the mother codeword (§11.1.1): 4 (I + 6) bits, zero-terminated.
pub fn encode(bits: &[u8]) -> Vec<u8> {
    let mut state: u32 = 0;
    let mut out = Vec::with_capacity(4 * (bits.len() + 6));
    for &b in bits.iter().chain([0u8; 6].iter()) {
        let reg = (b as u32 & 1) << 6 | state;
        let o = outputs(reg);
        out.extend_from_slice(&[o >> 3 & 1, o >> 2 & 1, o >> 1 & 1, o & 1]);
        state = reg >> 1;
    }
    out
}

/// For each bit of a punctured codeword, its index in the mother codeword
/// (§11.1.2), for a profile with `info_bits` = 32 x its blocks. Padding bits
/// at the end map to nothing (usize::MAX).
pub fn puncture_map(p: &Profile) -> Vec<usize> {
    let mut map = Vec::with_capacity(p.coded_bits());
    let mut m = 0usize;
    for &(blocks, pi) in &p.runs {
        for _ in 0..blocks {
            for _sub in 0..4 {
                for i in 0..32 {
                    if tables::kept(pi, i) {
                        map.push(m + i);
                    }
                }
                m += 32;
            }
        }
    }
    for i in 0..24 {
        if tables::PUNCTURE_TAIL >> (23 - i) & 1 == 1 {
            map.push(m + i);
        }
    }
    map.extend(std::iter::repeat(usize::MAX).take(p.padding));
    map
}

/// Puncture a mother codeword (transmitter side).
pub fn puncture(mother: &[u8], map: &[usize]) -> Vec<u8> {
    map.iter().map(|&m| if m == usize::MAX { 0 } else { mother[m] }).collect()
}

/// Soft bits: positive says 0, negative 1, zero knows nothing (a
/// punctured or erased bit), magnitude up to 127.
pub type Soft = i8;

/// A soft-decision Viterbi decoder for the mother code, reused across
/// calls (its buffers keep their size).
pub struct Viterbi {
    /// Path metrics, and the decision bits of every step (bit s of
    /// decisions[t]: which predecessor state s came from).
    metric: [i32; 64],
    decisions: Vec<u64>,
    mother: Vec<i16>,
    /// The four outputs for (new state, predecessor's low bit), as signs.
    out: [[u8; 2]; 64],
}

impl Default for Viterbi {
    fn default() -> Self {
        Self::new()
    }
}

impl Viterbi {
    pub fn new() -> Self {
        let mut out = [[0u8; 2]; 64];
        for (ns, o) in out.iter_mut().enumerate() {
            for b in 0..2 {
                // New state ns holds a_i in bit 5; it came from old state
                // ((ns & 31) << 1) | b, so the register was a_i, then old.
                let old = ((ns as u32 & 31) << 1) | b as u32;
                let reg = ((ns as u32 >> 5) << 6) | old;
                o[b] = outputs(reg);
            }
        }
        Viterbi { metric: [0; 64], decisions: Vec::new(), mother: Vec::new(), out }
    }

    /// Decode a punctured codeword's soft bits (`soft[i]` is coded bit
    /// `map[i]`'s) into `info_bits` bits, one per byte.
    pub fn decode(&mut self, soft: &[Soft], map: &[usize], info_bits: usize) -> Vec<u8> {
        let steps = info_bits + 6;
        self.mother.clear();
        self.mother.resize(4 * steps, 0);
        for (&s, &m) in soft.iter().zip(map) {
            if m != usize::MAX && m < self.mother.len() {
                self.mother[m] = s as i16;
            }
        }
        self.run(steps)
    }

    fn run(&mut self, steps: usize) -> Vec<u8> {
        const NEG: i32 = i32::MIN / 4;
        self.metric = [NEG; 64];
        self.metric[0] = 0;
        self.decisions.clear();
        self.decisions.resize(steps, 0);
        let mut next = [0i32; 64];
        for t in 0..steps {
            let s = &self.mother[4 * t..4 * t + 4];
            // The branch metric of each of the 16 output patterns.
            let mut bm = [0i32; 16];
            for (o, m) in bm.iter_mut().enumerate() {
                for (j, &v) in s.iter().enumerate() {
                    let bit = o >> (3 - j) & 1;
                    *m += if bit == 0 { v as i32 } else { -(v as i32) };
                }
            }
            let mut dec = 0u64;
            for ns in 0..64 {
                let p0 = (ns & 31) << 1;
                let m0 = self.metric[p0] + bm[self.out[ns][0] as usize];
                let m1 = self.metric[p0 | 1] + bm[self.out[ns][1] as usize];
                if m1 > m0 {
                    next[ns] = m1;
                    dec |= 1 << ns;
                } else {
                    next[ns] = m0;
                }
            }
            self.decisions[t] = dec;
            // Keep the metrics bounded: subtract the best.
            let best = *next.iter().max().unwrap();
            for (m, n) in self.metric.iter_mut().zip(next.iter()) {
                *m = n - best;
            }
        }
        // The code is terminated: trace back from state 0.
        let mut bits = vec![0u8; steps];
        let mut st = 0usize;
        for t in (0..steps).rev() {
            bits[t] = (st >> 5) as u8;
            let b = (self.decisions[t] >> st & 1) as usize;
            st = ((st & 31) << 1) | b;
        }
        bits.truncate(steps - 6);
        bits
    }
}

/// Bits (one per byte) to bytes, first bit in the high end.
pub fn pack(bits: &[u8]) -> Vec<u8> {
    bits.chunks(8).map(|c| c.iter().fold(0u8, |a, &b| a << 1 | (b & 1)) << (8 - c.len())).collect()
}

/// Bytes to bits, one per byte, first bit from the high end.
pub fn unpack(bytes: &[u8]) -> Vec<u8> {
    bytes.iter().flat_map(|&b| (0..8).rev().map(move |k| b >> k & 1)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dab::tables::{fic_profile, Protection};

    #[test]
    fn the_prbs_starts_as_table_12_prints() {
        assert_eq!(prbs(16), vec![0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 0]);
    }

    #[test]
    fn crcs_check_what_they_made() {
        let mut fib = vec![0x55u8; 30];
        let c = crc16_ccitt(&fib);
        fib.extend_from_slice(&c.to_be_bytes());
        assert!(crc16_ok(&fib));
        fib[3] ^= 1;
        assert!(!crc16_ok(&fib));
        // The CRC-16/CCITT check value of "123456789", complemented.
        assert_eq!(crc16_ccitt(b"123456789"), !0x29b1);
        assert_eq!(firecode(&[0; 9]), 0);
    }

    fn soft_of(bits: &[u8], noise: &mut crate::testsig::Rng, sigma: f64) -> Vec<Soft> {
        bits.iter()
            .map(|&b| {
                let v = (if b == 0 { 1.0 } else { -1.0 }) + sigma * noise.gauss();
                (v * 40.0).round().clamp(-127.0, 127.0) as Soft
            })
            .collect()
    }

    #[test]
    fn viterbi_undoes_every_profile_and_corrects_noise() {
        let mut rng = crate::testsig::Rng::new(7);
        let profiles = [
            fic_profile(),
            Protection::Uep { index: 35 }.profile().unwrap(),
            Protection::Eep { option: 0, level: 3, size: 48 }.profile().unwrap(),
            Protection::Eep { option: 1, level: 2, size: 21 }.profile().unwrap(),
        ];
        let mut v = Viterbi::new();
        for p in &profiles {
            let info: Vec<u8> = (0..p.info_bits()).map(|_| (rng.next_u64() & 1) as u8).collect();
            let map = puncture_map(p);
            assert_eq!(map.len(), p.coded_bits());
            let tx = puncture(&encode(&info), &map);
            // Clean, and through noise at Eb/N0 well inside the code's reach.
            assert_eq!(v.decode(&soft_of(&tx, &mut rng, 0.0), &map, info.len()), info);
            let got = v.decode(&soft_of(&tx, &mut rng, 0.6), &map, info.len());
            let errs = got.iter().zip(&info).filter(|(a, b)| a != b).count();
            assert_eq!(errs, 0, "{p:?}");
        }
    }

    #[test]
    fn packing_round_trips() {
        let b = [0xa5u8, 0x01, 0xff];
        assert_eq!(pack(&unpack(&b)), b);
        assert_eq!(prbs_bytes(2), pack(&prbs(16)));
    }
}
