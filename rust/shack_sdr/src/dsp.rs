//! Small DSP pieces the decoders share: a windowed-sinc low-pass, a
//! decimating FIR, the audio-to-IQ adapter the host puts in front of an
//! IQ-only decoder fed audio, and a resampler for any ratio.

use rustfft::num_complex::Complex32;

/// A low-pass FIR: `n` taps (odd), cutoff `fc` Hz at `rate`, Blackman
/// window, unity gain at DC.
pub fn lowpass(n: usize, fc: f64, rate: f64) -> Vec<f32> {
    let n = n | 1;
    let m = (n - 1) as f64 / 2.0;
    let w = 2.0 * std::f64::consts::PI * fc / rate;
    let mut taps: Vec<f64> = (0..n)
        .map(|i| {
            let x = i as f64 - m;
            let sinc = if x == 0.0 { w / std::f64::consts::PI } else { (w * x).sin() / (std::f64::consts::PI * x) };
            let a = 2.0 * std::f64::consts::PI * i as f64 / (n - 1) as f64;
            sinc * (0.42 - 0.5 * a.cos() + 0.08 * (2.0 * a).cos())
        })
        .collect();
    let sum: f64 = taps.iter().sum();
    taps.iter_mut().for_each(|t| *t /= sum);
    taps.into_iter().map(|t| t as f32).collect()
}

/// Filter-and-keep-every-D-th, carrying its state across calls.
#[derive(Clone, Debug)]
pub struct Decimator {
    taps: Vec<f32>,
    d: usize,
    buf: Vec<Complex32>,
    /// Input samples to skip before the next output.
    skip: usize,
}

impl Decimator {
    pub fn new(taps: Vec<f32>, d: usize) -> Self {
        let n = taps.len();
        Decimator { taps, d: d.max(1), buf: vec![Complex32::new(0.0, 0.0); n - 1], skip: 0 }
    }

    pub fn reset(&mut self) {
        let n = self.taps.len();
        self.buf.clear();
        self.buf.resize(n - 1, Complex32::new(0.0, 0.0));
        self.skip = 0;
    }

    /// Filter `x` and append every D-th output to `out`.
    pub fn process(&mut self, x: &[Complex32], out: &mut Vec<Complex32>) {
        let n = self.taps.len();
        self.buf.extend_from_slice(x);
        let mut i = self.skip; // index of the newest input sample of the next output, minus (n-1)
        while i + n <= self.buf.len() {
            let w = &self.buf[i..i + n];
            let mut re = 0.0f32;
            let mut im = 0.0f32;
            // Taps are symmetric, so their order against the window does not matter.
            for (s, t) in w.iter().zip(&self.taps) {
                re += s.re * t;
                im += s.im * t;
            }
            out.push(Complex32::new(re, im));
            i += self.d;
        }
        // Keep the last n-1 samples, and how far into them the next output is.
        let keep_from = self.buf.len().saturating_sub(n - 1);
        self.skip = i - keep_from;
        self.buf.drain(..keep_from);
    }
}

/// Real audio to complex baseband: the analytic signal of the audio,
/// shifted down by `centre` Hz, so a decoder of IQ hears a tone at
/// `centre` in the audio as one at 0 Hz. So RTTY on Icom's or Flex's
/// receive audio is the same decoder as on Radio's channel IQ
/// (RADIO-SDR-PLAN §4.1). The mix's image at -2 x centre is filtered out;
/// the output rate is the input's.
#[derive(Clone, Debug)]
pub struct AudioToIq {
    centre: f64,
    rate: f64,
    phase: f64,
    lp: Decimator,
}

impl AudioToIq {
    pub fn new(centre: f64, rate: f64) -> Self {
        // Pass +-0.9 x centre (and no more than 0.4 of the rate); the image
        // sits at -2 x centre, a full centre beyond the passband's edge.
        let fc = (0.9 * centre).min(0.4 * rate).max(100.0);
        let n = ((4.0 * rate / centre.max(100.0)).ceil() as usize).clamp(31, 255);
        AudioToIq { centre, rate, phase: 0.0, lp: Decimator::new(lowpass(n, fc, rate), 1) }
    }

    pub fn centre(&self) -> f64 {
        self.centre
    }

    pub fn reset(&mut self) {
        self.lp.reset();
    }

    /// The baseband of `x`, appended to `out` (one output per input, delayed
    /// by the filter's half length).
    pub fn process(&mut self, x: &[f32], out: &mut Vec<Complex32>) {
        let step = self.centre / self.rate;
        let mixed: Vec<Complex32> = x
            .iter()
            .map(|&a| {
                let (s, c) = (2.0 * std::f64::consts::PI * self.phase).sin_cos();
                self.phase += step;
                if self.phase >= 1.0 {
                    self.phase -= 1.0;
                }
                // x * e^{-j 2 pi f t}, times 2 so a tone of amplitude A comes
                // out at A, as an IQ tone would.
                Complex32::new((2.0 * a as f64 * c) as f32, (-2.0 * a as f64 * s) as f32)
            })
            .collect();
        self.lp.process(&mixed, out);
    }
}

/// What the resampler can carry: real audio or complex IQ.
pub trait Sample: Copy + Default + std::ops::Add<Output = Self> + std::ops::Mul<f32, Output = Self> {}
impl Sample for f32 {}
impl Sample for Complex32 {}

/// Polyphase phases between two input samples; the filter's value between
/// two phases is interpolated linearly. 128 keeps that interpolation's
/// error near -90 dB, below anything an 8-bit or 12-bit SDR delivers.
const PHASES: usize = 128;

/// Any rate to any other, streaming, its state carried across calls: a
/// Kaiser-windowed sinc evaluated at each output's position between input
/// samples. For DAB from a radio that cannot run at 2.048 MS/s (a Pluto at
/// 2.4, below its no-FIR floor: RADIO-SDR-PLAN §3.2), and for a decoder's
/// sound pulled at the app's own rate (DAB's 48 kHz into Radio's 12 kHz).
/// The ratio can be nudged while running (`set_ratio`), which is how DAB
/// follows a receiver whose sample clock is a few ppm off.
#[derive(Clone, Debug)]
pub struct Resampler<T: Sample> {
    /// in_rate / out_rate: input samples per output sample.
    step: f64,
    /// Taps per output (the filter's span in input samples).
    taps: usize,
    /// (PHASES + 1) x taps filter values.
    table: Vec<f32>,
    hist: Vec<T>,
    /// Position of the next output in `hist`, in input samples.
    pos: f64,
}

impl<T: Sample> Resampler<T> {
    /// `in_rate` to `out_rate`; `pass` is the fraction of the lower rate's
    /// Nyquist band kept flat (0.9 for audio; DAB's 1.536 MHz in 2.048 MS/s
    /// needs 0.75), and `taps` the filter's span at the lower rate.
    pub fn new(in_rate: f64, out_rate: f64, pass: f64, taps: usize) -> Self {
        let step = in_rate / out_rate;
        let scale = step.max(1.0); // decimating: the filter widens in input samples
        let n = ((taps as f64 * scale).ceil() as usize).max(4) & !1;
        // Cut-off midway between the passband edge and the lower Nyquist,
        // in cycles per input sample.
        let fc = 0.5 * (1.0 + pass) / 2.0 / scale;
        let beta = 8.0;
        let i0b = bessel_i0(beta);
        let half = n as f64 / 2.0;
        let mut table = vec![0f32; (PHASES + 1) * n];
        for ph in 0..=PHASES {
            let frac = ph as f64 / PHASES as f64;
            for j in 0..n {
                // Tap j sits at input offset (j - half + 1) - frac from the output point.
                let x = j as f64 - half + 1.0 - frac;
                let sinc = if x.abs() < 1e-12 { 2.0 * fc } else { (2.0 * std::f64::consts::PI * fc * x).sin() / (std::f64::consts::PI * x) };
                let r = x / half;
                let w = if r.abs() >= 1.0 { 0.0 } else { bessel_i0(beta * (1.0 - r * r).sqrt()) / i0b };
                table[ph * n + j] = (sinc * w) as f32;
            }
        }
        Resampler { step, taps: n, table, hist: vec![T::default(); n], pos: (n / 2) as f64 - 1.0 }
    }

    /// Input samples per output sample.
    pub fn ratio(&self) -> f64 {
        self.step
    }

    /// Change the ratio slightly (a clock correction), keeping the filter.
    pub fn set_ratio(&mut self, step: f64) {
        self.step = step;
    }

    pub fn reset(&mut self) {
        let n = self.taps;
        self.hist.clear();
        self.hist.resize(n, T::default());
        self.pos = (n / 2) as f64 - 1.0;
    }

    /// Resample `x`, appending to `out`. The output lags the input by half
    /// the filter's span.
    pub fn process(&mut self, x: &[T], out: &mut Vec<T>) {
        let n = self.taps;
        self.hist.extend_from_slice(x);
        // An output needs taps up to floor(pos) + n/2.
        while self.pos + (n / 2) as f64 + 1.0 <= self.hist.len() as f64 {
            let base = self.pos.floor();
            let frac = (self.pos - base) * PHASES as f64;
            let ph = (frac as usize).min(PHASES - 1);
            let a = (frac - ph as f64) as f32;
            let t0 = &self.table[ph * n..(ph + 1) * n];
            let t1 = &self.table[(ph + 1) * n..(ph + 2) * n];
            let start = base as usize + 1 - n / 2;
            let w = &self.hist[start..start + n];
            let mut acc = T::default();
            for k in 0..n {
                acc = acc + w[k] * (t0[k] + a * (t1[k] - t0[k]));
            }
            out.push(acc);
            self.pos += self.step;
        }
        // Keep what the next output still needs.
        let drop = (self.pos.floor() as usize + 1).saturating_sub(n / 2).min(self.hist.len());
        self.hist.drain(..drop);
        self.pos -= drop as f64;
    }
}

/// The zeroth-order modified Bessel function, for the Kaiser window.
fn bessel_i0(x: f64) -> f64 {
    let mut sum = 1.0;
    let mut term = 1.0;
    let q = x * x / 4.0;
    for k in 1..50 {
        term *= q / (k * k) as f64;
        sum += term;
        if term < sum * 1e-16 {
            break;
        }
    }
    sum
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(f: f64, rate: f64, n: usize) -> Vec<Complex32> {
        (0..n)
            .map(|i| {
                let p = 2.0 * std::f64::consts::PI * f * i as f64 / rate;
                Complex32::new(p.cos() as f32, p.sin() as f32)
            })
            .collect()
    }

    fn power(x: &[Complex32]) -> f64 {
        x.iter().map(|z| z.norm_sqr() as f64).sum::<f64>() / x.len() as f64
    }

    #[test]
    fn the_decimator_passes_the_band_and_stops_the_rest() {
        let mut d = Decimator::new(lowpass(63, 1000.0, 12_000.0), 4);
        let mut out = Vec::new();
        // In blocks of odd sizes, to exercise the carried state.
        let x = tone(300.0, 12_000.0, 12_000);
        for c in x.chunks(337) {
            d.process(c, &mut out);
        }
        assert_eq!(out.len(), 3000);
        assert!((power(&out[100..]) - 1.0).abs() < 0.02);
        let mut d = Decimator::new(lowpass(63, 1000.0, 12_000.0), 4);
        let mut out = Vec::new();
        d.process(&tone(2500.0, 12_000.0, 12_000), &mut out);
        assert!(power(&out[100..]) < 1e-4);
    }

    #[test]
    fn audio_becomes_baseband_with_no_image() {
        let rate = 12_000.0;
        let mut a = AudioToIq::new(1000.0, rate);
        // A real tone 85 Hz above the centre, amplitude 0.5.
        let audio: Vec<f32> =
            (0..24_000).map(|i| (0.5 * (2.0 * std::f64::consts::PI * 1085.0 * i as f64 / rate).cos()) as f32).collect();
        let mut out = Vec::new();
        a.process(&audio, &mut out);
        let y = &out[1000..];
        assert!((power(y) - 0.25).abs() < 0.01, "{}", power(y));
        // All of it at +85 Hz: correlate with that tone.
        let r = tone(85.0, rate, out.len());
        let c: Complex32 = y.iter().zip(&r[1000..]).map(|(a, b)| a * b.conj()).sum::<Complex32>() / y.len() as f32;
        assert!((c.norm() - 0.5).abs() < 0.01, "{}", c.norm());
    }

    #[test]
    fn the_resampler_keeps_a_tone_and_its_frequency() {
        // 2.4 MS/s to 2.048 (DAB from a Pluto), a tone near DAB's band edge,
        // fed in blocks of odd sizes.
        let (fi, fo) = (2_400_000.0, 2_048_000.0);
        let f = 700_000.0;
        let x = tone(f, fi, 240_000);
        let mut r = Resampler::new(fi, fo, 0.75, 24);
        let mut out = Vec::new();
        for c in x.chunks(9_973) {
            r.process(c, &mut out);
        }
        assert!((out.len() as f64 - 240_000.0 * fo / fi).abs() < 40.0, "{}", out.len());
        let y = &out[1000..out.len() - 100];
        assert!((power(y) - 1.0).abs() < 0.01, "{}", power(y));
        // All of it at f: the error against the ideal tone at the output rate,
        // allowing the filter's fixed delay as a phase, is far below it.
        let want = tone(f, fo, out.len());
        let c: Complex32 = y.iter().zip(&want[1000..]).map(|(a, b)| a * b.conj()).sum::<Complex32>() / y.len() as f32;
        assert!(c.norm() > 0.995, "{}", c.norm());
        let resid: f64 = y.iter().zip(&want[1000..]).map(|(a, b)| (a - b * c / c.norm()).norm_sqr() as f64).sum::<f64>() / y.len() as f64;
        assert!(resid < 1e-4, "{resid}");
    }

    #[test]
    fn the_resampler_stops_what_the_new_rate_cannot_hold() {
        // 48 kHz audio to 12 kHz: a 9 kHz tone would alias to 3 kHz.
        let x: Vec<f32> = (0..48_000).map(|i| (2.0 * std::f64::consts::PI * 9_000.0 * i as f64 / 48_000.0).sin() as f32).collect();
        let mut r = Resampler::<f32>::new(48_000.0, 12_000.0, 0.9, 32);
        let mut out = Vec::new();
        r.process(&x, &mut out);
        let p: f64 = out[200..].iter().map(|v| (*v as f64).powi(2)).sum::<f64>() / (out.len() - 200) as f64;
        assert!(p < 1e-5, "{p}");
        // One output per four inputs: the history starts full of zeros, so
        // the filter's delay costs no outputs.
        assert!((out.len() as i64 - 12_000).abs() <= 2, "{}", out.len());
    }
}
