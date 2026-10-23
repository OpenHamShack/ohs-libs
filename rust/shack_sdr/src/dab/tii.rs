//! Transmitter Identification Information (EN 300 401 §14.8), mode I: which
//! transmitters of a single-frequency network are being received.
//!
//! **What is sent.** Every transmitter puts a few carrier pairs into the
//! null symbol of every other frame (those holding CIFs 0-3 mod 8): for its
//! comb c (the sub-identifier, 0-23) and pattern p (the main identifier,
//! 0-69, four of eight groups), the pair (k0, k0 + 1) at k0 = base + 2c +
//! 48b for each of its four groups b, in each of the four 384-carrier
//! quarters (bases -768, -384, 1, 385): 32 carriers, each as strong as a
//! data carrier, both carriers of a pair with the phase reference's phase
//! at k0.
//!
//! **How it is found, our own design.** For each of the 768 pair positions
//! the null's carriers give y[k0] conj(y[k0 + 1]). For a transmitter's
//! pair that is |H|^2 times a phase set only by its delay (the PRS phase is
//! the same on both carriers and cancels; a fading or Doppler phase is
//! common to both and cancels too), so averaging it over frames builds it
//! up coherently, while noise in two different carriers averages towards
//! zero: no noise floor to subtract, and a steady transmitter 20 dB under
//! the strongest still stands out after a few seconds. The averages are
//! kept separately for odd and even frames, since only one of the two
//! carries TII: the stronger parity is the TII one and the other, which
//! carries nothing, says how large pure noise makes the statistic (its
//! median group), which sets the detection threshold whatever the SNR.
//! A group's figure is the sum over the four quarters of its pair's
//! magnitude (so frequency-selective fading in one quarter costs only that
//! quarter), and a transmitter is a pattern whose four groups all stand
//! above the threshold in one comb.
//!
//! **Its level** is its carriers' power against the phase reference's
//! carriers (less the noise the empty null shows): the share of the block's
//! received power that comes from that transmitter, 0 dB for a lone one,
//! about -3 dB each for two equal ones. Prior art read: welle.io's
//! `tii-decoder.cpp` (GPL-2.0-or-later) sums pair powers per comb against a
//! fixed threshold; none of its code is used.

/// The four quarters' first carrier (§14.8.1: the positive half starts at
/// 1, carrier 0 being unused).
pub const QUARTER_BASE: [i32; 4] = [-768, -384, 1, 385];
pub const COMBS: usize = 24;
pub const PATTERNS: usize = 70;
const GROUPS: usize = 8;

/// Main identifier p's four groups, as a mask with bit b set for group b.
/// Table 26 lists the 70 four-of-eight patterns in ascending order of the
/// eight bits read as a binary number with b = 0 the most significant, so
/// it is that order, built rather than copied.
pub fn pattern(main: u8) -> Option<u8> {
    (0u16..256).filter(|v| v.count_ones() == 4).nth(main as usize).map(|v| (0..8).fold(0u8, |m, b| m | ((((v >> (7 - b)) & 1) as u8) << b)))
}

/// The 32 carriers k (in -768..=768) that TII (main, sub) switches on.
pub fn carriers(main: u8, sub: u8) -> Option<Vec<i32>> {
    let mask = pattern(main)?;
    if sub as usize >= COMBS {
        return None;
    }
    let mut ks = Vec::with_capacity(32);
    for base in QUARTER_BASE {
        for b in 0..GROUPS as i32 {
            if mask >> b & 1 == 1 {
                let k0 = base + 2 * sub as i32 + 48 * b;
                ks.extend([k0, k0 + 1]);
            }
        }
    }
    Some(ks)
}

#[cfg(test)]
mod sent {
    use super::*;
    use crate::dab::tables::{TF, TNULL, TU};
    use crate::dab::tx::{Transmitter, TxEnsemble};
    use rustfft::FftPlanner;

    #[test]
    fn patterns_are_table_26() {
        // Spot rows of table 26 (b = 0..7 left to right), its first and
        // last, and that all 70 differ with four groups each.
        let row = |s: &str| s.bytes().enumerate().fold(0u8, |m, (b, c)| m | (((c == b'1') as u8) << b));
        for (p, s) in [(0, "00001111"), (11, "00110110"), (34, "01111000"), (35, "10000111"), (50, "10101100"), (69, "11110000")] {
            assert_eq!(pattern(p), Some(row(s)), "pattern {p}");
        }
        let all: std::collections::HashSet<u8> = (0..70).map(|p| pattern(p).unwrap()).collect();
        assert_eq!(all.len(), 70);
        assert!(all.iter().all(|m| m.count_ones() == 4));
        assert_eq!(pattern(70), None);
    }

    #[test]
    fn carriers_are_figure_64() {
        // Figure 64's example, comb 1 and pattern 11: its labelled pairs.
        let ks = carriers(11, 1).unwrap();
        for k in [-670, -669, -622, -621, -526, -525, -478, -477, -286, -285, -238, -237, -142, -141, -94, -93, 99, 100, 147, 148, 243, 244, 291, 292, 483, 484, 531, 532, 627, 628, 675, 676] {
            assert!(ks.contains(&k), "{k} missing from {ks:?}");
        }
        assert_eq!(ks.len(), 32);
        assert_eq!(carriers(0, 24), None);
    }

    #[test]
    fn the_model_sends_tii_in_every_other_null() {
        let mut tx = Transmitter::new(TxEnsemble::standard());
        tx.tii = Some((11, 1));
        let fft = FftPlanner::<f32>::new().plan_fft_forward(TU);
        let want = carriers(11, 1).unwrap();
        for frame in 0..4 {
            let x = tx.frame();
            assert_eq!(x.len(), TF);
            let mut y = x[TNULL - TU..TNULL].to_vec();
            fft.process(&mut y);
            let p = |k: i32| y[(k + TU as i32) as usize % TU].norm_sqr();
            let on: f32 = want.iter().map(|&k| p(k)).sum::<f32>() / 32.0;
            let off: f32 = (-768..=768).filter(|k| *k != 0 && !want.contains(k)).map(p).sum::<f32>();
            if frame % 2 == 0 {
                assert!(on > 0.1 && off < 1e-6 * on, "frame {frame}: on {on}, off {off}");
                // Both carriers of a pair have one phase.
                for pr in want.chunks(2) {
                    let (a, b) = (y[(pr[0] + TU as i32) as usize % TU], y[(pr[1] + TU as i32) as usize % TU]);
                    assert!((a * b.conj()).arg().abs() < 1e-3, "pair {pr:?}");
                }
            } else {
                assert!(on + off < 1e-9, "frame {frame} should have a silent null");
            }
        }
    }
}
