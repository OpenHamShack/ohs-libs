//! The small DSP pieces wide FM needs and the shared `dsp` module does not
//! have: a table oscillator, a Kaiser-window low-pass for the audio
//! decimator's steep edge, and a decimating FIR on real samples written so
//! the compiler vectorises it. Kept here rather than in `dsp.rs` so this
//! branch and the DAB one, which both grew that file, do not fight over it.

use std::sync::OnceLock;

const TABLE_BITS: u32 = 10;
const TABLE_LEN: usize = 1 << TABLE_BITS;

fn table() -> &'static [[f32; 2]] {
    static T: OnceLock<Vec<[f32; 2]>> = OnceLock::new();
    T.get_or_init(|| {
        (0..=TABLE_LEN)
            .map(|k| {
                let a = 2.0 * std::f64::consts::PI * k as f64 / TABLE_LEN as f64;
                [a.sin() as f32, a.cos() as f32]
            })
            .collect()
    })
}

/// sin and cos of a phase held as a fraction of a turn in a u32 (2^32 is
/// one turn), from a 1024-point table with linear interpolation: within
/// 5e-6 of the true values, so the stereo decoder's 38 kHz reference costs
/// no measurable separation, at a fraction of the price of `sin_cos`.
#[derive(Clone, Copy)]
pub struct SinCos {
    t: &'static [[f32; 2]],
}

impl SinCos {
    pub fn new() -> Self {
        SinCos { t: table() }
    }
    #[inline]
    pub fn at(&self, phase: u32) -> (f32, f32) {
        let idx = (phase >> (32 - TABLE_BITS)) as usize;
        let frac = ((phase << TABLE_BITS) >> 8) as f32 * (1.0 / 16_777_216.0);
        let a = self.t[idx];
        let b = self.t[idx + 1];
        (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)
    }
}

impl Default for SinCos {
    fn default() -> Self {
        Self::new()
    }
}

/// A frequency as the per-sample step of a u32 phase.
pub fn step(hz: f64, rate: f64) -> u32 {
    ((hz / rate).rem_euclid(1.0) * 4_294_967_296.0).round() as u64 as u32
}

/// Turns (any sign) as a u32 phase increment.
#[inline]
pub fn turns(t: f64) -> u32 {
    (t.rem_euclid(1.0) * 4_294_967_296.0) as u64 as u32
}

fn bessel_i0(x: f64) -> f64 {
    let mut sum = 1.0;
    let mut term = 1.0;
    let q = x * x / 4.0;
    for k in 1..60 {
        term *= q / (k * k) as f64;
        sum += term;
        if term < sum * 1e-17 {
            break;
        }
    }
    sum
}

/// A low-pass FIR with a Kaiser window: `n` taps (made odd), -6 dB at `fc`
/// Hz, unity at DC. `beta` 7 gives about 70 dB of stopband with a
/// transition of 4.6 x rate / n, against Blackman's 5.5 x rate / n and 58 dB.
pub fn kaiser_lowpass(n: usize, fc: f64, rate: f64, beta: f64) -> Vec<f32> {
    let n = n | 1;
    let m = (n - 1) as f64 / 2.0;
    let w = 2.0 * std::f64::consts::PI * fc / rate;
    let i0b = bessel_i0(beta);
    let mut taps: Vec<f64> = (0..n)
        .map(|i| {
            let x = i as f64 - m;
            let sinc = if x == 0.0 { w / std::f64::consts::PI } else { (w * x).sin() / (std::f64::consts::PI * x) };
            let r = x / m;
            sinc * bessel_i0(beta * (1.0 - r * r).max(0.0).sqrt()) / i0b
        })
        .collect();
    let sum: f64 = taps.iter().sum();
    taps.iter_mut().for_each(|t| *t /= sum);
    taps.into_iter().map(|t| t as f32).collect()
}

/// A dot product in eight lanes, which LLVM turns into vector adds; a plain
/// `zip().sum()` stays scalar because float addition is not associative.
#[inline]
pub fn dot(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    let (a, b) = (&a[..n], &b[..n]);
    let mut acc = [0.0f32; 8];
    let ca = a.chunks_exact(8);
    let cb = b.chunks_exact(8);
    let (ra, rb) = (ca.remainder(), cb.remainder());
    for (x, y) in ca.zip(cb) {
        for i in 0..8 {
            acc[i] += x[i] * y[i];
        }
    }
    let mut s = (acc[0] + acc[4]) + (acc[1] + acc[5]) + (acc[2] + acc[6]) + (acc[3] + acc[7]);
    for (x, y) in ra.iter().zip(rb) {
        s += x * y;
    }
    s
}

/// Filter real samples and keep every D-th, carrying state across calls
/// (dsp::Decimator's algorithm, on f32 and through `dot`).
#[derive(Clone, Debug)]
pub struct RealDecim {
    taps: Vec<f32>,
    d: usize,
    buf: Vec<f32>,
    skip: usize,
}

impl RealDecim {
    pub fn new(taps: Vec<f32>, d: usize) -> Self {
        let n = taps.len();
        RealDecim { taps, d: d.max(1), buf: vec![0.0; n - 1], skip: 0 }
    }

    pub fn reset(&mut self) {
        let n = self.taps.len();
        self.buf.clear();
        self.buf.resize(n - 1, 0.0);
        self.skip = 0;
    }

    pub fn process(&mut self, x: &[f32], out: &mut Vec<f32>) {
        let n = self.taps.len();
        self.buf.extend_from_slice(x);
        let mut i = self.skip;
        while i + n <= self.buf.len() {
            // Symmetric taps: the window's order against them does not matter.
            out.push(dot(&self.buf[i..i + n], &self.taps));
            i += self.d;
        }
        let keep_from = self.buf.len().saturating_sub(n - 1);
        self.skip = i - keep_from;
        self.buf.drain(..keep_from);
    }
}

/// A one-pole low-pass's coefficient for a corner at `hz`.
pub fn one_pole(hz: f64, rate: f64) -> f32 {
    (1.0 - (-2.0 * std::f64::consts::PI * hz / rate).exp()) as f32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_table_oscillator_is_within_its_promise() {
        let sc = SinCos::new();
        let mut worst = 0.0f64;
        for k in 0..100_000u64 {
            let p = (k.wrapping_mul(2_654_435_761) & 0xffff_ffff) as u32;
            let a = p as f64 / 4_294_967_296.0 * 2.0 * std::f64::consts::PI;
            let (s, c) = sc.at(p);
            worst = worst.max((s as f64 - a.sin()).abs()).max((c as f64 - a.cos()).abs());
        }
        assert!(worst < 6e-6, "{worst}");
    }

    #[test]
    fn the_kaiser_lowpass_has_its_stopband() {
        let rate = 240_000.0;
        let h = kaiser_lowpass(261, 17_000.0, rate, 7.0);
        let resp = |f: f64| {
            let (mut re, mut im) = (0.0, 0.0);
            for (i, t) in h.iter().enumerate() {
                let a = -2.0 * std::f64::consts::PI * f / rate * i as f64;
                re += *t as f64 * a.cos();
                im += *t as f64 * a.sin();
            }
            20.0 * (re * re + im * im).sqrt().log10()
        };
        assert!(resp(15_000.0).abs() < 0.1, "{}", resp(15_000.0));
        assert!(resp(19_000.0) < -60.0, "{}", resp(19_000.0));
        assert!(resp(40_000.0) < -65.0, "{}", resp(40_000.0));
    }

    #[test]
    fn the_real_decimator_matches_a_direct_convolution() {
        let taps: Vec<f32> = (0..31).map(|i| 1.0 / (1.0 + (i as f32 - 15.0).abs())).collect();
        let x: Vec<f32> = (0..1000).map(|i| ((i * 37) % 101) as f32 / 50.0 - 1.0).collect();
        let mut d = RealDecim::new(taps.clone(), 5);
        let mut out = Vec::new();
        for c in x.chunks(77) {
            d.process(c, &mut out);
        }
        // Output m ends at input 5m, with zeros before the first sample, as dsp::Decimator.
        for (m, y) in out.iter().enumerate() {
            let want: f32 = (0..31).filter(|&k| 5 * m + k >= 30).map(|k| taps[k] * x[5 * m + k - 30]).sum();
            assert!((y - want).abs() < 1e-4, "{m}: {y} {want}");
        }
        assert_eq!(out.len(), 200);
    }
}
