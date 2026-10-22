//! Test signals (RADIO-SDR-PLAN §7.1): each mode's encoder made for tests,
//! and the channel models the decoders are measured through - AWGN,
//! Watterson fading (ITU-R F.1487), a frequency offset and drift, and a
//! clock error.
//!
//! **Bit-identical on every platform.** The RTTY fixtures' golden numbers
//! (testdata/rtty/) were captured from the Python decoder on exactly these
//! samples, and the fixtures are pinned by the sha256 of what this module
//! makes. So nothing here calls the platform's libm, whose sin, cos and log
//! differ by an ulp between glibc, macOS and Windows: sines, cosines and
//! logarithms are our own series (`sin_cos_turns`, `ln`), and everything
//! else is IEEE addition, multiplication, division and sqrt, which are
//! correctly rounded everywhere. Rust never fuses a multiply-add unless told
//! to. The noise is xoshiro256** seeded by splitmix64, Gaussian by
//! Box-Muller on those series.

use crate::rtty::ita2;
use rustfft::num_complex::{Complex32, Complex64};

// ---------------------------------------------------------------- arithmetic

const PI: f64 = std::f64::consts::PI;
const LN2: f64 = std::f64::consts::LN_2;

/// sin and cos of `t` turns (t = 0.25 is a right angle). Reduced to an
/// eighth of a turn and summed as Taylor series to 1e-16, so the result
/// depends on nothing but IEEE arithmetic.
pub fn sin_cos_turns(t: f64) -> (f64, f64) {
    let t = t - t.floor(); // [0, 1)
    let q = (t * 4.0).floor(); // quadrant 0..3
    let r = t * 4.0 - q; // [0, 1) of the quadrant
    // r in quarter-turns; use the half nearer 0 so |x| <= pi/4.
    let (x, flip) = if r > 0.5 { ((r - 1.0) * (PI / 2.0), true) } else { (r * (PI / 2.0), false) };
    let x2 = x * x;
    // sin x = x - x^3/3! + ..., to x^19; cos x to x^18. |x| <= pi/4.
    let mut s = 0.0;
    let mut c = 0.0;
    let mut term_s = x;
    let mut term_c = 1.0;
    for k in 0..10 {
        s += term_s;
        c += term_c;
        let a = (2 * k + 2) as f64;
        let b = (2 * k + 3) as f64;
        term_s = -term_s * x2 / (a * b);
        term_c = -term_c * x2 / ((2 * k + 1) as f64 * a);
    }
    // (s, c) is the angle x; with flip, the angle is a quarter-turn more.
    let quadrant = q as i32 + if flip { 1 } else { 0 };
    match quadrant & 3 {
        0 => (s, c),
        1 => (c, -s),
        2 => (-s, -c),
        _ => (-c, s),
    }
}

/// Natural logarithm of x > 0, from its binary exponent and an atanh
/// series on the mantissa.
pub fn ln(x: f64) -> f64 {
    assert!(x > 0.0 && x.is_finite());
    let bits = x.to_bits();
    let mut e = ((bits >> 52) & 0x7ff) as i64 - 1023;
    let mut m = f64::from_bits((bits & 0x000f_ffff_ffff_ffff) | 0x3ff0_0000_0000_0000); // [1, 2)
    if e == -1023 {
        // Subnormal: scale up first.
        return ln(x * 2f64.powi(54)) - 54.0 * LN2;
    }
    if m > std::f64::consts::SQRT_2 {
        m *= 0.5;
        e += 1;
    }
    let z = (m - 1.0) / (m + 1.0); // |z| < 0.172
    let z2 = z * z;
    let mut sum = 0.0;
    let mut p = z;
    for k in 0..14 {
        sum += p / (2 * k + 1) as f64;
        p *= z2;
    }
    2.0 * sum + e as f64 * LN2
}

/// xoshiro256**, seeded by splitmix64: small, fast, and the same numbers on
/// every platform.
#[derive(Clone, Debug)]
pub struct Rng {
    s: [u64; 4],
    spare: Option<f64>,
}

impl Rng {
    pub fn new(seed: u64) -> Self {
        let mut z = seed;
        let mut next = || {
            z = z.wrapping_add(0x9e37_79b9_7f4a_7c15);
            let mut x = z;
            x = (x ^ (x >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
            x = (x ^ (x >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
            x ^ (x >> 31)
        };
        Rng { s: [next(), next(), next(), next()], spare: None }
    }

    pub fn next_u64(&mut self) -> u64 {
        let r = self.s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = self.s[1] << 17;
        self.s[2] ^= self.s[0];
        self.s[3] ^= self.s[1];
        self.s[1] ^= self.s[2];
        self.s[0] ^= self.s[3];
        self.s[2] ^= t;
        self.s[3] = self.s[3].rotate_left(45);
        r
    }

    /// Uniform in [0, 1).
    pub fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// Standard normal (Box-Muller, both halves used).
    pub fn gauss(&mut self) -> f64 {
        if let Some(g) = self.spare.take() {
            return g;
        }
        let u1 = 1.0 - self.uniform(); // (0, 1]
        let u2 = self.uniform();
        let r = (-2.0 * ln(u1)).sqrt();
        let (s, c) = sin_cos_turns(u2);
        self.spare = Some(r * s);
        r * c
    }

    /// Complex normal with E|z|^2 = 1.
    pub fn cgauss(&mut self) -> Complex64 {
        let k = std::f64::consts::FRAC_1_SQRT_2;
        Complex64::new(self.gauss() * k, self.gauss() * k)
    }
}

// ---------------------------------------------------------------- RTTY

/// How a test RTTY signal is sent.
#[derive(Clone, Debug)]
pub struct RttyTx {
    pub baud: f64,
    /// Mark minus space, Hz. Normal: mark is the higher frequency.
    pub shift: f64,
    /// Mark below space.
    pub reverse: bool,
    /// Stop bits (1.5 is what rtty.py and nearly every station send).
    pub stop_bits: f64,
    /// Seconds of steady mark before the first character and after the
    /// last (rtty.py's preamble and postamble).
    pub preamble: f64,
    pub postamble: f64,
    /// LTRS sent ahead of the text, as rtty.py's Encoder.LEAD_LTRS.
    pub lead_ltrs: usize,
    /// The sender's clock error, parts per million (a slow or fast keyer).
    pub clock_ppm: f64,
}

impl Default for RttyTx {
    fn default() -> Self {
        RttyTx {
            baud: 45.45,
            shift: 170.0,
            reverse: false,
            stop_bits: 1.5,
            preamble: 0.2,
            postamble: 0.1,
            lead_ltrs: 2,
            clock_ppm: 0.0,
        }
    }
}

/// The keying of `codes` as (offset from centre in Hz, seconds) segments:
/// start bit space, five data bits, stop bits mark, idle mark around them.
pub fn rtty_segments(codes: &[u8], tx: &RttyTx) -> Vec<(f64, f64)> {
    let half = tx.shift / 2.0 * if tx.reverse { -1.0 } else { 1.0 };
    let (mark, space) = (half, -half);
    let bit = 1.0 / (tx.baud * (1.0 + tx.clock_ppm * 1e-6));
    let mut segs = Vec::new();
    if codes.is_empty() {
        return segs;
    }
    segs.push((mark, tx.preamble));
    for &code in codes {
        segs.push((space, bit));
        for k in (0..5).rev() {
            segs.push((if (code >> k) & 1 == 1 { mark } else { space }, bit));
        }
        segs.push((mark, tx.stop_bits * bit));
    }
    // About four and a half bits of idle line at 45.45 Bd, never under two
    // (rtty.py Encoder.render_codes).
    segs.push((mark, tx.postamble.max(2.0 * bit)));
    segs
}

/// The codes rtty.py's Encoder sends for `text`: lead LTRS, then the text.
pub fn rtty_codes(text: &str, tx: &RttyTx) -> Vec<u8> {
    let codes = ita2::encode(text);
    if codes.is_empty() {
        return codes;
    }
    let mut out = vec![ita2::LTRS; tx.lead_ltrs];
    out.extend(codes);
    out
}

/// Continuous-phase FSK at unit amplitude, as complex baseband: the tones
/// sit at `centre` + the segment's offset. Each segment ends at the rounded
/// cumulative time, so 1.5-bit stop bits never accumulate rounding drift
/// (as rtty.py). `drift` is Hz per second added to the centre.
pub fn fsk(segments: &[(f64, f64)], rate: f64, centre: f64, drift: f64) -> Vec<Complex64> {
    let mut out = Vec::new();
    let mut phase = 0.0f64; // turns
    let mut t_end = 0.0;
    let mut pos: usize = 0;
    for &(off, dur) in segments {
        t_end += dur;
        let end = (t_end * rate).round() as usize;
        while pos < end {
            let t = pos as f64 / rate;
            let f = centre + off + drift * t;
            let (s, c) = sin_cos_turns(phase);
            out.push(Complex64::new(c, s));
            phase += f / rate;
            phase -= phase.floor();
            pos += 1;
        }
    }
    out
}

// ---------------------------------------------------------------- channels

/// ITU-R F.1487's HF channels: two equal Rayleigh paths with a Gaussian
/// Doppler spectrum. `spread_hz` is the frequency spread (twice the
/// spectrum's standard deviation), `delay_s` the second path's delay.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Watterson {
    pub delay_s: f64,
    pub spread_hz: f64,
}

impl Watterson {
    /// Mid-latitude "moderate".
    pub const MODERATE: Watterson = Watterson { delay_s: 1e-3, spread_hz: 0.5 };
    /// Mid-latitude "disturbed" / CCIR "poor".
    pub const POOR: Watterson = Watterson { delay_s: 2e-3, spread_hz: 1.0 };
    pub const GOOD: Watterson = Watterson { delay_s: 0.5e-3, spread_hz: 0.1 };
}

/// A complex Gaussian process with a Gaussian spectrum of standard deviation
/// `sigma_hz`, E|g|^2 = 1, at `rate`, `n` samples long: white noise at a
/// low rate through a Gaussian FIR, then linear interpolation.
fn doppler_process(rng: &mut Rng, sigma_hz: f64, rate: f64, n: usize) -> Vec<Complex64> {
    let low = 100.0; // Hz: far above any spread here, far below the audio rate
    let sigma_t = 1.0 / (2.0 * PI * sigma_hz); // seconds
    let half = (4.0 * sigma_t * low).ceil() as i64;
    let taps: Vec<f64> = (-half..=half)
        .map(|k| {
            let t = k as f64 / low;
            // exp(-x) = 1 / e^x, with e^x from our own series: x <= 8 here.
            exp_neg(t * t / (2.0 * sigma_t * sigma_t))
        })
        .collect();
    let norm = taps.iter().map(|t| t * t).sum::<f64>().sqrt();
    let m = (n as f64 / rate * low).ceil() as usize + 2;
    let white: Vec<Complex64> = (0..m + taps.len()).map(|_| rng.cgauss()).collect();
    let slow: Vec<Complex64> = (0..m)
        .map(|i| {
            let mut acc = Complex64::new(0.0, 0.0);
            for (k, t) in taps.iter().enumerate() {
                acc += white[i + k] * *t;
            }
            acc / norm
        })
        .collect();
    (0..n)
        .map(|i| {
            let x = i as f64 / rate * low;
            let j = x.floor() as usize;
            let f = x - j as f64;
            slow[j] * (1.0 - f) + slow[j + 1] * f
        })
        .collect()
}

/// e^-x for 0 <= x, from a series on x / 2^k squared back up.
fn exp_neg(x: f64) -> f64 {
    if x > 700.0 {
        return 0.0;
    }
    let mut k = 0;
    let mut y = x;
    while y > 0.5 {
        y *= 0.5;
        k += 1;
    }
    let mut sum = 1.0;
    let mut term = 1.0;
    for i in 1..20 {
        term *= -y / i as f64;
        sum += term;
    }
    for _ in 0..k {
        sum *= sum;
    }
    sum
}

/// Pass `x` through a Watterson channel. Mean power is kept: the two paths
/// carry half each.
pub fn watterson(x: &[Complex64], rate: f64, ch: Watterson, rng: &mut Rng) -> Vec<Complex64> {
    let sigma = ch.spread_hz / 2.0;
    let g1 = doppler_process(rng, sigma, rate, x.len());
    let g2 = doppler_process(rng, sigma, rate, x.len());
    let d = (ch.delay_s * rate).round() as usize;
    let k = std::f64::consts::FRAC_1_SQRT_2;
    (0..x.len())
        .map(|i| {
            let late = if i >= d { x[i - d] } else { Complex64::new(0.0, 0.0) };
            (x[i] * g1[i] + late * g2[i]) * k
        })
        .collect()
}

/// Signal-to-noise ratios here are in 2.5 kHz, as RTTY's are quoted
/// (RADIO-SDR-PLAN §7.2).
pub const SNR_BANDWIDTH: f64 = 2500.0;

// ---------------------------------------------------------------- a case

/// One test signal: what is sent, through what, at what SNR.
#[derive(Clone, Debug)]
pub struct RttyCase {
    pub text: String,
    pub tx: RttyTx,
    pub rate: f64,
    /// Where the signal's centre sits: the audio tone (1000 Hz, as rtty.py's
    /// default) for audio, or the offset from the channel centre for IQ.
    pub centre: f64,
    /// Hz per second.
    pub drift: f64,
    /// None: no noise at all.
    pub snr_db: Option<f64>,
    pub fading: Option<Watterson>,
    /// Seconds of noise alone before and after the transmission, where a
    /// decoder must print nothing.
    pub lead_s: f64,
    pub tail_s: f64,
    pub seed: u64,
}

impl Default for RttyCase {
    fn default() -> Self {
        RttyCase {
            text: String::new(),
            tx: RttyTx::default(),
            rate: 12_000.0,
            centre: 1000.0,
            drift: 0.0,
            snr_db: None,
            fading: None,
            lead_s: 1.0,
            tail_s: 1.0,
            seed: 1,
        }
    }
}

/// The transmission as complex baseband with the channel applied, before
/// noise, at amplitude 1 (lead and tail silent).
fn rtty_clean(case: &RttyCase, rng: &mut Rng) -> Vec<Complex64> {
    let codes = rtty_codes(&case.text, &case.tx);
    let segs = rtty_segments(&codes, &case.tx);
    let mut x = vec![Complex64::new(0.0, 0.0); (case.lead_s * case.rate).round() as usize];
    x.extend(fsk(&segs, case.rate, case.centre, case.drift));
    x.resize(x.len() + (case.tail_s * case.rate).round() as usize, Complex64::new(0.0, 0.0));
    match case.fading {
        Some(ch) => watterson(&x, case.rate, ch, rng),
        None => x,
    }
}

/// The case as real audio: the transmission's real part at amplitude 0.5,
/// plus white noise at the SNR in 2.5 kHz. Signal power 0.125; noise of
/// variance s2 over the band to rate/2 puts s2 * 2500 / (rate / 2) in 2.5 kHz.
pub fn rtty_audio(case: &RttyCase) -> Vec<f32> {
    let mut rng = Rng::new(case.seed);
    let x = rtty_clean(case, &mut rng);
    let amp = 0.5;
    let sigma = case
        .snr_db
        .map(|snr| {
            let p_sig = amp * amp / 2.0;
            let p_band = p_sig / db_to_ratio(snr);
            (p_band * (case.rate / 2.0) / SNR_BANDWIDTH).sqrt()
        })
        .unwrap_or(0.0);
    let mut noise = Rng::new(case.seed ^ 0x6e6f_6973_6521);
    x.iter().map(|z| (amp * z.re + sigma * noise.gauss()) as f32).collect()
}

/// The case as complex baseband IQ: amplitude 0.5 (power 0.25), complex
/// noise whose power across the whole `rate` puts the SNR in 2.5 kHz.
pub fn rtty_iq(case: &RttyCase) -> Vec<Complex32> {
    let mut rng = Rng::new(case.seed);
    let x = rtty_clean(case, &mut rng);
    let amp = 0.5;
    let sigma = case
        .snr_db
        .map(|snr| {
            let p_band = amp * amp / db_to_ratio(snr);
            (p_band * case.rate / SNR_BANDWIDTH).sqrt()
        })
        .unwrap_or(0.0);
    let mut noise = Rng::new(case.seed ^ 0x6e6f_6973_6521);
    x.iter()
        .map(|z| {
            let n = noise.cgauss() * sigma;
            Complex32::new((amp * z.re + n.re) as f32, (amp * z.im + n.im) as f32)
        })
        .collect()
}

/// 10^(db/10) by our own exp: e^(db ln10 / 10).
pub fn db_to_ratio(db: f64) -> f64 {
    let x = db * std::f64::consts::LN_10 / 10.0;
    if x >= 0.0 {
        1.0 / exp_neg(x)
    } else {
        exp_neg(-x)
    }
}

/// What a decoder should print for `text`: what the far teleprinter would
/// (the printer's own rules, so CR and BELL vanish, as in the decoder).
pub fn rtty_truth(text: &str) -> String {
    let mut p = ita2::Printer::new(true);
    ita2::encode(text).into_iter().filter_map(|c| p.code(c)).collect()
}

/// Edit distance, for the character error rate.
pub fn levenshtein(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    let mut cur = vec![0; b.len() + 1];
    for i in 1..=a.len() {
        cur[0] = i;
        for j in 1..=b.len() {
            let sub = prev[j - 1] + usize::from(a[i - 1] != b[j - 1]);
            cur[j] = sub.min(prev[j] + 1).min(cur[j - 1] + 1);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[b.len()]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_series_match_the_platform_to_rounding() {
        for i in 0..2000 {
            let t = i as f64 / 1999.0 * 3.0 - 1.0;
            let (s, c) = sin_cos_turns(t);
            assert!((s - (2.0 * PI * t).sin()).abs() < 1e-14, "sin {t}");
            assert!((c - (2.0 * PI * t).cos()).abs() < 1e-14, "cos {t}");
            let x = (i as f64 + 0.5) * 1e-3;
            assert!((ln(x) - x.ln()).abs() < 1e-14 * x.ln().abs().max(1.0), "ln {x}");
            assert!((exp_neg(x) - (-x).exp()).abs() < 1e-14, "exp {x}");
        }
        assert!((db_to_ratio(-5.0) - 10f64.powf(-0.5)).abs() < 1e-14);
    }

    #[test]
    fn noise_is_unit_normal() {
        let mut r = Rng::new(7);
        let n = 200_000;
        let v: Vec<f64> = (0..n).map(|_| r.gauss()).collect();
        let mean = v.iter().sum::<f64>() / n as f64;
        let var = v.iter().map(|x| (x - mean) * (x - mean)).sum::<f64>() / n as f64;
        assert!(mean.abs() < 0.01 && (var - 1.0).abs() < 0.01, "{mean} {var}");
        // Tails: Gaussian has 0.27 % beyond 3 sigma.
        let tail = v.iter().filter(|x| x.abs() > 3.0).count() as f64 / n as f64;
        assert!((tail - 0.0027).abs() < 0.0006, "{tail}");
    }

    #[test]
    fn the_snr_is_what_was_asked_for() {
        // Noise alone and signal alone, measured.
        let case = RttyCase { text: "RYRYRYRYRY".into(), snr_db: Some(-5.0), lead_s: 0.0, tail_s: 0.0, ..Default::default() };
        let mut clean = case.clone();
        clean.snr_db = None;
        let s = rtty_audio(&clean);
        let both = rtty_audio(&case);
        let p_sig = s.iter().map(|x| (*x as f64).powi(2)).sum::<f64>() / s.len() as f64;
        let p_noise = s.iter().zip(&both).map(|(a, b)| (*b as f64 - *a as f64).powi(2)).sum::<f64>() / s.len() as f64;
        let snr = 10.0 * (p_sig / (p_noise * SNR_BANDWIDTH / 6000.0)).log10();
        assert!((snr + 5.0).abs() < 0.15, "{snr}");
    }

    #[test]
    fn fading_keeps_the_mean_power() {
        let x = vec![Complex64::new(1.0, 0.0); 12_000 * 60];
        let mut rng = Rng::new(3);
        let y = watterson(&x, 12_000.0, Watterson::MODERATE, &mut rng);
        let p = y.iter().map(|z| z.norm_sqr()).sum::<f64>() / y.len() as f64;
        assert!((p - 1.0).abs() < 0.35, "{p}"); // a minute of 0.5 Hz fading
        // And it fades: the deepest second is far below the mean.
        let worst = y.chunks(12_000).map(|c| c.iter().map(|z| z.norm_sqr()).sum::<f64>() / 12_000.0).fold(f64::MAX, f64::min);
        assert!(worst < 0.3, "{worst}");
    }

    #[test]
    fn the_keying_is_rtty_pys() {
        let tx = RttyTx::default();
        let codes = rtty_codes("RY", &tx);
        assert_eq!(codes, vec![ita2::LTRS, ita2::LTRS, 0b01010, 0b10101]);
        let segs = rtty_segments(&codes, &tx);
        // preamble, 4 x (start + 5 + stop), postamble
        assert_eq!(segs.len(), 1 + 4 * 7 + 1);
        let total: f64 = segs.iter().map(|s| s.1).sum();
        assert!((total - (0.2 + 4.0 * 7.5 / 45.45 + 0.1)).abs() < 1e-9);
    }

    #[test]
    fn levenshtein_counts_edits() {
        assert_eq!(levenshtein("CQ CQ", "CQ CQ"), 0);
        assert_eq!(levenshtein("CQ XQ", "CQ CQ"), 1);
        assert_eq!(levenshtein("CQCQ", "CQ CQ"), 1);
        assert_eq!(levenshtein("", "ABC"), 3);
    }
}
