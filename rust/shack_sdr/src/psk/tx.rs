//! The PSK test transmitter and its channel (RADIO-SDR-PLAN §7.1): what the
//! decoder is measured on, and what psk.py's golden numbers were captured
//! on (testdata/psk/, tools/psk_golden.py).
//!
//! The signal is PSK31 as G3PLX described it: a 0 bit reverses the
//! phase, a 1 leaves it, idle is reversals, and the amplitude passes from
//! one symbol to the next along a half cosine, so idle is exactly two tones
//! half the baud either side of the carrier. Each symbol is thus a Hann
//! pulse two symbols long, centred on its symbol, which the decoder's
//! matched filter is. (psk.py's Encoder smooths the symbols with a
//! one-symbol Hann window instead: nearly the same transition, not quite
//! the same pulse.) It is written as a function of time, so it can be made
//! at any rate and with any sound-card clock error.
//!
//! Like testsig (whose arithmetic it uses), it is bit-identical on every
//! platform: the fixtures are pinned by the sha256 of what this makes.

use super::varicode;
use crate::testsig::{db_to_ratio, sin_cos_turns, watterson, Rng, Watterson, SNR_BANDWIDTH};
use rustfft::num_complex::{Complex32, Complex64};

/// How a test PSK signal is sent.
#[derive(Clone, Debug)]
pub struct PskTx {
    pub baud: f64,
    /// Seconds of idle (reversals) before the text and after it. Rounded
    /// down to an even number of symbols, as psk.py's Encoder does.
    pub preamble_s: f64,
    pub postamble_s: f64,
    /// The sender's sound-card clock error, parts per million.
    pub clock_ppm: f64,
    /// Third-order intermodulation of an overdriven transmitter, dB of the
    /// products at 1.5 baud against the tones at 0.5 baud on idle; None is
    /// a clean linear transmitter.
    pub imd_db: Option<f64>,
}

impl Default for PskTx {
    fn default() -> Self {
        // fldigi sends about a second of reversals before an over.
        PskTx { baud: 31.25, preamble_s: 1.0, postamble_s: 0.5, clock_ppm: 0.0, imd_db: None }
    }
}

/// A second station on the air beside the wanted one.
#[derive(Clone, Debug)]
pub struct Interferer {
    /// Its carrier, Hz from the wanted signal's.
    pub offset_hz: f64,
    /// Its mean power, dB over the wanted signal's.
    pub level_db: f64,
    pub baud: f64,
    pub text: String,
}

/// One test signal: what is sent, through what, at what SNR.
#[derive(Clone, Debug)]
pub struct PskCase {
    pub text: String,
    pub tx: PskTx,
    pub rate: f64,
    /// The carrier: the audio tone (1000 Hz, psk.py's default) for audio, or
    /// the offset from the channel centre for IQ.
    pub centre: f64,
    /// Hz per second.
    pub drift: f64,
    /// SNR in 2.5 kHz (testsig::SNR_BANDWIDTH) of the wanted signal's mean
    /// power; None: no noise.
    pub snr_db: Option<f64>,
    pub fading: Option<Watterson>,
    pub interferer: Option<Interferer>,
    /// Seconds of noise alone before and after.
    pub lead_s: f64,
    pub tail_s: f64,
    pub seed: u64,
}

impl Default for PskCase {
    fn default() -> Self {
        PskCase {
            text: String::new(),
            tx: PskTx::default(),
            rate: 12_000.0,
            centre: 1000.0,
            drift: 0.0,
            snr_db: None,
            fading: None,
            interferer: None,
            lead_s: 1.0,
            tail_s: 2.0,
            seed: 1,
        }
    }
}

/// The symbols (+1/-1) sent for `text`: preamble, the text's bits,
/// postamble. No text is no transmission at all (a case of noise alone).
pub fn symbols(text: &str, tx: &PskTx) -> Vec<f64> {
    if varicode::bits(text).is_empty() {
        return Vec::new();
    }
    let even = |s: f64| ((s * tx.baud).round() as usize) / 2 * 2;
    let mut bits = vec![0u8; even(tx.preamble_s)];
    bits.extend(varicode::bits(text));
    bits.extend(vec![0u8; even(tx.postamble_s)]);
    let mut state = 1.0;
    bits.iter()
        .map(|b| {
            if *b == 0 {
                state = -state;
            }
            state
        })
        .collect()
}

/// The transmission as complex baseband at peak amplitude 1, its carrier at
/// `centre` + `drift` x t. The amplitude eases from 0 to the first symbol
/// and back to 0 after the last, half a cosine each.
pub fn modulate(syms: &[f64], tx: &PskTx, rate: f64, centre: f64, drift: f64) -> Vec<Complex64> {
    if syms.is_empty() {
        return Vec::new();
    }
    let t_sym = 1.0 / (tx.baud * (1.0 + tx.clock_ppm * 1e-6));
    // Symbol centres at whole u = t / t_sym: a(0) = 0, a(j) = syms[j-1],
    // a(n+1) = 0.
    let a = |j: usize| if j == 0 || j > syms.len() { 0.0 } else { syms[j - 1] };
    let n = ((syms.len() + 1) as f64 * t_sym * rate).floor() as usize;
    // Third-order compression y = x (1 - k |x|^2): on idle's cos envelope it
    // leaves the tones at A(1 - 3kA^2/4) and puts kA^3/4 at 1.5 baud, so
    // for products r (amplitude) under the tones, k = 4r / (1 + 3r).
    let k = tx.imd_db.map(|db| {
        let r = db_to_ratio(db / 2.0);
        4.0 * r / (1.0 + 3.0 * r)
    });
    let mut phase = 0.0f64;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let t = i as f64 / rate;
        let u = t / t_sym;
        let j = u.floor() as usize;
        let f = u - j as f64;
        let (a0, a1) = (a(j), a(j + 1));
        let (_, c) = sin_cos_turns(f / 2.0);
        let b = 0.5 * (a0 + a1) + 0.5 * (a0 - a1) * c;
        let (s, cc) = sin_cos_turns(phase);
        let mut z = Complex64::new(b * cc, b * s);
        if let Some(k) = k {
            z *= 1.0 - k * b * b;
        }
        out.push(z);
        phase += (centre + drift * t) / rate;
        phase -= phase.floor();
    }
    out
}

/// Amplitude of the wanted signal's peak, for audio and IQ alike.
const AMP: f64 = 0.5;

/// The case's wanted signal, faded, with any interferer added, before
/// noise; and the wanted signal's mean power while it is on.
fn clean(case: &PskCase, rng: &mut Rng) -> (Vec<Complex64>, f64) {
    let syms = symbols(&case.text, &case.tx);
    let lead = (case.lead_s * case.rate).round() as usize;
    let sig = modulate(&syms, &case.tx, case.rate, case.centre, case.drift);
    // With nothing sent, the SNR is reckoned against idle's power (half the
    // peak's), so noise alone is as loud as it is beside a signal.
    let mean = if sig.is_empty() { 0.5 } else { sig.iter().map(|z| z.norm_sqr()).sum::<f64>() / sig.len() as f64 };
    let mut x = vec![Complex64::new(0.0, 0.0); lead];
    x.extend(sig);
    x.resize(x.len() + (case.tail_s * case.rate).round() as usize, Complex64::new(0.0, 0.0));
    let mut x = match case.fading {
        Some(ch) => watterson(&x, case.rate, ch, rng),
        None => x,
    };
    if let Some(i) = &case.interferer {
        let tx = PskTx { baud: i.baud, ..PskTx::default() };
        let other = modulate(&symbols(&i.text, &tx), &tx, case.rate, case.centre + i.offset_hz, case.drift);
        let p = if other.is_empty() { 1.0 } else { other.iter().map(|z| z.norm_sqr()).sum::<f64>() / other.len() as f64 };
        let g = (mean.max(1e-12) * db_to_ratio(i.level_db) / p).sqrt();
        // It starts with the lead, so the wanted signal arrives beside it.
        for (k, z) in other.iter().enumerate() {
            if let Some(y) = x.get_mut(k) {
                *y += z * g;
            }
        }
    }
    (x, mean)
}

/// The case as real audio: the real part at peak amplitude 0.5, plus white
/// noise for the SNR in 2.5 kHz.
pub fn psk_audio(case: &PskCase) -> Vec<f32> {
    let mut rng = Rng::new(case.seed);
    let (x, mean) = clean(case, &mut rng);
    let sigma = case
        .snr_db
        .map(|snr| {
            let p_sig = AMP * AMP * mean.max(1e-12) / 2.0;
            (p_sig / db_to_ratio(snr) * (case.rate / 2.0) / SNR_BANDWIDTH).sqrt()
        })
        .unwrap_or(0.0);
    let mut noise = Rng::new(case.seed ^ 0x6e6f_6973_6521);
    x.iter().map(|z| (AMP * z.re + sigma * noise.gauss()) as f32).collect()
}

/// The case as complex baseband IQ, complex noise for the SNR in 2.5 kHz.
pub fn psk_iq(case: &PskCase) -> Vec<Complex32> {
    let mut rng = Rng::new(case.seed);
    let (x, mean) = clean(case, &mut rng);
    let sigma = case
        .snr_db
        .map(|snr| {
            let p_sig = AMP * AMP * mean.max(1e-12);
            (p_sig / db_to_ratio(snr) * case.rate / SNR_BANDWIDTH).sqrt()
        })
        .unwrap_or(0.0);
    let mut noise = Rng::new(case.seed ^ 0x6e6f_6973_6521);
    x.iter()
        .map(|z| {
            let n = noise.cgauss() * sigma;
            Complex32::new((AMP * z.re + n.re) as f32, (AMP * z.im + n.im) as f32)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_is_two_tones_half_the_baud_apart() {
        let tx = PskTx { preamble_s: 2.0, postamble_s: 0.0, ..Default::default() };
        assert!(symbols("", &tx).is_empty());
        let syms: Vec<f64> = (0..62).map(|k| if k % 2 == 0 { -1.0 } else { 1.0 }).collect();
        assert!(syms.windows(2).all(|w| w[0] == -w[1]));
        let x = modulate(&syms, &tx, 12_000.0, 0.0, 0.0);
        // Away from the ends, the baseband is cos(pi t / T) up to sign: all
        // its power at +-15.625 Hz.
        let mid = &x[3072..3072 + 768 * 20]; // whole cycles of both tones
        let tone = |f: f64| {
            let s: Complex64 = mid
                .iter()
                .enumerate()
                .map(|(i, z)| z * Complex64::from_polar(1.0, -2.0 * std::f64::consts::PI * f * i as f64 / 12_000.0))
                .sum();
            (s / mid.len() as f64).norm()
        };
        assert!((tone(15.625) - 0.5).abs() < 0.01 && (tone(-15.625) - 0.5).abs() < 0.01);
        assert!(tone(46.875) < 0.005);
    }

    #[test]
    fn the_imd_asked_for_is_the_imd_sent() {
        let tx = PskTx { preamble_s: 2.0, postamble_s: 0.0, imd_db: Some(-25.0), ..Default::default() };
        let syms: Vec<f64> = (0..62).map(|k| if k % 2 == 0 { -1.0 } else { 1.0 }).collect();
        let x = modulate(&syms, &tx, 12_000.0, 0.0, 0.0);
        let mid = &x[3072..3072 + 768 * 20]; // whole cycles of both tones
        let tone = |f: f64| {
            let s: Complex64 = mid
                .iter()
                .enumerate()
                .map(|(i, z)| z * Complex64::from_polar(1.0, -2.0 * std::f64::consts::PI * f * i as f64 / 12_000.0))
                .sum();
            (s / mid.len() as f64).norm()
        };
        let imd = 20.0 * (tone(46.875) / tone(15.625)).log10();
        assert!((imd + 25.0).abs() < 0.2, "{imd}");
    }

    #[test]
    fn the_snr_is_what_was_asked_for() {
        let case = PskCase { text: "CQ CQ DE GM4BRI".into(), snr_db: Some(-10.0), lead_s: 0.0, tail_s: 0.0, ..Default::default() };
        let s = psk_audio(&PskCase { snr_db: None, ..case.clone() });
        let both = psk_audio(&case);
        let p_sig = s.iter().map(|x| (*x as f64).powi(2)).sum::<f64>() / s.len() as f64;
        let p_noise = s.iter().zip(&both).map(|(a, b)| (*b as f64 - *a as f64).powi(2)).sum::<f64>() / s.len() as f64;
        let snr = 10.0 * (p_sig / (p_noise * SNR_BANDWIDTH / 6000.0)).log10();
        assert!((snr + 10.0).abs() < 0.15, "{snr}");
    }
}
