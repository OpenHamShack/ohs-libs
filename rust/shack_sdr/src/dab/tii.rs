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
//! quarter; one quarter far over the rest is a spur's, and left out), and
//! a transmitter is a pattern whose four groups all stand above the
//! threshold in one comb.
//!
//! **What real signals added** (nine Band III blocks recorded on ohs-box,
//! 23/10/2026; tests/dab_tii_real.rs holds the detector to them): a
//! transmitter's amplifier mixes its own TII carriers, putting products
//! about 30 dB under it on its comb's other groups and, in its pattern, on
//! the combs either side; a weak transmitter beside a loud one cannot be
//! told from those, so it is listed at low confidence; and receivers have
//! spurs. `look` says how each is handled.
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
/// The list is worked out when each parity first has MIN_FRAMES frames
/// in, then every this many frames (32 x 96 ms: 3 s).
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
/// The quietest noise a look assumes, dB under the signal (see `look`).
const FLOOR_DB: f32 = -50.0;
/// How far under a transmitter its own amplifier's products may reach,
/// dB (see `look`): measured about 30 under, so 22 leaves room.
const IM_DB: f32 = 22.0;
/// A transmitter this much weaker than one within two combs of it may be
/// that one's products (see `look`): its confidence is at most SUSPECT.
const NEAR_DB: f32 = 25.0;
/// A quarter this many times the median of a group's four is a spur's.
const OUTLIER: f32 = 4.0;
const SUSPECT: f32 = 0.3;
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
    margin: (f32, u8, u8),
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
            margin: (0.0, 0, 0),
        }
    }

    /// Forget everything: a retune, or no signal at all.
    pub fn clear(&mut self) {
        *self = Self::new();
    }

    /// Start the averages again, keeping the last list until the next
    /// look replaces it: a new lock or a gap in the samples, after which
    /// the frame parity may differ. A host that falls behind drops
    /// samples now and then, and a list that blanked at every gap would
    /// flicker in the app for the 3 s each restart takes.
    pub fn restart(&mut self) {
        let heard = std::mem::take(&mut self.heard);
        *self = Self::new();
        self.heard = heard;
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
        // The first look as soon as each parity has MIN_FRAMES (1.5 s
        // after a lock), so a list shows soon and a signal that makes the
        // demodulator lock again now and then still gets one; then every
        // EVERY frames.
        let first = self.count == 2 * MIN_FRAMES as u64;
        if (first || self.count % EVERY == 0) && self.frames.iter().all(|&n| n >= MIN_FRAMES) {
            self.look();
        }
    }

    /// For measuring on recordings: each pair's complex average (by
    /// quarter, comb, group) in the TII parity and in the empty one, and
    /// the signal's power per carrier. None before both have frames.
    pub fn probe(&self) -> Option<(Vec<Complex32>, Vec<Complex32>, f32)> {
        if self.frames.iter().any(|&n| n == 0) {
            return None;
        }
        let g = [self.groups(0), self.groups(1)];
        let on = if g[0].iter().sum::<f32>() >= g[1].iter().sum::<f32>() { 0 } else { 1 };
        Some((self.pairs[on].clone(), self.pairs[1 - on].clone(), self.prs_power - self.null_power[1 - on]))
    }

    /// Each group's figure for one parity: the sum of its four quarters'
    /// magnitudes, but with one quarter far over the others (over
    /// OUTLIER times their median) left out and the other three scaled
    /// to four. On real receivers the "noise" in a null is not white: a
    /// spur or an interferer near one carrier pair lifts that pair alone,
    /// in one quarter, 10-15 dB over the rest (ohs-box, 23/10/2026: up to
    /// 4.8 times the median group with all four summed, where white noise
    /// stays under 1.8). A transmitter is in all four quarters. Always
    /// leaving out the largest was tried, and cost two transmitters
    /// sharing a comb their told-apart: where both send, the quarters
    /// differ with the two delays, and the largest is theirs.
    fn groups(&self, par: usize) -> Vec<f32> {
        let mut g = vec![0.0; COMBS * GROUPS];
        for c in 0..COMBS {
            for b in 0..GROUPS {
                let m: [f32; 4] = std::array::from_fn(|q| self.pairs[par][pair_index(q, c, b)].norm());
                let mut sorted = m;
                sorted.sort_by(f32::total_cmp);
                let mid = 0.5 * (sorted[1] + sorted[2]);
                g[c * GROUPS + b] = if sorted[3] > OUTLIER * mid { (sorted[0] + sorted[1] + sorted[2]) * 4.0 / 3.0 } else { sorted.iter().sum() };
            }
        }
        g
    }

    /// The nearest miss since the last restart: the best a comb with
    /// nothing listed came to (its best pattern's weakest group over the
    /// noise median, against THRESHOLD), and that pattern and comb. For
    /// measuring on recordings: on ohs-box's nine blocks it was a real
    /// transmitter just too weak every time, up to 2.19.
    pub fn nearest_miss(&self) -> (f32, u8, u8) {
        self.margin
    }

    fn look(&mut self) {
        let g = [self.groups(0), self.groups(1)];
        // The parity carrying TII has the larger groups; the other is noise.
        let total = |x: &[f32]| x.iter().sum::<f32>();
        let on = if total(&g[0]) >= total(&g[1]) { 0 } else { 1 };
        let mut quiet = g[1 - on].clone();
        quiet.sort_by(f32::total_cmp);
        let g = &g[on];
        // The signal's power per carrier, less the noise the empty null
        // shows; a transmitter's pair figure is its own power per carrier.
        let signal = (self.prs_power - self.null_power[1 - on]).max(1e-30);
        // A noiseless signal (a simulator's) leaves the empty null at
        // rounding error, under which every leak of a transmitter's
        // carriers into its neighbours' stands out: noise is taken as at
        // least FLOOR_DB under a 0 dB transmitter's group (four pairs).
        let noise = quiet[quiet.len() / 2].max(4.0 * signal * 10f32.powf(FLOOR_DB / 10.0)).max(1e-30);
        let im = 10f32.powf(-IM_DB / 10.0);
        let level = |figure: f32| 10.0 * ((figure - noise).max(1e-30) / 4.0 / signal).log10();
        let z_of = |c: usize| -> Vec<f32> { (0..GROUPS).map(|b| g[c * GROUPS + b] / noise).collect() };
        let weakest_in = |z: &[f32], mask: u8| (0..GROUPS).filter(|b| mask >> b & 1 == 1).map(|b| z[b]).fold(f32::INFINITY, f32::min);
        let best_of = |z: &[f32]| (0..PATTERNS as u8).filter_map(|p| pattern(p).map(|m| (p, m))).max_by(|a, b| weakest_in(z, a.1).total_cmp(&weakest_in(z, b.1))).unwrap();
        // A transmitter's amplifier mixes its own 32 carriers, and the
        // products land on its comb's other groups and, in its own
        // pattern, on the combs beside it, about 30 dB under it (Brown
        // Carrick Hill's 64/14 on 11B and 12B, ohs-box 23/10/2026: comb
        // 14's other groups and groups 0, 1, 3, 4 of combs 13 and 15 at
        // -28 to -43 dB, in the TII frames only; combs 12 and 16 clean).
        // Read as transmitters, that was 5/14, 64/13 and 64/15. So each
        // comb first learns what its loud neighbours could have put on it:
        // their pattern, up to IM_DB under them.
        let loud: Vec<(usize, u8, f32)> = (0..COMBS)
            .map(|c| {
                let z = z_of(c);
                let (_, m) = best_of(&z);
                (c, m, weakest_in(&z, m))
            })
            .filter(|&(_, _, w)| w * im >= THRESHOLD)
            .collect();
        let mut heard: Vec<Heard> = Vec::new();
        let mut margin = (0f32, 0u8, 0u8);
        for c in 0..COMBS {
            let z = z_of(c);
            let weakest = |mask: u8| weakest_in(&z, mask);
            let (p, m) = best_of(&z);
            let strong: u8 = (0..GROUPS).filter(|&b| z[b] >= THRESHOLD).fold(0, |m, b| m | 1 << b);
            if strong.count_ones() < 4 {
                if weakest(m) > margin.0 {
                    margin = (weakest(m), p, c as u8);
                }
                continue;
            }
            // The louder neighbour, in the comb either side, whose products
            // could reach this comb: (its pattern, the most they can read
            // here). Not two combs off: products were never seen there, and
            // a network's transmitters often share one main identifier, so
            // a real one two combs from a loud one has its pattern too
            // (Sergeant Law's 63/3 beside Darvel's 63/5 on 11C, 26 dB under).
            let near = loud
                .iter()
                .filter(|&&(c2, _, _)| c2.abs_diff(c) == 1)
                .max_by(|a, b| a.2.total_cmp(&b.2))
                .map(|&(_, m2, w2)| (m2, w2 * im));
            if let Some((m2, cap)) = near {
                if strong & !m2 == 0 && weakest(strong) <= cap {
                    continue; // only a neighbour's products
                }
            }
            // The other strong groups, if any, are a second transmitter
            // only when they are not far under this one: in its own comb a
            // transmitter's products are on the groups it does not send.
            let rest = strong & !m;
            let rest_max = (0..GROUPS).filter(|b| rest >> b & 1 == 1).map(|b| z[b]).fold(0.0, f32::max);
            if rest == 0 || rest_max < weakest(m) * im {
                if near.is_some_and(|(m2, cap)| m2 == m && weakest(m) <= cap) {
                    continue; // the neighbour's products in its own pattern
                }
                let confidence = ((weakest(m) - THRESHOLD) / (SURE - THRESHOLD)).clamp(0.0, 1.0);
                let fig: f32 = (0..GROUPS).filter(|b| m >> b & 1 == 1).map(|b| g[c * GROUPS + b]).sum::<f32>() / 4.0;
                heard.push(Heard { main: p, sub: c as u8, level_db: level(fig), confidence });
                continue;
            }
            for (p, l, fitted) in explain(&z, strong, near) {
                let confidence = ((1.0 + l - THRESHOLD) / (SURE - THRESHOLD)).clamp(0.0, 1.0);
                // Which four groups are whose is a fit, not a reading.
                let confidence = if fitted { 0.7 * confidence } else { confidence };
                heard.push(Heard { main: p, sub: c as u8, level_db: level(l * noise + noise), confidence });
            }
        }
        // What remains beside a much louder transmitter may still be its
        // products: they fall on particular groups of the combs beside it
        // (third-order mixing of its four groups), not evenly, so a
        // pattern can stand out of them. Measured 28-44 dB under the loud
        // one (ohs-box: 2/9 beside Darvel's 64/10 on 12A, 22/15 beside
        // Brown Carrick Hill's 64/14) - but so is a real weak transmitter
        // beside it (Rothesay's 57/13, 30 dB under 64/14 on 12B). Nothing
        // in the null tells them apart, so such a one is listed with its
        // confidence at most SUSPECT, for the app to weigh against what it
        // knows of the network.
        let all = heard.clone();
        for h in heard.iter_mut() {
            if all.iter().any(|o| o.sub.abs_diff(h.sub) <= 2 && o.sub != h.sub && o.level_db - h.level_db >= NEAR_DB) {
                h.confidence = h.confidence.min(SUSPECT);
            }
        }
        heard.sort_by(|a, b| b.level_db.total_cmp(&a.level_db));
        heard.truncate(MAX_LISTED);
        self.heard = heard;
        if margin.0 > self.margin.0 {
            self.margin = margin;
        }
    }
}

/// Least squares with levels never under zero: `need` components (a
/// mask of groups each) that must be there with a level of their own, and
/// `may` components that may be there or not (a neighbour's products);
/// each subset of `may` is tried and the closest feasible fit wins. The
/// levels (of `need` then `may`, zero where left out; z over noise, less
/// noise's 1) and the residual.
fn fit_nonneg(z: &[f32], need: &[u8], may: &[u8]) -> Option<(Vec<f32>, f32)> {
    let mut best: Option<(Vec<f32>, f32)> = None;
    for set in 0u32..(1 << may.len()) {
        let used: Vec<usize> = (0..may.len()).filter(|i| set >> i & 1 == 1).collect();
        let comps: Vec<u8> = need.iter().copied().chain(used.iter().map(|&i| may[i])).collect();
        let Some((l, res)) = fit(z, &comps) else { continue };
        if best.as_ref().is_none_or(|b| res < b.1) {
            let mut all = l[..need.len()].to_vec();
            all.extend((0..may.len()).map(|i| used.iter().position(|&u| u == i).map(|k| l[need.len() + k]).unwrap_or(0.0)));
            best = Some((all, res));
        }
    }
    best
}

/// Least squares: the levels (z over noise, less noise's 1) of each
/// component (a mask of groups) that best make the comb's eight figures,
/// and the residual; None if a level comes out under zero or the
/// components cannot be told apart.
fn fit(z: &[f32], comps: &[u8]) -> Option<(Vec<f32>, f32)> {
    let n = comps.len();
    let ind = |m: u8, b: usize| (m >> b & 1) as f64;
    let mut a = vec![vec![0f64; n + 1]; n];
    for i in 0..n {
        for j in 0..n {
            a[i][j] = (0..GROUPS).map(|b| ind(comps[i], b) * ind(comps[j], b)).sum();
        }
        a[i][n] = (0..GROUPS).map(|b| ind(comps[i], b) * (z[b] as f64 - 1.0)).sum();
    }
    for col in 0..n {
        let piv = (col..n).max_by(|&x, &y| a[x][col].abs().total_cmp(&a[y][col].abs()))?;
        if a[piv][col].abs() < 1e-9 {
            return None;
        }
        a.swap(col, piv);
        for r in 0..n {
            if r != col {
                let f = a[r][col] / a[col][col];
                for k in col..=n {
                    a[r][k] -= f * a[col][k];
                }
            }
        }
    }
    let l: Vec<f32> = (0..n).map(|i| (a[i][n] / a[i][i]) as f32).collect();
    if l.iter().any(|&v| v < 0.0) {
        return None;
    }
    let res = (0..GROUPS)
        .map(|b| {
            let model: f32 = comps.iter().zip(&l).map(|(&m, &v)| v * ind(m, b) as f32).sum();
            (z[b] - 1.0 - model).powi(2)
        })
        .sum();
    Some((l, res))
}

/// What a comb with more than one thing on it holds: (main, level, fitted).
///
/// Two transmitters can share a comb (two regions' with one
/// sub-identifier), or one can sit on a loud neighbour's products
/// (Rothesay's 57/13 beside Brown Carrick Hill's 64/14 on 12B, ohs-box):
/// their patterns usually share groups. Each explanation is fitted to the
/// comb's eight figures by least squares, with `near`'s products (if any)
/// as two more components - its pattern, and its other groups lower - that
/// may never be fitted louder than `near` allows and are never listed. One
/// transmitter is the answer unless a second one makes the fit at least
/// four times closer: a group both send reads about the sum, one only
/// sends reads its own, so the right pair fits far better than one
/// transmitter does, while products and noise give a second pattern
/// little to explain.
fn explain(z: &[f32], strong: u8, near: Option<(u8, f32)>) -> Vec<(u8, f32, bool)> {
    let extra: Vec<u8> = near.map(|(m2, _)| vec![m2, !m2]).unwrap_or_default();
    let cap = near.map(|n| n.1).unwrap_or(f32::INFINITY);
    let within: Vec<(u8, u8)> = (0..PATTERNS as u8).filter_map(|p| pattern(p).map(|m| (p, m))).filter(|&(_, m)| m & strong == m).collect();
    let covers = |m: u8| m | if near.is_some() { 0xff } else { 0 };
    let ok = |l: &[f32], k: usize| l[k..].iter().all(|&v| v <= cap);
    let mut one: Option<(f32, u8, f32)> = None;
    for &(p, m) in &within {
        if near.is_none() && covers(m) & strong != strong {
            continue;
        }
        if let Some((l, res)) = fit_nonneg(z, &[m], &extra) {
            if ok(&l, 1) && one.as_ref().is_none_or(|o| res < o.0) {
                one = Some((res, p, l[0]));
            }
        }
    }
    let mut two: Option<(f32, u8, u8, f32, f32)> = None;
    for (i, &(p1, m1)) in within.iter().enumerate() {
        for &(p2, m2) in &within[i + 1..] {
            if near.is_none() && m1 | m2 != strong {
                continue;
            }
            if let Some((l, res)) = fit_nonneg(z, &[m1, m2], &extra) {
                if ok(&l, 2) && l[0] >= THRESHOLD - 1.0 && l[1] >= THRESHOLD - 1.0 && two.as_ref().is_none_or(|t| res < t.0) {
                    two = Some((res, p1, p2, l[0], l[1]));
                }
            }
        }
    }
    let mut out = match (one, two) {
        (Some(o), Some(t)) if t.0 * 4.0 > o.0 => vec![(o.1, o.2, near.is_some())],
        (_, Some(t)) => vec![(t.1, t.3, true), (t.2, t.4, true)],
        (Some(o), None) => vec![(o.1, o.2, near.is_some())],
        (None, None) => vec![],
    };
    // A listed pattern that is the neighbour's own, no louder than its
    // products can be, is its products.
    if let Some((m2, cap)) = near {
        out.retain(|&(p, l, _)| !(pattern(p) == Some(m2) && l + 1.0 <= cap));
    }
    out.retain(|&(_, l, _)| l + 1.0 >= THRESHOLD);
    out
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
