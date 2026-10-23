//! RDS below the bit level: the 57 kHz subcarrier in the composite signal
//! to data bits (EN 62106 §1: BPSK of a biphase-coded, differentially
//! coded 1187.5 bit/s stream on a suppressed 57 kHz carrier).
//!
//! - **Down to baseband**, by a fixed 57 kHz oscillator and two decimating
//!   low-passes to about 12 kS/s. The second keeps ±2.4 kHz, the whole of
//!   RDS's shaped spectrum, and stops at 4 kHz, where the stereo
//!   difference signal's upper sideband ends (53 kHz).
//! - **Matched filter.** The transmitter sends each bit as an impulse pair,
//!   + then - half a bit later, through the standard's shaping filter
//!   cos(pi f td / 4) up to 2/td; the receiver correlates with exactly that
//!   symbol, which is the best it can do in noise and also what tells a
//!   bit's first half from its second.
//! - **Carrier.** The subcarrier is locked to the pilot's third harmonic
//!   in phase or in quadrature, the standard allows either, and in mono
//!   there is no pilot at all; so the phase is found from RDS itself, by a
//!   Costas loop on the matched filter's output (BPSK: the error is I x Q).
//!   Its frequency correction, a few hertz from the transmitter's and the
//!   receiver's clocks, also corrects the bit clock, which the standard
//!   divides from the same 57 kHz (by 48).
//! - **Bit timing** is where the matched filter's energy peaks within a
//!   bit: sixteen bins across a bit period average the output's power, and
//!   bits are read at their circular mean. The biphase symbol's energy at
//!   the half-bit point is a quarter of the peak's, so unlike Gardner's
//!   detector this cannot settle half a bit out. Read by interpolation
//!   between samples, at about 10.1 a bit.
//! - **Differential decoding** takes the carrier's 180-degree ambiguity
//!   away: a data bit is whether the symbol changed sign.
//!
//! The pieces follow the standard; redsea (MIT, github.com/windytan/redsea)
//! was read for the overall shape (mix, filter, Costas, symbol sync), and
//! nothing was copied.

use super::filt::{step, SinCos};
use crate::dsp::{lowpass, Decimator};
use rustfft::num_complex::Complex32;

pub const SUBCARRIER: f64 = 57_000.0;
pub const BITRATE: f64 = SUBCARRIER / 48.0;
const BINS: usize = 16;

/// The standard's transmitted pulse for one impulse: H(f) = cos(pi f td/4)
/// for |f| <= 2/td, in closed form (its inverse transform).
pub fn shaped_pulse(t: f64) -> f64 {
    let td = 1.0 / BITRATE;
    let a = std::f64::consts::PI * td / 4.0;
    let mut b = 2.0 * std::f64::consts::PI * t;
    // The two removable singularities at t = ±td/8.
    if (a * a - b * b).abs() < 1e-12 {
        b += 1e-7;
    }
    2.0 * a * (4.0 * std::f64::consts::PI * t / td).cos() / (a * a - b * b) * (1.0 / td)
}

/// One bit's symbol: the pulse, then its negative half a bit later.
pub fn biphase(t: f64) -> f64 {
    shaped_pulse(t) - shaped_pulse(t - 0.5 / BITRATE)
}

pub struct RdsDemod {
    sc: SinCos,
    phase: u32,
    step: u32,
    dec1: Decimator,
    dec2: Decimator,
    rate2: f64,
    mf: Vec<f32>,
    mf_buf: Vec<Complex32>,
    // Costas loop.
    theta: f64,
    freq: f64,
    kp: f64,
    ki: f64,
    pwr: f64,
    // Bit timing.
    energy: [f64; BINS],
    prev_r: f32,
    prev_bp: f64,
    psi: f64,
    prev_sym: bool,
    prev_v: f32,
    level: f64,
    noise: f64,
    pub bits_out: u64,
    scratch1: Vec<Complex32>,
    scratch2: Vec<Complex32>,
}

impl RdsDemod {
    pub fn new(rate: f64) -> Self {
        // To about 24 kS/s, then halve. The first stage only has to stop
        // what would alias into ±4 kHz at 24 kS/s.
        let d1 = (rate / 24_000.0).round().max(1.0) as usize;
        let r1 = rate / d1 as f64;
        let n1 = ((6.0 * rate / 16_000.0) as usize).max(15);
        let dec1 = Decimator::new(lowpass(n1, 10_000.0_f64.min(0.4 * r1), rate), d1);
        let n2 = ((5.5 * r1 / 1_500.0) as usize).max(15);
        let dec2 = Decimator::new(lowpass(n2, 3_300.0, r1), 2);
        let rate2 = r1 / 2.0;
        // The matched filter: the symbol about its centre, over ±2 bits,
        // correlated (oldest sample against the earliest time).
        let td = 1.0 / BITRATE;
        let half = (2.0 * td * rate2).ceil() as i64;
        let mut mf: Vec<f32> = (-half..=half).map(|k| biphase(td / 4.0 + k as f64 / rate2) as f32).collect();
        let norm = mf.iter().map(|x| x * x).sum::<f32>().sqrt();
        mf.iter_mut().for_each(|x| *x /= norm);
        // Costas: a 2nd-order loop, about 8 Hz natural frequency.
        let wn = 2.0 * std::f64::consts::PI * 8.0 / rate2;
        RdsDemod {
            sc: SinCos::new(),
            phase: 0,
            step: step(-SUBCARRIER, rate),
            dec1,
            dec2,
            rate2,
            mf_buf: vec![Complex32::new(0.0, 0.0); mf.len() - 1],
            mf,
            theta: 0.0,
            freq: 0.0,
            kp: 2.0 * 0.707 * wn,
            ki: wn * wn,
            pwr: 1e-6,
            energy: [0.0; BINS],
            prev_r: 0.0,
            prev_bp: 0.0,
            psi: 0.0,
            prev_sym: false,
            prev_v: 0.0,
            level: 0.0,
            noise: 0.0,
            bits_out: 0,
            scratch1: Vec::new(),
            scratch2: Vec::new(),
        }
    }

    pub fn reset(&mut self) {
        self.dec1.reset();
        self.dec2.reset();
        self.mf_buf.iter_mut().for_each(|z| *z = Complex32::new(0.0, 0.0));
        self.theta = 0.0;
        self.freq = 0.0;
        self.pwr = 1e-6;
        self.energy = [0.0; BINS];
        self.level = 0.0;
        self.noise = 0.0;
    }

    /// The carrier's offset from 57 kHz the loop has found, Hz.
    pub fn offset_hz(&self) -> f64 {
        self.freq * self.rate2 / (2.0 * std::f64::consts::PI)
    }

    /// The symbols' signal-to-noise ratio as the bit decisions see it, dB
    /// (the mean decision magnitude against its spread).
    pub fn snr_db(&self) -> f64 {
        if self.noise <= 0.0 {
            return 0.0;
        }
        10.0 * (self.level * self.level / self.noise).max(1e-6).log10()
    }

    /// Composite in (deviation-normalised); data bits out (appended), each
    /// with its confidence, the smaller of its two symbols' magnitudes.
    pub fn process(&mut self, mpx: &[f32], bits: &mut Vec<(bool, f32)>) {
        let mut mixed = std::mem::take(&mut self.scratch1);
        mixed.clear();
        for &x in mpx {
            let (s, c) = self.sc.at(self.phase);
            self.phase = self.phase.wrapping_add(self.step);
            mixed.push(Complex32::new(x * c, x * s));
        }
        let mut y1 = std::mem::take(&mut self.scratch2);
        y1.clear();
        self.dec1.process(&mixed, &mut y1);
        mixed.clear();
        self.dec2.process(&y1, &mut mixed);
        self.mf_buf.extend_from_slice(&mixed);
        let n = self.mf.len();
        let mut i = 0;
        while i + n <= self.mf_buf.len() {
            let w = &self.mf_buf[i..i + n];
            let (mut re, mut im) = (0.0f32, 0.0f32);
            for (z, t) in w.iter().zip(&self.mf) {
                re += z.re * t;
                im += z.im * t;
            }
            self.sample(Complex32::new(re, im), bits);
            i += 1;
        }
        self.mf_buf.drain(..i);
        self.scratch1 = mixed;
        self.scratch2 = y1;
    }

    fn sample(&mut self, z: Complex32, bits: &mut Vec<(bool, f32)>) {
        // Costas loop.
        let (s, c) = self.theta.sin_cos();
        let (s, c) = (s as f32, c as f32);
        let r = z.re * c + z.im * s;
        let q = z.im * c - z.re * s;
        let p = (r * r + q * q) as f64;
        self.pwr += 0.001 * (p - self.pwr);
        let e = (r as f64 * q as f64 / self.pwr.max(1e-12)).clamp(-1.0, 1.0) * 0.5;
        self.freq = (self.freq + self.ki * e).clamp(-0.02, 0.02);
        self.theta += self.freq + self.kp * e;
        if self.theta > std::f64::consts::PI {
            self.theta -= 2.0 * std::f64::consts::PI;
        } else if self.theta < -std::f64::consts::PI {
            self.theta += 2.0 * std::f64::consts::PI;
        }
        // Bit timing: the bit clock, corrected by the carrier's offset.
        let f_off = self.freq * self.rate2 / (2.0 * std::f64::consts::PI);
        let bitstep = BITRATE * (1.0 + f_off / SUBCARRIER) / self.rate2;
        let bp = self.prev_bp + bitstep;
        let frac = bp - bp.floor();
        let bin = ((frac * BINS as f64) as usize).min(BINS - 1);
        self.energy[bin] += 0.004 * ((r * r) as f64 - self.energy[bin]);
        // The peak, as the circular mean of the bins' energy.
        let (mut sx, mut sy) = (0.0, 0.0);
        for (k, e) in self.energy.iter().enumerate() {
            let a = 2.0 * std::f64::consts::PI * (k as f64 + 0.5) / BINS as f64;
            sx += e * a.cos();
            sy += e * a.sin();
        }
        let est = (sy.atan2(sx) / (2.0 * std::f64::consts::PI)).rem_euclid(1.0);
        // Kept continuous: a peak near the bit boundary would otherwise flip
        // between 0.99 and 0.01 and read a bit twice or not at all.
        let psi_prev = self.psi;
        self.psi += (est - psi_prev.rem_euclid(1.0) + 0.5).rem_euclid(1.0) - 0.5;
        let psi = self.psi;
        // A bit when the clock passes the peak, read between the samples.
        if (self.prev_bp - psi_prev).floor() < (bp - psi).floor() {
            let target = (bp - psi).floor() + psi;
            let f = ((target - self.prev_bp) / (bp - self.prev_bp)).clamp(0.0, 1.0) as f32;
            let v = self.prev_r + f * (r - self.prev_r);
            let sym = v > 0.0;
            // A data bit is two symbols compared: as sure as the weaker.
            bits.push((sym != self.prev_sym, v.abs().min(self.prev_v)));
            self.prev_sym = sym;
            self.prev_v = v.abs();
            self.bits_out += 1;
            let a = v.abs() as f64;
            self.level += 0.01 * (a - self.level);
            self.noise += 0.01 * ((a - self.level).powi(2) - self.noise);
        }
        self.prev_r = r;
        // Both clocks move down together, so their difference is kept.
        if bp > 1e6 {
            self.prev_bp = bp - 1e6;
            self.psi -= 1e6;
        } else {
            self.prev_bp = bp;
        }
    }
}
