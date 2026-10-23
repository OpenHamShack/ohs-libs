//! Mode I OFDM reception (EN 300 401 §14), our own design:
//!
//! 1. **Find the null symbol.** The quietest 2560 samples of a frame's span
//!    (energy in blocks of 64): DAB's frame begins with 1.3 ms of silence.
//!    If it is not markedly quieter than the rest, there is no DAB here.
//! 2. **Fractional frequency** from the guard intervals: each is a copy of
//!    its symbol's last 504 samples, so r[n] conj(r[n + 2048]) summed over a
//!    frame has the angle -2 pi f Tu, whatever the timing (the data parts
//!    average away). That gives f modulo 1 kHz, the carrier spacing.
//! 3. **Integer frequency and timing** from the phase reference symbol: its
//!    spectrum times the known one's conjugate, for each whole-carrier shift
//!    m, inverse-transformed, is the channel's impulse response when m is
//!    right and noise when not. The best m is the offset in kHz; the
//!    earliest strong path in that response fixes where the useful part of
//!    every symbol begins, so a weaker echo arriving first is not cut off.
//! 4. **Each frame**, symbols are cut at their timing (a little early into
//!    the guard), de-rotated by the frequency estimate, transformed, and
//!    differentially demodulated against the previous symbol, carrier by
//!    carrier, then frequency de-interleaved into soft bits. The phase
//!    reference of each frame re-measures timing (a loop that also learns
//!    the sample clock's error) and the guards re-measure frequency.
//!
//! Only the symbols asked for are transformed: the FIC's always, the MSC's
//! that carry the chosen sub-channel. Prior art read: welle.io's
//! `ofdm-processor.cpp` (GPL-2.0-or-later) does steps 1-3 differently
//! (a fixed-threshold null detector and a time-domain correlation); none of
//! its code is used.

use super::coding::Soft;
use super::tables::{self, CIF_SYMBOLS, GUARD, K, L, MSC_FIRST, RATE, SYMBOL_BITS, TF, TNULL, TS, TU};
use rustfft::num_complex::{Complex32, Complex64};
use rustfft::{Fft, FftPlanner};
use std::sync::Arc;

/// How far into the guard interval the FFT window starts, in samples: room
/// for the timing estimate's error and a clock drifting within a frame.
const ADVANCE: usize = 32;
/// Integer frequency search: +-40 carriers (40 kHz; a receiver 150 ppm off
/// at 240 MHz is 36 kHz).
const MAX_SHIFT: i32 = 40;
/// The phase reference's correlation peak over its mean, for a first sync.
/// Noise alone, over 81 shifts of 2048 delays, reaches 12-15 now and then
/// (the largest of ~166 000 exponential draws), and once locked a false
/// sync to noise at 14 was seen; real phase references give hundreds even
/// at 3 dB SNR.
const PEAK_RATIO: f32 = 40.0;
/// Once locked (one shift, 2048 delays: noise's largest is about 8), the
/// phase reference counts as present above this.
const TRACK_RATIO: f32 = 16.0;

/// Why a span of samples gave no frame.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Miss {
    /// No null symbol: no DAB here.
    NoNull,
    /// A null, but no phase reference after it.
    NoPrs,
}

impl Miss {
    pub fn code(self) -> &'static str {
        match self {
            Miss::NoNull => "no_signal",
            Miss::NoPrs => "no_sync",
        }
    }
}

/// One frame's soft bits.
pub struct FrameOut {
    /// Absolute sample index (at 2.048 MS/s) of the frame's null symbol.
    pub start: f64,
    /// The FIC's 9216 soft bits (symbols 2-4).
    pub fic: Vec<Soft>,
    /// Each CIF's 55 296 soft bits, zero outside the symbols asked for.
    pub cifs: Vec<Vec<Soft>>,
    pub snr_db: f32,
    pub offset_hz: f64,
}

pub enum Event {
    Frame(FrameOut),
    Miss(Miss),
}

enum State {
    Search,
    Locked { start: f64, misses: u32 },
}

pub struct Demod {
    buf: Vec<Complex32>,
    /// Absolute index of buf[0].
    base: u64,
    state: State,
    /// Frequency offset estimate, Hz; and the sample clock's error (ratio).
    pub offset_hz: f64,
    pub clock: f64,
    fft: Arc<dyn Fft<f32>>,
    ifft: Arc<dyn Fft<f32>>,
    /// The phase reference's carriers, by k + 768.
    prs: Vec<Complex32>,
    interleave: Vec<i32>,
    scratch: Vec<Complex32>,
    pub frames: u64,
    pub resyncs: u64,
}

fn bin(k: i32) -> usize {
    (k + TU as i32) as usize % TU
}

impl Default for Demod {
    fn default() -> Self {
        Self::new()
    }
}

impl Demod {
    pub fn new() -> Self {
        let mut p = FftPlanner::new();
        let mut prs = vec![Complex32::new(0.0, 0.0); 2 * 768 + 1];
        for k in (-768i32..=768).filter(|&k| k != 0) {
            prs[(k + 768) as usize] = Complex32::from_polar(1.0, tables::prs_quarter_turns(k) as f32 * std::f32::consts::FRAC_PI_2);
        }
        Demod {
            buf: Vec::new(),
            base: 0,
            state: State::Search,
            offset_hz: 0.0,
            clock: 0.0,
            fft: p.plan_fft_forward(TU),
            ifft: p.plan_fft_inverse(TU),
            prs,
            interleave: tables::frequency_interleave(),
            scratch: vec![Complex32::new(0.0, 0.0); TU],
            frames: 0,
            resyncs: 0,
        }
    }

    /// Forget everything (a retune, a gap in the samples).
    pub fn reset(&mut self) {
        self.base += self.buf.len() as u64;
        self.buf.clear();
        self.state = State::Search;
        self.offset_hz = 0.0;
        self.clock = 0.0;
    }

    /// Search again from the samples now buffered (the decoder saw that a
    /// lock reads nothing).
    pub fn resync(&mut self) {
        self.state = State::Search;
    }

    pub fn locked(&self) -> bool {
        matches!(self.state, State::Locked { .. })
    }

    /// Samples at 2.048 MS/s.
    pub fn push(&mut self, x: &[Complex32]) {
        self.buf.extend_from_slice(x);
    }

    /// Absolute index of the next sample `push` takes.
    pub fn end(&self) -> u64 {
        self.base + self.buf.len() as u64
    }

    fn drop_before(&mut self, abs: u64) {
        if abs > self.base {
            let n = ((abs - self.base) as usize).min(self.buf.len());
            self.buf.drain(..n);
            self.base += n as u64;
        }
    }

    fn at(&self, abs: u64) -> Option<&[Complex32]> {
        let i = abs.checked_sub(self.base)? as usize;
        self.buf.get(i..)
    }

    /// The next frame, or why there was none in the span looked at; None
    /// when more samples are needed. `msc` says which MSC symbols (within
    /// a CIF, 0-17) to demodulate.
    pub fn next(&mut self, msc: &[bool; CIF_SYMBOLS]) -> Option<Event> {
        if let State::Search = self.state {
            // A search that locks returns nothing and goes on to the frame.
            if let Some(miss) = self.search() {
                return Some(miss);
            }
        }
        match self.state {
            State::Search => None,
            State::Locked { start, misses } => self.frame(start, misses, msc),
        }
    }

    /// Samples from `abs`, de-rotated by the frequency estimate, FFT'd into
    /// the carriers (by k + 768). `frac` is how far (in samples) `abs` is
    /// past where the window should really start: the window is cut at a
    /// whole sample, and the remainder is taken out as the phase ramp it
    /// puts across the carriers. Without that, a clock a few ppm off makes
    /// the rounding step by a whole sample between two symbols now and
    /// then, a ramp of 2.4 rad across the outer carriers of that pair.
    fn carriers(&mut self, abs: u64, frac: f64) -> Vec<Complex32> {
        let w = -2.0 * std::f64::consts::PI * self.offset_hz / RATE;
        let mut rot = Complex64::from_polar(1.0, (w * abs as f64).rem_euclid(2.0 * std::f64::consts::PI));
        let step = Complex64::from_polar(1.0, w);
        let i = (abs - self.base) as usize;
        for (j, s) in self.scratch.iter_mut().enumerate() {
            let r = Complex32::new(rot.re as f32, rot.im as f32);
            *s = self.buf[i + j] * r;
            rot *= step;
        }
        self.fft.process(&mut self.scratch);
        let mut out = vec![Complex32::new(0.0, 0.0); 2 * 768 + 1];
        for k in (-768i32..=768).filter(|&k| k != 0) {
            let ramp = Complex32::from_polar(1.0, (-2.0 * std::f64::consts::PI * k as f64 * frac / TU as f64) as f32);
            out[(k + 768) as usize] = self.scratch[bin(k)] * ramp;
        }
        out
    }

    /// The impulse response of the phase reference measured in `y`
    /// (carriers by k + 768, shifted by m carriers): |h|^2 by delay, and
    /// the peak over the mean.
    fn response(&mut self, y: &[Complex32], m: i32) -> (Vec<f32>, f32) {
        let mut z = vec![Complex32::new(0.0, 0.0); TU];
        for k in (-768i32..=768).filter(|&k| k != 0) {
            let km = k + m;
            if (-768..=768).contains(&km) && km != 0 {
                z[bin(k)] = y[(km + 768) as usize] * self.prs[(k + 768) as usize].conj();
            }
        }
        self.ifft.process(&mut z);
        let p: Vec<f32> = z.iter().map(|v| v.norm_sqr()).collect();
        let mean = p.iter().sum::<f32>() / TU as f32;
        let peak = p.iter().cloned().fold(0.0, f32::max);
        (p, peak / mean.max(1e-30))
    }

    /// The earliest strong path, as a delay (-1024..1024) from the window.
    fn first_path(p: &[f32]) -> i32 {
        let (ip, &peak) = p.iter().enumerate().max_by(|a, b| a.1.total_cmp(b.1)).unwrap();
        let signed = |i: usize| if i >= TU / 2 { i as i32 - TU as i32 } else { i as i32 };
        let np = signed(ip);
        // Up to 400 samples (195 us) earlier, the first above a tenth of it.
        for d in (0..400).rev() {
            let n = np - d;
            if p[(n + TU as i32) as usize % TU] > 0.1 * peak {
                return n;
            }
        }
        np
    }

    /// The guard intervals' frequency estimate over the symbols of a frame
    /// whose null starts at `start` (as many as the buffer holds, at least
    /// eight): the offset modulo 1 kHz, in -500..500.
    fn guard_offset(&self, start: f64) -> Option<f64> {
        let mut acc = Complex64::new(0.0, 0.0);
        let mut used = 0;
        for l in 1..=L {
            let s = (start + ((TNULL + (l - 1) * TS) as f64) * (1.0 + self.clock)).round() as u64;
            let Some(x) = self.at(s) else { break };
            if x.len() < TS {
                break;
            }
            used += 1;
            for j in 8..GUARD - 8 {
                let v = x[j] * x[j + TU].conj();
                acc += Complex64::new(v.re as f64, v.im as f64);
            }
        }
        (used >= 8).then(|| -acc.arg() * RATE / (2.0 * std::f64::consts::PI * TU as f64))
    }

    fn search(&mut self) -> Option<Event> {
        let need = TF + TNULL + 4 * TS;
        if self.buf.len() < need {
            return None;
        }
        // 1. The quietest 40 blocks of 64 in the first frame's span.
        let blocks: Vec<f32> = self.buf[..TF + TNULL].chunks(64).map(|c| c.iter().map(|v| v.norm_sqr()).sum()).collect();
        let total: f32 = blocks.iter().sum();
        let mean = total / blocks.len() as f32;
        let mut run: f32 = blocks[..40].iter().sum();
        let (mut best, mut at) = (run, 0usize);
        for b in 40..blocks.len() {
            run += blocks[b] - blocks[b - 40];
            if run < best {
                best = run;
                at = b - 39;
            }
        }
        if best / 40.0 > 0.6 * mean {
            // Keep the end: a null may straddle it.
            let keep = self.end().saturating_sub(TNULL as u64 + 64);
            self.drop_before(keep);
            return Some(Event::Miss(Miss::NoNull));
        }
        // The null's end is where the quiet ends: the first block after the
        // quietest window louder than halfway (in dB) between the quiet and
        // the mean. Not the window's own end: the quiet before a signal
        // starts (or a long fade) is no null, and only the edge into the
        // phase reference is sure. Then to the sample, within that block.
        let quiet = best / 40.0;
        let edge = (quiet * mean).sqrt();
        // Loud must last: the phase reference and data follow a null, while
        // one loud block in a stretch of noise is only noise.
        let sustained = |b: usize| blocks.get(b..b + 16).is_some_and(|w| w.iter().sum::<f32>() / 16.0 > edge);
        let Some(b_end) = (at..blocks.len()).find(|&b| blocks[b] > edge && sustained(b)) else {
            self.drop_before(self.base + (TF / 2) as u64);
            return Some(Event::Miss(Miss::NoNull));
        };
        let per = edge / 64.0;
        let off = self.buf[b_end * 64..b_end * 64 + 64]
            .windows(8)
            .position(|w| w.iter().map(|v| v.norm_sqr()).sum::<f32>() / 8.0 > per)
            .unwrap_or(0);
        let null_end = self.base + (b_end * 64 + off) as u64;
        // 2. Fractional frequency from the guards of the frame after it.
        let frac = self.guard_offset(null_end as f64 - TNULL as f64).unwrap_or(0.0);
        // 3. Integer frequency and timing from the phase reference.
        self.offset_hz = frac;
        let w0 = null_end + GUARD as u64 - 100;
        let y = self.carriers(w0, 0.0);
        let mut best = (0i32, 0f32, Vec::new());
        for m in -MAX_SHIFT..=MAX_SHIFT {
            let (p, ratio) = self.response(&y, m);
            if ratio > best.1 {
                best = (m, ratio, p);
            }
        }
        if best.1 < PEAK_RATIO {
            self.drop_before(null_end);
            return Some(Event::Miss(Miss::NoPrs));
        }
        self.offset_hz = frac + 1000.0 * best.0 as f64;
        let u = w0 as f64 + Self::first_path(&best.2) as f64;
        let start = u - (GUARD + TNULL) as f64;
        self.state = State::Locked { start, misses: 0 };
        self.resyncs += 1;
        None
    }

    fn frame(&mut self, start: f64, misses: u32, msc: &[bool; CIF_SYMBOLS]) -> Option<Event> {
        let scale = 1.0 + self.clock;
        // Symbol l's window: its useful part, ADVANCE samples early; where it
        // is cut, and how far past the exact position that is.
        let exact = |l: usize| -> f64 { start + ((TNULL + (l - 1) * TS + GUARD) as f64) * scale - ADVANCE as f64 };
        let sym_at = |l: usize| -> u64 { exact(l).round() as u64 };
        if start < self.base as f64 {
            // Before what the buffer holds (a timing estimate gone wrong):
            // search again, past the samples just tried.
            self.state = State::Search;
            self.drop_before(self.base + TNULL as u64);
            return Some(Event::Miss(Miss::NoPrs));
        }
        if sym_at(L) + (TU as u64) + (TS as u64) > self.end() {
            return None;
        }
        // Which symbols: the PRS and FIC always, and the asked MSC ones with
        // the symbol before each (the differential reference).
        let mut need = [false; L + 1];
        for n in need.iter_mut().take(MSC_FIRST).skip(1) {
            *n = true;
        }
        for c in 0..4 {
            for (j, &w) in msc.iter().enumerate() {
                if w {
                    let l = MSC_FIRST + c * CIF_SYMBOLS + j;
                    need[l] = true;
                    need[l - 1] = true;
                }
            }
        }
        let mut fic = Vec::with_capacity(3 * SYMBOL_BITS);
        let mut cifs = vec![vec![0 as Soft; tables::CIF_BITS]; 4];
        let mut prev: Option<Vec<Complex32>> = None;
        let mut prs_ratio = 0.0;
        let mut timing_err = 0.0;
        for l in 1..=L {
            if !need[l] {
                prev = None;
                continue;
            }
            let y = self.carriers(sym_at(l), sym_at(l) as f64 - exact(l));
            if l == 1 {
                let (p, ratio) = self.response(&y, 0);
                prs_ratio = ratio;
                timing_err = (Self::first_path(&p) - ADVANCE as i32) as f64;
            }
            if let Some(pv) = &prev {
                let soft = self.dqpsk(&y, pv);
                if l < MSC_FIRST {
                    fic.extend(soft);
                } else {
                    let c = (l - MSC_FIRST) / CIF_SYMBOLS;
                    let j = (l - MSC_FIRST) % CIF_SYMBOLS;
                    cifs[c][j * SYMBOL_BITS..(j + 1) * SYMBOL_BITS].copy_from_slice(&soft);
                }
            }
            prev = Some(y);
        }
        // SNR from the null's power against the symbols'.
        let p_of = |a: u64, n: usize| -> f64 {
            self.at(a).map(|x| x[..n].iter().map(|v| v.norm_sqr() as f64).sum::<f64>() / n as f64).unwrap_or(0.0)
        };
        let null = p_of(start.round() as u64 + 300, TNULL - 600);
        let sig = p_of(sym_at(2), TU);
        let snr_db = (10.0 * ((sig - null).max(1e-12) / (0.75 * null).max(1e-12)).log10()) as f32;
        // Track: frequency from the guards, timing (and the clock) from the
        // phase reference, while it is still clearly there.
        if let Some(f) = self.guard_offset(start) {
            let err = (f - self.offset_hz + 500.0).rem_euclid(1000.0) - 500.0;
            self.offset_hz += 0.5 * err;
        }
        let good = prs_ratio >= TRACK_RATIO;
        let (start, misses) = if good && timing_err.abs() < 300.0 {
            self.clock = (self.clock + 0.2 * timing_err / TF as f64).clamp(-2e-4, 2e-4);
            (start + timing_err * 0.5, 0)
        } else {
            (start, misses + 1)
        };
        let next = start + TF as f64 * (1.0 + self.clock);
        self.frames += 1;
        self.state = if misses >= 4 { State::Search } else { State::Locked { start: next, misses } };
        self.drop_before((next as u64).saturating_sub(2 * TS as u64));
        Some(Event::Frame(FrameOut { start, fic, cifs, snr_db, offset_hz: self.offset_hz }))
    }

    /// One symbol's differential demodulation and frequency de-interleaving:
    /// 3072 soft bits, n = 0..K the real parts and K..2K the imaginary.
    fn dqpsk(&self, y: &[Complex32], prev: &[Complex32]) -> Vec<Soft> {
        let d: Vec<Complex32> = y.iter().zip(prev).map(|(a, b)| a * b.conj()).collect();
        let mean = d.iter().map(|v| v.norm()).sum::<f32>() / K as f32;
        let g = 48.0 / mean.max(1e-30);
        let mut out = vec![0 as Soft; SYMBOL_BITS];
        for (n, &k) in self.interleave.iter().enumerate() {
            let v = d[(k + 768) as usize] * g;
            out[n] = v.re.round().clamp(-127.0, 127.0) as Soft;
            out[n + K] = v.im.round().clamp(-127.0, 127.0) as Soft;
        }
        out
    }
}
