//! The AMBE+2 frames inside a DMR voice burst, and their channel coding:
//! from 72 received bits to the 49 bits a vocoder takes, and back for the
//! test transmitter.
//!
//! **This is framing and FEC only, never the vocoder.** Turning the 49
//! bits into sound is the patented AMBE+2 algorithm, which only the user's
//! own mbelib does, loaded at run time (vocoder.rs, RADIO-SDR-PLAN §9.2).
//! What is here is the channel code around it, which DMR's specification
//! and every open decoder describe the same way:
//!
//! - a burst's 216 voice bits are three 72-bit frames (bits 0-71, 72-107
//!   with 156-191 across the sync, 192-263);
//! - each frame's 36 dibits are spread over four vectors: C0 (24 bits,
//!   Golay(24,12)), C1 (23 bits, Golay(23,12), whitened by a pseudo-random
//!   sequence seeded from C0's data), C2 (11 bits) and C3 (14 bits), both
//!   unprotected;
//! - the 49 bits are C0's 12 data bits, C1's 12, then C2 and C3.
//!
//! The interleave schedule is DSD's (`dmr_const.h` rW/rX/rY/rZ, ISC), and
//! the whitening is mbelib's `mbe_demodulateAmbe3600x2450Data` (ISC), both
//! checked against MMDVMHost's DMR_A/B/C tables and PRNG table; the order of
//! the 49 bits is what mbelib's `mbe_processAmbe2450Dataf` reads.

use super::fec::{golay23_decode, golay23_encode, golay24_decode, golay24_encode};

/// For dibit i of a frame, its first bit goes to vector RW[i], bit RX[i];
/// its second to RY[i], RZ[i].
const RW: [usize; 36] = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2];
const RX: [usize; 36] =
    [23, 10, 22, 9, 21, 8, 20, 7, 19, 6, 18, 5, 17, 4, 16, 3, 15, 2, 14, 1, 13, 0, 12, 10, 11, 9, 10, 8, 9, 7, 8, 6, 7, 5, 6, 4];
const RY: [usize; 36] = [0, 2, 0, 2, 0, 2, 0, 2, 0, 3, 0, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3];
const RZ: [usize; 36] =
    [5, 3, 4, 2, 3, 1, 2, 0, 1, 13, 0, 12, 22, 11, 21, 10, 20, 9, 19, 8, 18, 7, 17, 6, 16, 5, 15, 4, 14, 3, 13, 2, 12, 1, 11, 0];

/// The four vectors' lengths.
const LEN: [usize; 4] = [24, 23, 11, 14];

/// The bits per vocoder frame and per frame on the air.
pub const DATA_BITS: usize = 49;
pub const FRAME_BITS: usize = 72;

/// The whitening over C1: bit j of the 23-bit word is mbelib's pr[23 - j],
/// the top bit of a linear congruential sequence seeded with 16 x C0's data.
pub fn prng_mask(c0_data: u32) -> u32 {
    let mut pr = (16 * (c0_data & 0xFFF)) as u32;
    let mut mask = 0u32;
    for k in 1..24 {
        pr = (173 * pr + 13849) % 65536;
        if pr >= 32768 {
            mask |= 1 << (23 - k);
        }
    }
    mask
}

/// One frame decoded.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Decoded {
    pub data: [u8; DATA_BITS],
    /// Bits corrected in C0 (4: more errors than it can correct).
    pub errs_c0: u32,
    /// Bits corrected in C0 and C1 together: what mbelib takes as its
    /// error count, and what our BER estimate counts.
    pub errs: u32,
}

/// The vectors as integers, bit k of vector v being its bit k.
fn vectors(frame: &[u8]) -> [u32; 4] {
    let mut v = [0u32; 4];
    for i in 0..36 {
        v[RW[i]] |= (frame[2 * i] as u32 & 1) << RX[i];
        v[RY[i]] |= (frame[2 * i + 1] as u32 & 1) << RZ[i];
    }
    v
}

/// 72 received bits to the 49 vocoder bits.
pub fn decode(frame: &[u8]) -> Decoded {
    assert!(frame.len() >= FRAME_BITS);
    let v = vectors(frame);
    let (a, errs_c0) = match golay24_decode(v[0]) {
        Some(x) => x,
        // Four errors: keep what (23,12) makes of it, as mbelib would, and
        // say it is beyond correction.
        None => (golay23_decode(v[0] >> 1).0, 4),
    };
    let (b, errs_c1) = golay23_decode(v[1] ^ prng_mask(a));
    let mut data = [0u8; DATA_BITS];
    let mut k = 0;
    let mut put = |x: u32, n: usize| {
        for i in (0..n).rev() {
            data[k] = ((x >> i) & 1) as u8;
            k += 1;
        }
    };
    put(a, 12);
    put(b, 12);
    put(v[2], LEN[2]);
    put(v[3], LEN[3]);
    Decoded { data, errs_c0, errs: errs_c0 + errs_c1 }
}

/// 49 vocoder bits to the 72 sent, for the test transmitter.
pub fn encode(data: &[u8; DATA_BITS]) -> [u8; FRAME_BITS] {
    let take = |from: usize, n: usize| data[from..from + n].iter().fold(0u32, |a, &b| (a << 1) | (b as u32 & 1));
    let a = take(0, 12);
    let v = [golay24_encode(a), golay23_encode(take(12, 12)) ^ prng_mask(a), take(24, 11), take(35, 14)];
    let mut out = [0u8; FRAME_BITS];
    for i in 0..36 {
        out[2 * i] = ((v[RW[i]] >> RX[i]) & 1) as u8;
        out[2 * i + 1] = ((v[RY[i]] >> RZ[i]) & 1) as u8;
    }
    out
}

/// A voice burst's three frames, in order.
pub fn frames_of(burst: &[u8; 264]) -> [[u8; FRAME_BITS]; 3] {
    let mut f = [[0u8; FRAME_BITS]; 3];
    f[0].copy_from_slice(&burst[0..72]);
    f[1][..36].copy_from_slice(&burst[72..108]);
    f[1][36..].copy_from_slice(&burst[156..192]);
    f[2].copy_from_slice(&burst[192..264]);
    f
}

/// Put three frames into a burst's voice bits.
pub fn put_frames(burst: &mut [u8; 264], f: &[[u8; FRAME_BITS]; 3]) {
    burst[0..72].copy_from_slice(&f[0]);
    burst[72..108].copy_from_slice(&f[1][..36]);
    burst[156..192].copy_from_slice(&f[1][36..]);
    burst[192..264].copy_from_slice(&f[2]);
}

/// The AMBE+2 silence frame as every DMR network sends it, and as
/// MMDVMHost's DMR_SILENCE_DATA carries it three times a burst.
pub const SILENCE: [u8; 9] = [0xB9, 0xE8, 0x81, 0x52, 0x61, 0x73, 0x00, 0x2A, 0x6B];

/// Bytes to bits, most significant first.
pub fn bits_of(bytes: &[u8]) -> Vec<u8> {
    bytes.iter().flat_map(|&b| (0..8).rev().map(move |i| (b >> i) & 1)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_interleave_is_a_permutation_of_the_four_vectors() {
        let mut seen = [[false; 24]; 4];
        for i in 0..36 {
            for (w, x) in [(RW[i], RX[i]), (RY[i], RZ[i])] {
                assert!(x < LEN[w] && !seen[w][x], "dibit {i}");
                seen[w][x] = true;
            }
        }
        assert_eq!(seen.iter().flatten().filter(|&&s| s).count(), 72);
    }

    #[test]
    fn the_whitening_matches_the_known_table() {
        // MMDVMHost's PRNG_TABLE[d] >> 1 is the 23-bit mask for C0 data d.
        for (d, t) in [(0u32, 0x42CC47u32), (1, 0x19D6FE), (2, 0x304729), (9, 0xDE25CE)] {
            assert_eq!(prng_mask(d), t >> 1, "data {d}");
        }
    }

    #[test]
    fn the_networks_silence_frame_decodes_with_no_errors() {
        // If the interleave, either Golay or the whitening were wrong, a real
        // frame would not decode clean.
        let d = decode(&bits_of(&SILENCE));
        assert_eq!((d.errs_c0, d.errs), (0, 0));
        assert_eq!(encode(&d.data).to_vec(), bits_of(&SILENCE));
    }

    #[test]
    fn frames_round_trip_and_correct_c0_and_c1_errors() {
        for seed in 0..20u32 {
            let mut data = [0u8; DATA_BITS];
            for (i, b) in data.iter_mut().enumerate() {
                *b = ((seed.wrapping_mul(0x9E37_79B9).rotate_left(i as u32) >> 3) & 1) as u8;
            }
            let mut f = encode(&data);
            assert_eq!(decode(&f).data, data);
            // Dibit 0's first bit is in C0, dibit 1's first in C1.
            f[0] ^= 1;
            f[2] ^= 1;
            let d = decode(&f);
            assert_eq!(d.data, data);
            assert_eq!((d.errs_c0, d.errs), (1, 2));
        }
    }
}
