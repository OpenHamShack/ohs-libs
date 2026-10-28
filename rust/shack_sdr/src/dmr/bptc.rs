//! DMR's block product turbo codes (TS 102 361-1 B.1): BPTC(196,96), which
//! carries every 96-bit data burst payload (the voice LC header and
//! terminator, CSBK, data header, rate 1/2 data), and the embedded LC's
//! (128,72), spread over four voice bursts' 32-bit fragments.
//!
//! BPTC(196,96) is a 13 x 15 matrix, after one reserved bit: nine rows of
//! Hamming(15,11) and fifteen columns of Hamming(13,9), interleaved by
//! position i -> 181 i mod 196. Decoding alternates columns and rows, up to
//! five passes, as each pass's corrections can make the other's possible.

use super::fec::{H13_9, H15_11, H16_11};

/// The 96 data bits' places in the deinterleaved matrix: row 0 begins with
/// three reserved bits (positions 1-3), so it holds 8; the other eight rows
/// 11 each.
fn data_positions() -> impl Iterator<Item = usize> {
    (0..9).flat_map(|r| {
        let first = if r == 0 { 4 } else { r * 15 + 1 };
        first..r * 15 + 12
    })
}

/// 96 data bits to the 196 transmitted bits.
pub fn encode196(data: &[u8; 96]) -> [u8; 196] {
    let mut m = [0u8; 196];
    for (p, &b) in data_positions().zip(data.iter()) {
        m[p] = b;
    }
    for r in 0..9 {
        H15_11.encode(&mut m[r * 15 + 1..r * 15 + 16]);
    }
    for c in 0..15 {
        let mut col: Vec<u8> = (0..13).map(|a| m[c + 1 + 15 * a]).collect();
        H13_9.encode(&mut col);
        for (a, b) in col.into_iter().enumerate() {
            m[c + 1 + 15 * a] = b;
        }
    }
    let mut out = [0u8; 196];
    for (a, &b) in m.iter().enumerate() {
        out[(a * 181) % 196] = b;
    }
    out
}

/// What a BPTC decode found.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Decoded {
    /// Bits the Hamming codes corrected.
    pub corrected: u32,
    /// Every row and column checks after correction: the data can be
    /// trusted as far as the code goes (a CRC or RS check then decides).
    pub clean: bool,
}

fn check_all(m: &[u8; 196]) -> bool {
    let rows = (0..9).all(|r| H15_11.decode(&mut m[r * 15 + 1..r * 15 + 16].to_vec()) == Some(0));
    let cols = (0..15).all(|c| {
        let mut col: Vec<u8> = (0..13).map(|a| m[c + 1 + 15 * a]).collect();
        H13_9.decode(&mut col) == Some(0)
    });
    rows && cols
}

/// 196 received bits to 96 data bits.
pub fn decode196(bits: &[u8; 196]) -> ([u8; 96], Decoded) {
    let mut m = [0u8; 196];
    for (a, v) in m.iter_mut().enumerate() {
        *v = bits[(a * 181) % 196];
    }
    let mut corrected = 0;
    for _ in 0..5 {
        let mut fixed = 0;
        for c in 0..15 {
            let mut col: Vec<u8> = (0..13).map(|a| m[c + 1 + 15 * a]).collect();
            if let Some(n) = H13_9.decode(&mut col) {
                if n > 0 {
                    fixed += n;
                    for (a, b) in col.into_iter().enumerate() {
                        m[c + 1 + 15 * a] = b;
                    }
                }
            }
        }
        for r in 0..9 {
            if let Some(n) = H15_11.decode(&mut m[r * 15 + 1..r * 15 + 16]) {
                fixed += n;
            }
        }
        corrected += fixed;
        if fixed == 0 {
            break;
        }
    }
    let mut data = [0u8; 96];
    for (d, p) in data.iter_mut().zip(data_positions()) {
        *d = m[p];
    }
    (data, Decoded { corrected, clean: check_all(&m) })
}

/// The embedded LC (B.2.1): 72 LC bits and a 5-bit checksum in a 8 x 16
/// matrix, seven rows of Hamming(16,11,4) and a row of column parity, sent
/// column by column as four 32-bit fragments.
pub fn encode_embedded(lc: &[u8; 72], checksum: u8) -> [u8; 128] {
    let w = H16_11.n();
    let mut m = [0u8; 128];
    // Data rows 0-1 carry 11 LC bits, rows 2-6 ten and a checksum bit in
    // column 10 (most significant first).
    let mut it = lc.iter();
    for r in 0..7 {
        let n = if r < 2 { 11 } else { 10 };
        for c in 0..n {
            m[r * w + c] = *it.next().expect("72 bits");
        }
        if r >= 2 {
            m[r * w + 10] = (checksum >> (6 - r)) & 1;
        }
        H16_11.encode(&mut m[r * w..r * w + w]);
    }
    for c in 0..w {
        m[7 * w + c] = (0..7).fold(0, |a, r| a ^ m[r * w + c]);
    }
    let mut out = [0u8; 128];
    let mut b = 0;
    for o in out.iter_mut() {
        *o = m[b];
        b += 16;
        if b > 127 {
            b -= 127;
        }
    }
    out
}

/// 128 bits gathered from four voice bursts back to the LC and its
/// checksum; None when a row or the column parity fails (the code is
/// distance 4 a row: one error a row is corrected, two refused).
pub fn decode_embedded(bits: &[u8; 128]) -> Option<([u8; 72], u8, u32)> {
    let w = 16;
    let mut m = [0u8; 128];
    let mut b = 0;
    for &x in bits.iter() {
        m[b] = x;
        b += 16;
        if b > 127 {
            b -= 127;
        }
    }
    let mut corrected = 0;
    for r in 0..7 {
        corrected += H16_11.decode(&mut m[r * w..r * w + w])?;
    }
    if (0..w).any(|c| (0..8).fold(0, |a, r| a ^ m[r * w + c]) != 0) {
        return None;
    }
    let mut lc = [0u8; 72];
    let mut k = 0;
    let mut sum = 0u8;
    for r in 0..7 {
        let n = if r < 2 { 11 } else { 10 };
        for c in 0..n {
            lc[k] = m[r * w + c];
            k += 1;
        }
        if r >= 2 {
            sum = (sum << 1) | m[r * w + 10];
        }
    }
    Some((lc, sum, corrected))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pattern(n: usize, seed: u32) -> Vec<u8> {
        (0..n).map(|i| ((seed.wrapping_mul(2_654_435_761).wrapping_add(i as u32 * 40_503) >> 7) & 1) as u8).collect()
    }

    #[test]
    fn bptc196_corrects_scattered_errors_and_says_so() {
        let data: [u8; 96] = pattern(96, 7).try_into().unwrap();
        let tx = encode196(&data);
        let (d, r) = decode196(&tx);
        assert_eq!(d, data);
        assert_eq!(r, Decoded { corrected: 0, clean: true });
        let mut rx = tx;
        for i in [3usize, 50, 101, 150, 190] {
            rx[i] ^= 1;
        }
        let (d, r) = decode196(&rx);
        assert_eq!(d, data);
        assert!(r.clean && r.corrected >= 4, "{r:?}");
    }

    #[test]
    fn the_embedded_lc_survives_one_error_a_row() {
        let lc: [u8; 72] = pattern(72, 3).try_into().unwrap();
        let tx = encode_embedded(&lc, 0b10110);
        assert_eq!(decode_embedded(&tx), Some((lc, 0b10110, 0)));
        let mut rx = tx;
        // Two errors in different rows (raw bit a sits in row 16a mod 127 / 16).
        rx[5] ^= 1;
        rx[8] ^= 1;
        let (got, sum, n) = decode_embedded(&rx).unwrap();
        assert_eq!((got, sum, n), (lc, 0b10110, 2));
    }
}
