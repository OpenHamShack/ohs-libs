//! The AIS test transmitter and its channel: valid bursts as a ship's
//! transponder keys them (ITU-R M.1371-5 Annex 2), at any sample rate, on
//! either channel, with noise, a frequency error and overlapping bursts,
//! so the decoder is measured on what it will hear. Receive only: nothing
//! here reaches a radio; it makes numbers for tests and for Radio's
//! simulator.
//!
//! A burst is: 8 bits of power ramp, 24 bits of training (0101...), the
//! flag, the stuffed payload and FCS, the flag, and 8 bits of ramp down;
//! NRZI; GMSK at 9600 baud with BT 0.4 (the transmitter's figure, §2.3)
//! and modulation index 0.5 (±2400 Hz).
//!
//! Unlike testsig's RTTY and PSK, nothing here is pinned by a sha256: the
//! AIS tests check what is decoded, never the samples, so the Gaussian
//! pulse may use the platform's exp.

use super::hdlc::{burst_bits, nrzi_encode, pack};
use crate::testsig::{db_to_ratio, sin_cos_turns, Rng};
use rustfft::num_complex::{Complex32, Complex64};

pub const BAUD: f64 = 9600.0;
/// Each channel's centre, Hz from 162.000 MHz: 87B (A) and 88B (B).
pub const CHANNEL_A_HZ: f64 = -25_000.0;
pub const CHANNEL_B_HZ: f64 = 25_000.0;
/// The bandwidth the test SNR is measured in: the bit rate, so the SNR is
/// Eb/N0.
pub const SNR_BANDWIDTH: f64 = 9600.0;
const DEVIATION_HZ: f64 = 2400.0;
const RAMP_BITS: usize = 8;

/// Which AIS channel.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Channel {
    A,
    B,
}

impl Channel {
    pub fn hz(self) -> f64 {
        match self {
            Channel::A => CHANNEL_A_HZ,
            Channel::B => CHANNEL_B_HZ,
        }
    }
    pub fn code(self) -> char {
        match self {
            Channel::A => 'A',
            Channel::B => 'B',
        }
    }
    pub fn parse(c: &str) -> Option<Channel> {
        match c {
            "A" | "a" => Some(Channel::A),
            "B" | "b" => Some(Channel::B),
            _ => None,
        }
    }
}

/// One burst on the air.
#[derive(Clone, Debug)]
pub struct Burst {
    /// When its first ramp bit starts, seconds into the scene.
    pub t_s: f64,
    pub channel: Channel,
    /// The payload's bits, MSB-first (msg::encode's output).
    pub bits: Vec<u8>,
    /// Its power, dB over the 0 dB the scene's SNR is quoted against.
    pub level_db: f64,
    /// The transmitter's frequency error, Hz.
    pub offset_hz: f64,
    /// On-air bits (before NRZI, counted from the ramp's first) to invert:
    /// a damaged burst.
    pub flip_bits: Vec<usize>,
}

impl Burst {
    pub fn new(t_s: f64, channel: Channel, bits: Vec<u8>) -> Self {
        Burst { t_s, channel, bits, level_db: 0.0, offset_hz: 0.0, flip_bits: Vec::new() }
    }
    /// Seconds on air.
    pub fn duration_s(&self) -> f64 {
        on_air_bits(&self.bits).len() as f64 / BAUD
    }
}

/// A stretch of the band: bursts, and the noise under them.
#[derive(Clone, Debug)]
pub struct Scene {
    pub rate: f64,
    pub duration_s: f64,
    /// Where 162.000 MHz sits, Hz from the IQ's centre.
    pub centre_hz: f64,
    /// Signal (a 0 dB burst) to noise in SNR_BANDWIDTH; None is no noise.
    pub snr_db: Option<f64>,
    pub seed: u64,
    pub bursts: Vec<Burst>,
}

fn on_air_bits(payload: &[u8]) -> Vec<u8> {
    burst_bits(&pack(payload), RAMP_BITS, RAMP_BITS)
}

/// The Gaussian-filtered rectangular frequency pulse, one bit wide, at `t`
/// bits from the bit's centre: 1 in the middle of a long run.
fn pulse(t: f64, bt: f64) -> f64 {
    let a = std::f64::consts::PI * bt * (2.0 / std::f64::consts::LN_2).sqrt();
    0.5 * (erf(a * (t + 0.5)) - erf(a * (t - 0.5)))
}

/// erf to about 1e-7 (Abramowitz and Stegun 7.1.26), plenty for a pulse shape.
fn erf(x: f64) -> f64 {
    let s = x.signum();
    let x = x.abs();
    let t = 1.0 / (1.0 + 0.327_591_1 * x);
    let y = 1.0
        - (((((1.061_405_429 * t - 1.453_152_027) * t) + 1.421_413_741) * t - 0.284_496_736) * t + 0.254_829_592)
            * t
            * (-x * x).exp();
    s * y
}

/// One burst as complex baseband at `rate`, its carrier at `freq_hz`,
/// amplitude `amp`. `flip` corrupts on-air bits (indices into the bits
/// before NRZI).
pub fn burst_iq(payload: &[u8], rate: f64, freq_hz: f64, amp: f64, flip: &[usize]) -> Vec<Complex64> {
    let mut bits = on_air_bits(payload);
    for &i in flip {
        if i < bits.len() {
            bits[i] ^= 1;
        }
    }
    // NRZI levels to +-1 symbols.
    let sym: Vec<f64> = nrzi_encode(&bits, 0).into_iter().map(|l| if l == 1 { 1.0 } else { -1.0 }).collect();
    let nb = sym.len();
    let n = (nb as f64 * rate / BAUD).ceil() as usize;
    let bt = 0.4;
    let mut out = Vec::with_capacity(n);
    let mut phase = 0.0f64; // turns
    let mut carrier = 0.0f64;
    let ramp = RAMP_BITS as f64;
    for i in 0..n {
        let tb = i as f64 * BAUD / rate; // time in bits
        let k0 = (tb - 3.0).floor().max(0.0) as usize;
        let k1 = ((tb + 3.0).ceil() as usize).min(nb);
        let mut fdev = 0.0;
        for (k, s) in sym.iter().enumerate().take(k1).skip(k0) {
            fdev += s * pulse(tb - (k as f64 + 0.5), bt);
        }
        // Power ramps up over the first 8 bits and down over the last 8.
        let a = if tb < ramp {
            0.5 - 0.5 * sin_cos_turns(tb / ramp / 2.0).1
        } else if tb > nb as f64 - ramp {
            0.5 - 0.5 * sin_cos_turns((nb as f64 - tb).max(0.0) / ramp / 2.0).1
        } else {
            1.0
        };
        let (s, c) = sin_cos_turns(phase + carrier);
        out.push(Complex64::new(amp * a * c, amp * a * s));
        phase += fdev * DEVIATION_HZ / rate;
        carrier += freq_hz / rate;
        phase -= phase.floor();
        carrier -= carrier.floor();
    }
    out
}

/// The scene as complex IQ at its rate: every burst at its time, channel,
/// level and offset, over complex Gaussian noise.
pub fn scene_iq(s: &Scene) -> Vec<Complex32> {
    let n = (s.duration_s * s.rate).round() as usize;
    let mut acc = vec![Complex64::new(0.0, 0.0); n];
    for b in &s.bursts {
        let f = s.centre_hz + b.channel.hz() + b.offset_hz;
        let amp = db_to_ratio(b.level_db).sqrt();
        let x = burst_iq(&b.bits, s.rate, f, amp, &b.flip_bits);
        // The carrier's phase at the burst's start follows from its time, so
        // two bursts of one station are not phase-locked to the scene.
        let start = (b.t_s * s.rate).round() as usize;
        for (i, z) in x.into_iter().enumerate() {
            if let Some(a) = acc.get_mut(start + i) {
                *a += z;
            }
        }
    }
    let sigma = match s.snr_db {
        Some(snr) => (s.rate / (db_to_ratio(snr) * SNR_BANDWIDTH)).sqrt(),
        None => 0.0,
    };
    let mut rng = Rng::new(s.seed);
    acc.into_iter()
        .map(|z| {
            let w = if sigma > 0.0 { rng.cgauss() * sigma } else { Complex64::new(0.0, 0.0) };
            Complex32::new((z.re + w.re) as f32, (z.im + w.im) as f32)
        })
        .collect()
}
