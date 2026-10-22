//! Small DSP pieces the decoders share: a windowed-sinc low-pass, a
//! decimating FIR, and the audio-to-IQ adapter the host puts in front of an
//! IQ-only decoder fed audio.

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
}
