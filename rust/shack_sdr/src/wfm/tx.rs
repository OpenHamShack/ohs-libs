//! An FM stereo and RDS transmitter for tests (RADIO-SDR-PLAN §7.1: no
//! transmitter, so the signals are made here). It builds the composite
//! signal as ITU-R BS.450 lays it out and EN 62106 adds RDS to it, and
//! frequency-modulates a carrier with it:
//!
//!   mpx = a (L+R)/2 + p sin(th) + a (L-R)/2 sin(2 th) + r(t) sin(3 th | 3 th + 90°)
//!
//! with th the 19 kHz pilot's phase, `a` the audio's share of the
//! deviation, `p` the pilot's (9 % by default), and r(t) RDS's shaped
//! biphase symbols (rds::Station's groups, differentially coded) at
//! 57 kHz / 48 bit/s. L and R are pre-emphasised (50 or 75 µs) by the exact
//! inverse of the receiver's de-emphasis, so a tone comes back at the level
//! it went in. Everything clocked from one oscillator can be put off by
//! `clock_ppm`, as a real station's is, and the channel adds AWGN at a
//! carrier-to-noise ratio, a tuning offset and multipath echoes.
//!
//! Receive-only by construction still holds: this makes sample arrays for
//! tests, and nothing in the crate can send them anywhere.

use super::filt::{kaiser_lowpass, turns, RealDecim, SinCos};
use super::rds::{group_bits, Station};
use super::rdsdemod::{biphase, BITRATE};
use crate::testsig::Rng;
use rustfft::num_complex::Complex32;

pub const PILOT_HZ: f64 = 19_000.0;
pub const DEVIATION: f64 = 75_000.0;

/// One channel's programme.
#[derive(Clone, Debug)]
pub enum Audio {
    Silence,
    /// A tone, amplitude 1.0 = full scale.
    Tone { hz: f64, amp: f64 },
    /// Gaussian noise falling 6 dB an octave above 1 kHz and band-limited
    /// to 15 kHz, `amp` its RMS: a stand-in for music, whose spectrum falls
    /// like that, so pre-emphasis does not drive it far past full deviation.
    Noise { amp: f64, seed: u64 },
}

#[derive(Clone, Debug)]
pub struct Tx {
    pub rate: f64,
    pub left: Audio,
    pub right: Audio,
    /// The audio's share of the deviation (`a` above).
    pub audio: f64,
    /// The pilot's share; 0 sends mono (no pilot, no L-R).
    pub pilot: f64,
    pub preemph_us: f64,
    pub rds: Option<Station>,
    pub rds_level: f64,
    /// RDS in quadrature with the pilot's third harmonic, which the
    /// standard also allows.
    pub rds_quadrature: bool,
    pub clock_ppm: f64,
}

impl Tx {
    pub fn new(rate: f64) -> Self {
        Tx {
            rate,
            left: Audio::Silence,
            right: Audio::Silence,
            audio: 0.87,
            pilot: 0.09,
            preemph_us: 50.0,
            rds: None,
            rds_level: 0.04,
            rds_quadrature: false,
            clock_ppm: 0.0,
        }
    }
}

fn audio(a: &Audio, rate: f64, n: usize) -> Vec<f32> {
    match a {
        Audio::Silence => vec![0.0; n],
        Audio::Tone { hz, amp } => {
            (0..n).map(|i| (amp * (2.0 * std::f64::consts::PI * hz * i as f64 / rate).sin()) as f32).collect()
        }
        Audio::Noise { amp, seed } => {
            let mut rng = Rng::new(*seed);
            let h = kaiser_lowpass(255, 15_000.0, rate, 7.0);
            let k = (1.0 - (-2.0 * std::f64::consts::PI * 1_000.0 / rate).exp()) as f32;
            let mut lp = 0.0f32;
            let x: Vec<f32> = (0..n + 2 * 254)
                .map(|_| {
                    lp += k * (rng.gauss() as f32 - lp);
                    lp
                })
                .collect();
            let mut f = RealDecim::new(h.clone(), 1);
            let mut y = Vec::with_capacity(n + 2 * 254);
            f.process(&x, &mut y);
            let y = &y[254.min(y.len())..];
            let rms = (y.iter().map(|v| (v * v) as f64).sum::<f64>() / y.len().max(1) as f64).sqrt();
            let mut y: Vec<f32> = y.iter().map(|v| (*v as f64 / rms * amp) as f32).collect();
            y.resize(n, 0.0);
            y
        }
    }
}

/// Pre-emphasis: the inverse of the receiver's one-pole de-emphasis at
/// the same rate (stereo.rs `Deemphasis`).
fn preemphasis(x: &mut [f32], tau_us: f64, rate: f64) {
    if tau_us <= 0.0 {
        return;
    }
    let a = (-1.0 / (tau_us * 1e-6 * rate)).exp() as f32;
    let mut prev = 0.0f32;
    for v in x.iter_mut() {
        let y = *v;
        *v = (y - a * prev) / (1.0 - a);
        prev = y;
    }
}

/// The composite signal, deviation-normalised (1.0 = 75 kHz), `seconds` long.
pub fn mpx(tx: &Tx, seconds: f64) -> Vec<f32> {
    let n = (seconds * tx.rate).round() as usize;
    let mut l = audio(&tx.left, tx.rate, n);
    let mut r = audio(&tx.right, tx.rate, n);
    preemphasis(&mut l, tx.preemph_us, tx.rate);
    preemphasis(&mut r, tx.preemph_us, tx.rate);
    let k = 1.0 + tx.clock_ppm * 1e-6;
    let fp = PILOT_HZ * k;
    // RDS: the data bits, differentially coded, enough for the whole signal.
    let tb = 1.0 / (BITRATE * k);
    let nbits = (seconds / tb).ceil() as usize + 8;
    let mut sym: Vec<f32> = Vec::new();
    if let Some(st) = &tx.rds {
        let mut prev = false;
        let mut g = 0;
        while sym.len() < nbits {
            for b in group_bits(st.group(g)) {
                prev ^= b;
                sym.push(if prev { 1.0 } else { -1.0 });
            }
            g += 1;
        }
    }
    let sc = SinCos::new();
    let dth = fp / tx.rate;
    let shift = if tx.rds_quadrature { 0.25 } else { 0.0 };
    // The symbol's peak, so `rds_level` is the peak of RDS's share.
    let peak = (0..400).map(|j| biphase((j as f64 / 400.0 - 0.25) / BITRATE).abs()).fold(0.0, f64::max);
    (0..n)
        .map(|i| {
            let t = i as f64 / tx.rate;
            let th = dth * i as f64;
            let (s1, _) = sc.at(turns(th));
            let (s2, _) = sc.at(turns(2.0 * th));
            let (s3, _) = sc.at(turns(3.0 * th + shift));
            let m = 0.5 * (l[i] + r[i]) as f64;
            let d = 0.5 * (l[i] - r[i]) as f64;
            let mut v = tx.audio * m;
            if tx.pilot > 0.0 {
                v += tx.pilot * s1 as f64 + tx.audio * d * s2 as f64;
            }
            if !sym.is_empty() {
                // Each bit's symbol reaches about three bits either side.
                let kc = (t / tb).floor() as i64;
                let mut rds = 0.0;
                for kb in (kc - 3).max(0)..=(kc + 3) {
                    rds += sym[kb as usize] as f64 * biphase((t - kb as f64 * tb) * k);
                }
                v += tx.rds_level * rds / peak * s3 as f64;
            }
            v as f32
        })
        .collect()
}

/// Frequency modulation of `mpx` (at `rate`) at ±75 kHz for 1.0, unit
/// amplitude.
pub fn fm(mpx: &[f32], rate: f64) -> Vec<Complex32> {
    let sc = SinCos::new();
    let mut ph = 0u32;
    mpx.iter()
        .map(|&m| {
            ph = ph.wrapping_add(turns(DEVIATION * m as f64 / rate));
            let (s, c) = sc.at(ph);
            Complex32::new(c, s)
        })
        .collect()
}

/// What happens between transmitter and receiver.
#[derive(Clone, Debug, Default)]
pub struct Channel {
    /// Carrier to noise, dB, the noise measured over the whole sampled
    /// band (`rate`); None adds none.
    pub cnr_db: Option<f64>,
    /// The receiver tuned this far off the carrier, Hz.
    pub offset_hz: f64,
    /// Echoes: (delay in samples, amplitude, phase in turns).
    pub echoes: Vec<(usize, f64, f64)>,
    pub seed: u64,
}

pub fn channel(x: &[Complex32], rate: f64, ch: &Channel) -> Vec<Complex32> {
    let sc = SinCos::new();
    let mut y: Vec<Complex32> = x.to_vec();
    for &(delay, amp, turn) in &ch.echoes {
        let (s, c) = sc.at(turns(turn));
        let g = Complex32::new(c, s) * amp as f32;
        for i in (delay..x.len()).rev() {
            y[i] += x[i - delay] * g;
        }
    }
    if ch.offset_hz != 0.0 {
        let step = turns(-ch.offset_hz / rate);
        let mut ph = 0u32;
        for v in y.iter_mut() {
            let (s, c) = sc.at(ph);
            *v *= Complex32::new(c, s);
            ph = ph.wrapping_add(step);
        }
    }
    if let Some(cnr) = ch.cnr_db {
        let p: f64 = y.iter().map(|z| z.norm_sqr() as f64).sum::<f64>() / y.len().max(1) as f64;
        let sigma = (p / 10f64.powf(cnr / 10.0)).sqrt();
        let mut rng = Rng::new(ch.seed);
        for v in y.iter_mut() {
            let n = rng.cgauss();
            *v += Complex32::new((n.re * sigma) as f32, (n.im * sigma) as f32);
        }
    }
    y
}

/// One station in a synthetic band: where, how strong (dBFS), and what it
/// sends (its `Tx`, whose rate is the composite's, about 250 kS/s).
#[derive(Clone, Debug)]
pub struct BandStation {
    pub offset_hz: f64,
    pub level_db: f64,
    pub tx: Tx,
}

/// A wide capture holding several stations: each one's composite made at
/// its own rate and carried to `rate` by windowed-sinc interpolation as it
/// is frequency-modulated, then shifted to its offset and scaled; with
/// complex noise of `noise_db` dBFS over the whole band. Linear
/// interpolation was tried first: its images of the composite, about 43 dB
/// down, frequency-modulated the strong stations' carriers 250 kHz either
/// side, and raised a 12-station band's apparent floor by 9 dB.
pub fn band(rate: f64, seconds: f64, stations: &[BandStation], noise_db: f64, seed: u64) -> Vec<Complex32> {
    let n = (seconds * rate).round() as usize;
    let mut out = vec![Complex32::new(0.0, 0.0); n];
    let sc = SinCos::new();
    for st in stations {
        let m = mpx(&st.tx, seconds + 0.01);
        let interp = Interp::new(0.4);
        let ratio = st.tx.rate / rate;
        let amp = 10f64.powf(st.level_db / 20.0) as f32;
        let off = turns(st.offset_hz / rate);
        let (mut ph, mut carrier) = (0u32, 0u32);
        for (i, o) in out.iter_mut().enumerate() {
            let v = interp.at(&m, i as f64 * ratio);
            ph = ph.wrapping_add(turns(DEVIATION * v as f64 / rate));
            let (s, c) = sc.at(ph.wrapping_add(carrier));
            carrier = carrier.wrapping_add(off);
            *o += Complex32::new(c, s) * amp;
        }
    }
    let sigma = 10f64.powf(noise_db / 20.0);
    let mut rng = Rng::new(seed);
    for o in out.iter_mut() {
        let z = rng.cgauss();
        *o += Complex32::new((z.re * sigma) as f32, (z.im * sigma) as f32);
    }
    out
}

/// Band-limited interpolation of a signal at any fractional position: a
/// 16-tap Kaiser-windowed sinc (cut off at `fc` of the signal's own rate),
/// tabulated at 256 phases. Images fall more than 70 dB.
struct Interp {
    table: Vec<[f32; 16]>,
}

impl Interp {
    const PHASES: usize = 256;

    fn new(fc: f64) -> Self {
        let beta = 7.0;
        let i0 = |x: f64| {
            let (mut sum, mut term) = (1.0, 1.0);
            for k in 1..40 {
                term *= (x / 2.0).powi(2) / (k * k) as f64;
                sum += term;
            }
            sum
        };
        let table = (0..Self::PHASES)
            .map(|p| {
                let frac = p as f64 / Self::PHASES as f64;
                let mut taps = [0.0f32; 16];
                let mut sum = 0.0;
                for (j, t) in taps.iter_mut().enumerate() {
                    // Tap j is sample k + j - 7 for position k + frac.
                    let x = j as f64 - 7.0 - frac;
                    let sinc = if x.abs() < 1e-12 { 2.0 * fc } else { (2.0 * std::f64::consts::PI * fc * x).sin() / (std::f64::consts::PI * x) };
                    let r = x / 8.5;
                    let w = i0(beta * (1.0 - r * r).max(0.0).sqrt()) / i0(beta);
                    *t = (sinc * w) as f32;
                    sum += sinc * w;
                }
                taps.iter_mut().for_each(|t| *t = (*t as f64 / sum) as f32);
                taps
            })
            .collect();
        Interp { table }
    }

    fn at(&self, x: &[f32], pos: f64) -> f32 {
        let k = pos.floor() as isize;
        let p = (((pos - k as f64) * Self::PHASES as f64) as usize).min(Self::PHASES - 1);
        let taps = &self.table[p];
        let mut v = 0.0;
        for (j, t) in taps.iter().enumerate() {
            let i = k + j as isize - 7;
            if i >= 0 && (i as usize) < x.len() {
                v += x[i as usize] * t;
            }
        }
        v
    }
}
