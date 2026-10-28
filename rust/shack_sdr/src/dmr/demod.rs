//! 4FSK to symbols: the channel at 48 kS/s (ten samples a symbol), an FM
//! discriminator, the receive half of the root-raised-cosine pair, and a
//! framer that finds bursts by their sync and then follows the 30 ms slot
//! clock through bursts with no sync (voice B-F).
//!
//! **Finding a sync.** The four voice syncs (repeater, radio, direct slot 1
//! and 2) are correlated against the discriminator's output every sample,
//! after taking out the mean: a carrier off frequency is a DC offset there,
//! which must not decide. A data sync is a voice sync with every symbol
//! negated, so the sign of the same correlation finds it. Over 0.8 (of a
//! perfect match) starts a burst; while locked only the expected place, a
//! few samples either side, is looked at, so a voice burst's random bits
//! cannot pass for a sync.
//!
//! **Levels.** Each sync is a known run of +-3 symbols: a least-squares fit
//! of the samples to them gives the gain and the DC (the carrier's offset)
//! for that burst. Bursts without a sync refit both on their own decided
//! symbols, gently, so a slow drift is followed.

use super::burst::{sync_symbols, SyncKind, BURST_SYMBOLS, CACH_SYMBOLS, SLOT_SYMBOLS, SYNCS, SYNC_SYMBOL, SYNC_SYMBOLS};
use crate::dsp::{lowpass, Decimator, Resampler};
use rustfft::num_complex::Complex32;
use std::collections::VecDeque;

pub const RATE: f64 = 48_000.0;
pub const SPS: usize = 10;
pub const BAUD: f64 = 4_800.0;
/// Hz of deviation per symbol unit: +3 is +1944 Hz.
pub const HZ_PER_UNIT: f64 = 648.0;
/// The roll-off of DMR's root-raised-cosine pulse.
pub const ROLLOFF: f64 = 0.2;
/// Symbols each side the RRC filters span.
const RRC_SPAN: usize = 4;

const SEARCH_THRESHOLD: f32 = 0.80;
const LOCKED_THRESHOLD: f32 = 0.70;
/// How far, in samples, a locked framer looks either side of where a sync
/// should be.
const LOCKED_WINDOW: i64 = 4;
/// Bursts in a row with nothing recognisable before the framer searches
/// again (12: 360 ms, three voice superframes' worth of one slot).
pub const MAX_MISSES: u32 = 12;

/// A root-raised-cosine pulse at `sps` samples a symbol, `span` symbols
/// each side, unit sum.
pub fn rrc(sps: usize, span: usize, beta: f64) -> Vec<f32> {
    let n = 2 * span * sps + 1;
    let m = (span * sps) as f64;
    let pi = std::f64::consts::PI;
    let mut h: Vec<f64> = (0..n)
        .map(|i| {
            let t = (i as f64 - m) / sps as f64;
            if t.abs() < 1e-9 {
                1.0 - beta + 4.0 * beta / pi
            } else if (t.abs() - 1.0 / (4.0 * beta)).abs() < 1e-9 {
                beta / 2f64.sqrt() * ((1.0 + 2.0 / pi) * (pi / (4.0 * beta)).sin() + (1.0 - 2.0 / pi) * (pi / (4.0 * beta)).cos())
            } else {
                ((pi * t * (1.0 - beta)).sin() + 4.0 * beta * t * (pi * t * (1.0 + beta)).cos())
                    / (pi * t * (1.0 - (4.0 * beta * t).powi(2)))
            }
        })
        .collect();
    let s: f64 = h.iter().sum();
    h.iter_mut().for_each(|x| *x /= s);
    h.into_iter().map(|x| x as f32).collect()
}

/// IQ at any rate to the RRC-filtered discriminator output at 48 kS/s, in
/// symbol units (+-3 at full deviation), kept with each sample's channel
/// power for the burst's signal level.
pub struct Demod {
    resampler: Option<Resampler<Complex32>>,
    chan: Decimator,
    rrc: Vec<f32>,
    rrc_hist: VecDeque<f32>,
    last: Complex32,
    /// Filtered discriminator samples; `base` is the 48 kS/s index of f[0].
    pub f: VecDeque<f32>,
    /// |IQ|^2 after the channel filter, aligned with `f`.
    pub pw: VecDeque<f32>,
    pub base: u64,
    /// 48 kS/s samples produced in all.
    pub produced: u64,
    scratch: Vec<Complex32>,
    chan_out: Vec<Complex32>,
}

impl Demod {
    pub fn new(in_rate: f64) -> Self {
        let resampler = ((in_rate - RATE).abs() > 1e-6).then(|| Resampler::new(in_rate, RATE, 0.5, 24));
        let rrc = rrc(SPS, RRC_SPAN, ROLLOFF);
        let n = rrc.len();
        Demod {
            resampler,
            // The signal is within +-5 kHz of its carrier; 6.5 leaves room
            // for a carrier 1.5 kHz off and stops the adjacent channel.
            chan: Decimator::new(lowpass(63, 6_500.0, RATE), 1),
            rrc,
            rrc_hist: VecDeque::from(vec![0.0; n]),
            last: Complex32::new(0.0, 0.0),
            f: VecDeque::new(),
            pw: VecDeque::new(),
            base: 0,
            produced: 0,
            scratch: Vec::new(),
            chan_out: Vec::new(),
        }
    }

    pub fn process(&mut self, x: &[Complex32]) {
        self.scratch.clear();
        let input: &[Complex32] = match self.resampler.as_mut() {
            Some(r) => {
                r.process(x, &mut self.scratch);
                &self.scratch
            }
            None => x,
        };
        self.chan_out.clear();
        self.chan.process(input, &mut self.chan_out);
        let k = (RATE / (2.0 * std::f64::consts::PI) / HZ_PER_UNIT) as f32;
        for &z in &self.chan_out {
            let d = z * self.last.conj();
            self.last = z;
            let v = d.im.atan2(d.re) * k;
            self.rrc_hist.pop_front();
            self.rrc_hist.push_back(v);
            let y: f32 = self.rrc_hist.iter().zip(&self.rrc).map(|(a, b)| a * b).sum();
            self.f.push_back(y);
            self.pw.push_back(z.norm_sqr());
        }
        self.produced += self.chan_out.len() as u64;
    }

    /// The sample at absolute index `i` (fractional: linear between two).
    pub fn at(&self, i: f64) -> Option<f32> {
        let r = i - self.base as f64;
        if r < 0.0 {
            return None;
        }
        let k = r.floor() as usize;
        let a = (r - k as f64) as f32;
        let x0 = *self.f.get(k)?;
        let x1 = self.f.get(k + 1).copied().unwrap_or(x0);
        Some(x0 + a * (x1 - x0))
    }

    /// The end of what has arrived (exclusive).
    pub fn end(&self) -> u64 {
        self.base + self.f.len() as u64
    }

    /// Forget samples before `i`.
    pub fn trim(&mut self, i: u64) {
        while self.base < i && !self.f.is_empty() {
            self.f.pop_front();
            self.pw.pop_front();
            self.base += 1;
        }
    }

    pub fn reset(&mut self) {
        if let Some(r) = self.resampler.as_mut() {
            r.reset();
        }
        self.chan.reset();
        self.rrc_hist.iter_mut().for_each(|x| *x = 0.0);
        self.last = Complex32::new(0.0, 0.0);
        self.base = self.produced;
        self.f.clear();
        self.pw.clear();
    }
}

/// One burst as the framer found it.
#[derive(Clone, Debug)]
pub struct RawBurst {
    /// 48 kS/s index of the burst's first symbol (fractional).
    pub p0: f64,
    /// The CACH's 12 symbols then the burst's 132, in symbol units.
    pub symbols: [f32; SLOT_SYMBOLS],
    /// The sync found in it: kind, voice (true) or data, match (0-1).
    pub sync: Option<(SyncKind, bool, f32)>,
    /// Channel power over the burst, dB relative to full scale.
    pub rssi_db: f32,
    /// Mean squared distance of the burst's symbols from the nearest
    /// symbol: the eye's opening, for the SNR.
    pub mse: f32,
}

#[derive(Clone, Copy, Debug)]
struct Levels {
    gain: f32,
    dc: f32,
}

/// Finds bursts and follows the slot clock.
pub struct Framer {
    patterns: Vec<(SyncKind, [f32; SYNC_SYMBOLS], f32)>,
    /// Locked: where the next burst's first symbol is expected.
    next: Option<f64>,
    /// Searching: the next sample index to try as a sync's last symbol.
    search_from: u64,
    pub misses: u32,
    levels: Levels,
}

impl Default for Framer {
    fn default() -> Self {
        Self::new()
    }
}

/// The offset from a burst's first symbol to its sync's last symbol.
const SYNC_END: f64 = ((SYNC_SYMBOL + SYNC_SYMBOLS - 1) * SPS) as f64;

impl Framer {
    pub fn new() -> Self {
        let patterns = SYNCS
            .iter()
            .map(|&(k, p)| {
                let s = sync_symbols(p);
                let mean = s.iter().sum::<f32>() / SYNC_SYMBOLS as f32;
                let mut c = [0f32; SYNC_SYMBOLS];
                for i in 0..SYNC_SYMBOLS {
                    c[i] = s[i] - mean;
                }
                let norm = c.iter().map(|x| x * x).sum::<f32>().sqrt();
                (k, c, norm)
            })
            .collect();
        Framer { patterns, next: None, search_from: 0, misses: 0, levels: Levels { gain: 1.0, dc: 0.0 } }
    }

    pub fn locked(&self) -> bool {
        self.next.is_some()
    }

    pub fn offset_units(&self) -> f32 {
        self.levels.dc
    }

    pub fn reset(&mut self, from: u64) {
        self.next = None;
        self.search_from = from;
        self.misses = 0;
    }

    /// The best sync match with its last symbol at sample `end`: (pattern,
    /// signed normalised correlation).
    fn score(&self, d: &Demod, end: f64) -> Option<(usize, f32)> {
        let mut v = [0f32; SYNC_SYMBOLS];
        for (k, x) in v.iter_mut().enumerate() {
            *x = d.at(end - ((SYNC_SYMBOLS - 1 - k) * SPS) as f64)?;
        }
        let mean = v.iter().sum::<f32>() / SYNC_SYMBOLS as f32;
        let var: f32 = v.iter().map(|x| (x - mean) * (x - mean)).sum();
        if var <= 1e-9 {
            return Some((0, 0.0));
        }
        let sd = var.sqrt();
        let mut best = (0usize, 0f32);
        for (i, (_, c, norm)) in self.patterns.iter().enumerate() {
            let r = c.iter().zip(&v).map(|(a, b)| a * b).sum::<f32>() / (norm * sd);
            if r.abs() > best.1.abs() {
                best = (i, r);
            }
        }
        Some(best)
    }

    /// The least-squares gain and DC of the sync at `end`.
    fn fit(&self, d: &Demod, end: f64, pattern: usize, voice: bool) -> Option<Levels> {
        let s = sync_symbols(SYNCS[pattern].1);
        let sign = if voice { 1.0 } else { -1.0 };
        let (mut sx, mut sy, mut sxx, mut sxy) = (0f32, 0f32, 0f32, 0f32);
        for (k, &sk) in s.iter().enumerate() {
            let x = sign * sk;
            let y = d.at(end - ((SYNC_SYMBOLS - 1 - k) * SPS) as f64)?;
            sx += x;
            sy += y;
            sxx += x * x;
            sxy += x * y;
        }
        let n = SYNC_SYMBOLS as f32;
        let gain = (n * sxy - sx * sy) / (n * sxx - sx * sx);
        (gain > 0.05).then(|| Levels { gain, dc: (sy - gain * sx) / n })
    }

    /// Bursts complete in what `d` holds, in order.
    pub fn run(&mut self, d: &mut Demod) -> Vec<RawBurst> {
        let mut out = Vec::new();
        loop {
            match self.next {
                Some(p0) => {
                    // Room for the burst, and for a sync a few samples late.
                    let need = p0 + ((BURST_SYMBOLS - 1) * SPS) as f64 + LOCKED_WINDOW as f64 + 2.0;
                    if (d.end() as f64) < need {
                        break;
                    }
                    let b = self.burst_at(d, p0, true);
                    let p = b.p0;
                    out.push(b);
                    self.next = Some(p + (SLOT_SYMBOLS * SPS) as f64);
                }
                None => {
                    if !self.search(d, &mut out) {
                        break;
                    }
                }
            }
        }
        // Keep what the next burst, its CACH and a search window need.
        let keep = match self.next {
            Some(p0) => (p0 as u64).saturating_sub((CACH_SYMBOLS * SPS) as u64 + 64),
            None => self.search_from.saturating_sub((SYNC_END as u64) + (CACH_SYMBOLS * SPS) as u64 + 64),
        };
        d.trim(keep);
        out
    }

    /// Search for a sync; true if one was found (the framer is then locked).
    fn search(&mut self, d: &Demod, out: &mut Vec<RawBurst>) -> bool {
        let first = self.search_from.max(d.base + ((SYNC_SYMBOLS - 1) * SPS) as u64);
        let mut n = first;
        while n + 6 < d.end() {
            let Some((_, r)) = self.score(d, n as f64) else {
                n += 1;
                continue;
            };
            if r.abs() >= SEARCH_THRESHOLD {
                // The peak within the next few samples.
                let mut best = (n, r.abs());
                for m in n + 1..n + 6 {
                    if let Some((_, q)) = self.score(d, m as f64) {
                        if q.abs() > best.1 {
                            best = (m, q.abs());
                        }
                    }
                }
                let p0 = best.0 as f64 - SYNC_END;
                // The burst needs all its samples; wait for them.
                if (d.end() as f64) < p0 + ((BURST_SYMBOLS - 1) * SPS) as f64 + LOCKED_WINDOW as f64 + 2.0 {
                    self.search_from = n;
                    return false;
                }
                let b = self.burst_at(d, p0, false);
                let p = b.p0;
                out.push(b);
                self.next = Some(p + (SLOT_SYMBOLS * SPS) as f64);
                self.misses = 0;
                return true;
            }
            n += 1;
        }
        self.search_from = n;
        false
    }

    /// Read the burst whose first symbol is near `p0`: look for its sync a
    /// few samples either side (when locked), set the levels, slice.
    fn burst_at(&mut self, d: &Demod, p0: f64, locked: bool) -> RawBurst {
        let end0 = p0 + SYNC_END;
        let window = if locked { LOCKED_WINDOW } else { 1 };
        let mut best: Option<(f64, usize, f32)> = None;
        let mut scores = Vec::new();
        for o in -window..=window {
            let s = self.score(d, end0 + o as f64);
            scores.push(s.map(|x| x.1.abs()).unwrap_or(0.0));
            if let Some((i, r)) = s {
                if best.is_none_or(|b| r.abs() > b.2.abs()) {
                    best = Some((o as f64, i, r));
                }
            }
        }
        let threshold = if locked { LOCKED_THRESHOLD } else { SEARCH_THRESHOLD - 0.05 };
        let mut sync = None;
        let mut p0 = p0;
        if let Some((o, i, r)) = best.filter(|b| b.2.abs() >= threshold) {
            // A parabola through the peak and its neighbours places it
            // between samples.
            let k = (o as i64 + window) as usize;
            let frac = if k > 0 && k + 1 < scores.len() {
                let (a, b, c) = (scores[k - 1], scores[k], scores[k + 1]);
                let den = a - 2.0 * b + c;
                if den.abs() > 1e-6 { (0.5 * (a - c) / den).clamp(-0.5, 0.5) as f64 } else { 0.0 }
            } else {
                0.0
            };
            p0 += o + frac;
            let voice = r > 0.0;
            if let Some(l) = self.fit(d, p0 + SYNC_END, i, voice) {
                self.levels = l;
            }
            sync = Some((SYNCS[i].0, voice, r.abs()));
        }
        let start = p0 - (CACH_SYMBOLS * SPS) as f64;
        let mut raw = [0f32; SLOT_SYMBOLS];
        for (k, v) in raw.iter_mut().enumerate() {
            *v = d.at(start + (k * SPS) as f64).unwrap_or(self.levels.dc);
        }
        let Levels { gain, dc } = self.levels;
        let mut symbols = [0f32; SLOT_SYMBOLS];
        for (y, &v) in symbols.iter_mut().zip(&raw) {
            *y = (v - dc) / gain;
        }
        // The eye, and (without a sync) a gentle refit on decided symbols.
        let (mut se, mut sx, mut sy, mut sxx, mut sxy) = (0f32, 0f32, 0f32, 0f32, 0f32);
        let burst = &symbols[CACH_SYMBOLS..];
        for (k, &y) in burst.iter().enumerate() {
            let s = nearest(y);
            se += (y - s) * (y - s);
            let v = raw[CACH_SYMBOLS + k];
            sx += s;
            sy += v;
            sxx += s * s;
            sxy += s * v;
        }
        let n = BURST_SYMBOLS as f32;
        let mse = se / n;
        if sync.is_none() && mse < 0.5 {
            let g = (n * sxy - sx * sy) / (n * sxx - sx * sx);
            if g > 0.05 {
                let dcn = (sy - g * sx) / n;
                self.levels.gain += 0.2 * (g - self.levels.gain);
                self.levels.dc += 0.2 * (dcn - self.levels.dc);
            }
        }
        let i0 = (p0.max(d.base as f64) as u64 - d.base) as usize;
        let i1 = (i0 + BURST_SYMBOLS * SPS).min(d.pw.len());
        let p: f32 = d.pw.range(i0.min(i1)..i1).sum::<f32>() / (i1 - i0.min(i1)).max(1) as f32;
        RawBurst { p0, symbols, sync, rssi_db: 10.0 * p.max(1e-20).log10(), mse }
    }
}

pub fn nearest(y: f32) -> f32 {
    if y >= 2.0 {
        3.0
    } else if y >= 0.0 {
        1.0
    } else if y >= -2.0 {
        -1.0
    } else {
        -3.0
    }
}
