//! The 1090 MHz test transmitter and its channel: Mode S replies and
//! squitters as a transponder keys them (Annex 10 Vol IV §3.1.2.2), and
//! the Mode A/C replies ("fruit") a real receiver hears far more of, at any
//! sample rate, at any sub-sample timing, with noise, a frequency error, a
//! receiver's IF filter and overlapping replies - so the decoder is
//! measured on what it will hear. Receive only: nothing here reaches a
//! radio; it makes numbers for tests and for Radio's simulator.
//!
//! **Mode S:** a preamble of four 0.5 us pulses at 0, 1.0, 3.5 and 4.5 us,
//! then from 8 us one bit a microsecond, pulse-position modulated: a 1 is a
//! pulse in the first half of its microsecond, a 0 in the second.
//!
//! **Mode A/C:** framing pulses F1 and F2, 20.3 us apart, and up to twelve
//! code pulses on the 1.45 us grid between them, each 0.45 us. They carry
//! no parity and look nothing like a Mode S preamble to a careful
//! detector, but their pulses fall where a careless one would see one.
//!
//! Each pulse is rectangular and integrated over each sample it covers, so
//! timing is exact at any rate; `if_bw_hz` then low-passes the whole
//! signal as a receiver's IF filter does (an RSP at 2.4 MS/s chooses 1.536
//! MHz), rounding the pulses as real ones arrive. `snr_db` is a 0 dB
//! pulse's power over the noise's in the whole sample bandwidth.

use crate::dsp::lowpass;
use crate::testsig::{db_to_ratio, sin_cos_turns, Rng};
use rustfft::num_complex::{Complex32, Complex64};

/// One reply on the air.
#[derive(Clone, Debug)]
pub struct Reply {
    /// Seconds from the scene's start to the first preamble (or F1) pulse.
    pub t: f64,
    pub kind: ReplyKind,
    pub level_db: f64,
    /// The transponder's frequency error (Annex 10 allows ±1 MHz).
    pub offset_hz: f64,
    /// Carrier phase at t, turns.
    pub phase: f64,
}

#[derive(Clone, Debug)]
pub enum ReplyKind {
    /// A Mode S message, its bytes as sent (7 or 14).
    ModeS(Vec<u8>),
    /// A Mode A/C reply's twelve code bits (which of the 1.45 us slots
    /// between the framing pulses carry a pulse, slot 7, X, never does).
    ModeAc(u16),
}

impl Reply {
    pub fn mode_s(t: f64, bytes: Vec<u8>) -> Self {
        Reply { t, kind: ReplyKind::ModeS(bytes), level_db: 0.0, offset_hz: 0.0, phase: 0.0 }
    }

    /// The pulses: (start, length) in seconds from the scene's start.
    pub fn pulses(&self) -> Vec<(f64, f64)> {
        let us = 1e-6;
        match &self.kind {
            ReplyKind::ModeS(b) => {
                let mut p: Vec<(f64, f64)> = [0.0, 1.0, 3.5, 4.5].iter().map(|s| (self.t + s * us, 0.5 * us)).collect();
                for k in 0..b.len() * 8 {
                    let one = (b[k / 8] >> (7 - k % 8)) & 1 == 1;
                    p.push((self.t + (8.0 + k as f64 + if one { 0.0 } else { 0.5 }) * us, 0.5 * us));
                }
                p
            }
            ReplyKind::ModeAc(code) => {
                let mut p = vec![(self.t, 0.45 * us), (self.t + 20.3 * us, 0.45 * us)];
                for slot in 0..13 {
                    if slot != 6 && code >> (12 - slot.min(12)) & 1 == 1 {
                        p.push((self.t + 1.45 * (slot + 1) as f64 * us, 0.45 * us));
                    }
                }
                p
            }
        }
    }

    /// How long it lasts, seconds.
    pub fn span(&self) -> f64 {
        match &self.kind {
            ReplyKind::ModeS(b) => (8.0 + b.len() as f64 * 8.0) * 1e-6,
            ReplyKind::ModeAc(_) => 20.75e-6,
        }
    }
}

/// A stretch of air.
#[derive(Clone, Debug)]
pub struct Scene {
    pub rate: f64,
    pub duration_s: f64,
    pub replies: Vec<Reply>,
    /// None: no noise at all.
    pub snr_db: Option<f64>,
    /// The receiver's IF bandwidth (two-sided), or None for none.
    pub if_bw_hz: Option<f64>,
    pub seed: u64,
}

/// The scene's complex IQ.
pub fn scene_iq(s: &Scene) -> Vec<Complex32> {
    let n = (s.duration_s * s.rate).round() as usize;
    let mut x = vec![Complex64::new(0.0, 0.0); n];
    let dt = 1.0 / s.rate;
    for r in &s.replies {
        let amp = db_to_ratio(r.level_db).sqrt();
        for (start, len) in r.pulses() {
            let end = start + len;
            let i0 = (start / dt).floor().max(0.0) as usize;
            let i1 = ((end / dt).ceil() as usize).min(n);
            for (i, v) in x.iter_mut().enumerate().take(i1).skip(i0) {
                let (a, b) = (i as f64 * dt, (i + 1) as f64 * dt);
                let cover = (b.min(end) - a.max(start)).max(0.0) / dt;
                if cover <= 0.0 {
                    continue;
                }
                let (si, co) = sin_cos_turns(r.phase + r.offset_hz * (a - r.t));
                *v += Complex64::new(co, si) * (amp * cover);
            }
        }
    }
    if let Some(bw) = s.if_bw_hz {
        // Twelve taps per (rate / bandwidth): sharp, as a receiver's IF filter
        // is. A softer one flattered the decoder: through a sharp 1.536 MHz
        // filter some bit patterns read wrong at any SNR, as on air.
        let taps = lowpass(((12.0 * s.rate / bw).ceil() as usize) | 1, bw / 2.0, s.rate);
        let h = taps.len() / 2;
        let y: Vec<Complex64> = (0..n)
            .map(|i| {
                let mut acc = Complex64::new(0.0, 0.0);
                for (k, t) in taps.iter().enumerate() {
                    let j = i as isize + k as isize - h as isize;
                    if j >= 0 && (j as usize) < n {
                        acc += x[j as usize] * *t as f64;
                    }
                }
                acc
            })
            .collect();
        x = y;
    }
    if let Some(snr) = s.snr_db {
        let mut rng = Rng::new(s.seed);
        let sigma = db_to_ratio(-snr).sqrt();
        for v in x.iter_mut() {
            *v += rng.cgauss() * sigma;
        }
    }
    x.into_iter().map(|z| Complex32::new(z.re as f32, z.im as f32)).collect()
}

/// Mode A/C fruit at `per_s` replies a second over `duration_s`, random
/// codes, levels between `lo_db` and `hi_db`, random frequency errors: the
/// background a real 1090 MHz receiver hears whenever a radar sweeps.
pub fn fruit(rng: &mut Rng, duration_s: f64, per_s: f64, lo_db: f64, hi_db: f64) -> Vec<Reply> {
    let n = (duration_s * per_s).round() as usize;
    (0..n)
        .map(|_| Reply {
            t: rng.uniform() * (duration_s - 25e-6).max(0.0),
            kind: ReplyKind::ModeAc((rng.next_u64() & 0x1FFF) as u16 & !(1 << 6)),
            level_db: lo_db + (hi_db - lo_db) * rng.uniform(),
            offset_hz: (rng.uniform() - 0.5) * 1e6,
            phase: rng.uniform(),
        })
        .collect()
}

/// Mode S replies of random bits under every format we read, at `per_s` a
/// second: what interference, overlapping replies and aircraft too far to
/// hear cleanly put in front of the parity check. None of them is a valid
/// message except by the chance the parity allows.
pub fn garbage(rng: &mut Rng, duration_s: f64, per_s: f64, lo_db: f64, hi_db: f64) -> Vec<Reply> {
    let n = (duration_s * per_s).round() as usize;
    const DFS: [u8; 7] = [4, 5, 11, 17, 18, 20, 21];
    (0..n)
        .map(|_| {
            let df = DFS[(rng.next_u64() % DFS.len() as u64) as usize];
            let len = if df < 16 { 7 } else { 14 };
            let mut b: Vec<u8> = (0..len).map(|_| rng.next_u64() as u8).collect();
            b[0] = (df << 3) | (b[0] & 7);
            Reply {
                t: rng.uniform() * (duration_s - 130e-6).max(0.0),
                kind: ReplyKind::ModeS(b),
                level_db: lo_db + (hi_db - lo_db) * rng.uniform(),
                offset_hz: (rng.uniform() - 0.5) * 1e6,
                phase: rng.uniform(),
            }
        })
        .collect()
}
