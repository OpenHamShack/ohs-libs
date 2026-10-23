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

use crate::decoder::Value;
use rustfft::num_complex::Complex32;

/// The four quarters' first carrier (§14.8.1: the positive half starts at
/// 1, carrier 0 being unused).
pub const QUARTER_BASE: [i32; 4] = [-768, -384, 1, 385];
pub const COMBS: usize = 24;
pub const PATTERNS: usize = 70;
const GROUPS: usize = 8;
const PAIRS: usize = 4 * COMBS * GROUPS;
/// Each parity's averages follow about this many of its frames (16 x 192
/// ms: 3 s), a new frame weighing 1/AVERAGE once that many are in.
const AVERAGE: f32 = 16.0;
/// The list is worked out every this many frames (32 x 96 ms: 3 s), once
/// each parity has MIN_FRAMES frames in.
const EVERY: u64 = 32;
const MIN_FRAMES: u32 = 8;
/// A group stands out when it is this many times the empty parity's median
/// group. Noise's group (four Rayleigh magnitudes summed) has a spread of
/// about a quarter of its median, so the largest of the 192 an empty null
/// gives is about 1.8 times it, and four in one comb at once above 2.2
/// times is a chance of order 1e-8 per look; a transmitter's group grows
/// with averaging while noise's does not.
const THRESHOLD: f32 = 2.2;
/// At this many times the median every group of a transmitter is beyond
/// doubt: confidence 1.
const SURE: f32 = 6.0;
/// At most this many transmitters (the strongest) are listed.
const MAX_LISTED: usize = 12;

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

fn pair_index(quarter: usize, comb: usize, group: usize) -> usize {
    (quarter * COMBS + comb) * GROUPS + group
}

/// One transmitter heard.
#[derive(Clone, Debug, PartialEq)]
pub struct Heard {
    pub main: u8,
    pub sub: u8,
    /// Its share of the received power, dB (0: all of it).
    pub level_db: f32,
    /// 0-1: how far its weakest group stands above noise.
    pub confidence: f32,
}

/// The TII detector: fed each frame's null and phase reference carriers.
pub struct Detector {
    /// Per frame parity: each pair's y[k0] conj(y[k0 + 1]), averaged.
    pairs: [Vec<Complex32>; 2],
    /// Per frame parity: the null's mean carrier power, averaged.
    null_power: [f32; 2],
    frames: [u32; 2],
    /// The phase reference's mean carrier power, averaged.
    prs_power: f32,
    count: u64,
    heard: Vec<Heard>,
}

impl Default for Detector {
    fn default() -> Self {
        Self::new()
    }
}

impl Detector {
    pub fn new() -> Self {
        Detector {
            pairs: [vec![Complex32::new(0.0, 0.0); PAIRS], vec![Complex32::new(0.0, 0.0); PAIRS]],
            null_power: [0.0; 2],
            frames: [0; 2],
            prs_power: 0.0,
            count: 0,
            heard: Vec::new(),
        }
    }

    /// Forget everything: a new lock (whose frame parity may differ from
    /// the last's), a retune.
    pub fn clear(&mut self) {
        *self = Self::new();
    }

    /// What was heard at the last look, strongest first.
    pub fn heard(&self) -> &[Heard] {
        &self.heard
    }

    /// One frame: its null's carriers and its phase reference's, both by
    /// k + 768 from the same FFT and windowing.
    pub fn push(&mut self, null: &[Complex32], prs: &[Complex32]) {
        let par = (self.count % 2) as usize;
        self.count += 1;
        self.frames[par] += 1;
        // A running mean until AVERAGE frames are in, then exponential.
        let a = 1.0 / (self.frames[par] as f32).min(AVERAGE);
        let at = |k: i32| null[(k + 768) as usize];
        let acc = &mut self.pairs[par];
        for (q, base) in QUARTER_BASE.iter().enumerate() {
            for c in 0..COMBS {
                for b in 0..GROUPS {
                    let k0 = base + 2 * c as i32 + 48 * b as i32;
                    let v = at(k0) * at(k0 + 1).conj();
                    let slot = &mut acc[pair_index(q, c, b)];
                    *slot += (v - *slot) * a;
                }
            }
        }
        let mean = |x: &[Complex32]| x.iter().map(|v| v.norm_sqr()).sum::<f32>() / (x.len() - 1) as f32;
        self.null_power[par] += (mean(null) - self.null_power[par]) * a;
        let b = 1.0 / (self.count as f32).min(2.0 * AVERAGE);
        self.prs_power += (mean(prs) - self.prs_power) * b;
        if self.count % EVERY == 0 && self.frames.iter().all(|&n| n >= MIN_FRAMES) {
            self.look();
        }
    }

    /// Each group's figure for one parity: its four quarters' magnitudes.
    fn groups(&self, par: usize) -> Vec<f32> {
        let mut g = vec![0.0; COMBS * GROUPS];
        for q in 0..4 {
            for c in 0..COMBS {
                for b in 0..GROUPS {
                    g[c * GROUPS + b] += self.pairs[par][pair_index(q, c, b)].norm();
                }
            }
        }
        g
    }

    fn look(&mut self) {
        let g = [self.groups(0), self.groups(1)];
        // The parity carrying TII has the larger groups; the other is noise.
        let total = |x: &[f32]| x.iter().sum::<f32>();
        let on = if total(&g[0]) >= total(&g[1]) { 0 } else { 1 };
        let mut quiet = g[1 - on].clone();
        quiet.sort_by(f32::total_cmp);
        let noise = quiet[quiet.len() / 2].max(1e-30);
        let g = &g[on];
        // The signal's power per carrier, less the noise the empty null
        // shows; a transmitter's pair figure is its own power per carrier.
        let signal = (self.prs_power - self.null_power[1 - on]).max(1e-30);
        let mut heard = Vec::new();
        for c in 0..COMBS {
            let z: Vec<f32> = (0..GROUPS).map(|b| g[c * GROUPS + b] / noise).collect();
            let strong: u8 = (0..GROUPS).filter(|&b| z[b] >= THRESHOLD).fold(0, |m, b| m | 1 << b);
            if strong.count_ones() < 4 {
                continue;
            }
            let weakest = |mask: u8| (0..GROUPS).filter(|b| mask >> b & 1 == 1).map(|b| z[b]).fold(f32::INFINITY, f32::min);
            let within: Vec<(u8, u8)> = (0..PATTERNS as u8).filter_map(|p| pattern(p).map(|m| (p, m))).filter(|&(_, m)| m & strong == m).collect();
            if strong.count_ones() == 4 {
                // One transmitter: its pairs, less what noise adds, over 16.
                let (p, m) = within[0];
                let mine: f32 = (0..GROUPS).filter(|b| m >> b & 1 == 1).map(|b| g[c * GROUPS + b] - noise).sum::<f32>() / 16.0;
                let confidence = ((weakest(m) - THRESHOLD) / (SURE - THRESHOLD)).clamp(0.0, 1.0);
                heard.push(Heard { main: p, sub: c as u8, level_db: 10.0 * (mine.max(1e-30) / signal).log10(), confidence });
                continue;
            }
            // More than four: two transmitters share this comb (as two
            // regions' transmitters with one sub-identifier can), and
            // their patterns usually share groups too. Each pair of
            // patterns covering the strong groups is fitted to the comb's
            // eight figures as noise (1) plus each one's own level where
            // it sends, the levels by least squares; the pair that fits
            // best wins. A group both send reads about the sum, one only
            // sends reads its own, so the right pair fits far better than
            // a pair that puts a shared group where only one sends.
            let mut best: Option<(f32, (u8, u8), (u8, u8), f32, f32)> = None;
            for (i, &(p1, m1)) in within.iter().enumerate() {
                for &(p2, m2) in &within[i + 1..] {
                    if m1 | m2 != strong {
                        continue;
                    }
                    let x = |b: usize| z[b] - 1.0;
                    let ind = |m: u8, b: usize| (m >> b & 1) as f32;
                    let o = (m1 & m2).count_ones() as f32;
                    let s1: f32 = (0..GROUPS).map(|b| ind(m1, b) * x(b)).sum();
                    let s2: f32 = (0..GROUPS).map(|b| ind(m2, b) * x(b)).sum();
                    let det = 16.0 - o * o;
                    let l1 = (4.0 * s1 - o * s2) / det;
                    let l2 = (4.0 * s2 - o * s1) / det;
                    let res: f32 = (0..GROUPS).map(|b| (x(b) - l1 * ind(m1, b) - l2 * ind(m2, b)).powi(2)).sum();
                    if best.as_ref().is_none_or(|bst| res < bst.0) {
                        best = Some((res, (p1, m1), (p2, m2), l1, l2));
                    }
                }
            }
            let Some((_, (p1, _), (p2, _), l1, l2)) = best else { continue };
            for (p, l) in [(p1, l1), (p2, l2)] {
                // A level in noise medians; a group figure is four pairs.
                let mine = l * noise / 4.0;
                // Which four groups are whose is a fit, not a reading.
                let confidence = 0.7 * ((1.0 + l - THRESHOLD) / (SURE - THRESHOLD)).clamp(0.0, 1.0);
                heard.push(Heard { main: p, sub: c as u8, level_db: 10.0 * (mine.max(1e-30) / signal).log10(), confidence });
            }
        }
        heard.sort_by(|a, b| b.level_db.total_cmp(&a.level_db));
        heard.truncate(MAX_LISTED);
        self.heard = heard;
    }
}

/// The feed's `tii` list: `[{"main", "sub", "levelDb", "confidence"}]`,
/// strongest first; the level to 0.5 dB and the confidence to 0.1, so the
/// meta (sent when anything in it changes) does not change with every
/// look's small wander.
pub fn to_value(heard: &[Heard]) -> Value {
    Value::List(
        heard
            .iter()
            .map(|h| {
                Value::Map(vec![
                    ("main".into(), Value::Num(h.main as f64)),
                    ("sub".into(), Value::Num(h.sub as f64)),
                    ("levelDb".into(), Value::Num((h.level_db as f64 * 2.0).round() / 2.0)),
                    ("confidence".into(), Value::Num((h.confidence as f64 * 10.0).round() / 10.0)),
                ])
            })
            .collect(),
    )
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
