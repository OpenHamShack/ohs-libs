//! The broadcast FM receiver: a station's channel IQ (or its composite)
//! in, 48 kHz stereo PCM, the stereo state and RDS out. It replaces the
//! mono path of openhamshack-nb `ohscore/dsp/wfm.py`, and keeps that
//! module's scale (full deviation comes out at 0.5) and its 20 Hz DC
//! high-pass, so Radio's levels do not move.
//!
//! **Why IQ in, not the composite.** Radio already has the channel IQ (the
//! bank's 240 kS/s channel), and the discriminator is one `atan2` a sample;
//! taking IQ puts the whole path in Rust off the GIL, gives the S-meter
//! level and the tuning offset from the same pass, and leaves Radio one
//! call where it would otherwise demodulate in numpy and hand over the
//! result. `process_mpx` takes a composite too (another app's
//! discriminator output, or a test's).
//!
//! The path, at the channel rate R (a multiple of 48 kHz for audio:
//! Radio's 240 kS/s is 5 x 48):
//!
//! - **Discriminator:** angle(x[n] conj x[n-1]), 75 kHz = 1.0 (wfm.py's).
//! - **Pilot PLL:** a second-order loop (about 8 Hz) on the 19 kHz pilot,
//!   its phase detector's arms low-passed twice at 300 Hz so programme
//!   4 kHz away from the pilot does not pull it. The pilot's amplitude,
//!   averaged over a fifth of a second, is the stereo detector: on above
//!   4 % of deviation (a station's pilot is 8-10 %) for 150 ms, off below
//!   3 % for 300 ms, so a fade or a burst of noise does not flick the
//!   indicator.
//! - **Stereo:** L-R is the composite times 2 sin(2 th), th the PLL's
//!   phase, so the 38 kHz reference is the pilot's own second harmonic.
//!   Sum and difference are de-emphasised (50 or 75 µs, one pole, at R,
//!   where the impulse-invariant pole is within 0.1 dB of the analogue one
//!   to 15 kHz) and decimated to 48 kHz by one Kaiser low-pass, flat to
//!   15 kHz and 70 dB down from 19 kHz, so the pilot, L-R's carrier
//!   products and RDS never reach the audio. Both go through the same
//!   filter, so their delays match and separation is not lost to it.
//! - **Blend:** the composite's noise between 64 and 84 kHz, where nothing
//!   is transmitted and an FM discriminator's noise is about the strength
//!   it is in L-R (it rises with frequency), estimates the stereo channel's
//!   SNR. Below `BLEND_LO_DB` the difference is faded out (mono); above
//!   `BLEND_HI_DB` it is all in; it falls in a tenth of a second and comes
//!   back over half a second, so a flutter never pumps. No pilot, or `stereo`
//!   off, is mono.
//! - **RDS** taps the composite before any of that (rdsdemod.rs, rds.rs),
//!   so it works in mono, with no pilot, and with audio off (the band
//!   scanner's way).

use super::filt::{kaiser_lowpass, one_pole, step, turns, RealDecim, SinCos};
use super::rds::{Fields, RdsDecoder};
use super::rdsdemod::RdsDemod;
use super::tx::{DEVIATION, PILOT_HZ};
use crate::dsp::{lowpass, Decimator};
use rustfft::num_complex::Complex32;

pub const LEVEL: f32 = 0.5;
pub const AUDIO_RATE: u32 = 48_000;
const PILOT_ON: f64 = 0.04;
const PILOT_OFF: f64 = 0.03;
const PILOT_ON_S: f64 = 0.15;
const PILOT_OFF_S: f64 = 0.3;
/// The stereo channel's estimated SNR (dB, unweighted, de-emphasised, to
/// full deviation in one channel) below which it is mono, and above which
/// fully stereo. Mono is 20 dB quieter than stereo on the same signal
/// (tests/wfm.rs measures it), so at 26 dB the mono listener still has
/// about 46; full stereo at 40 dB is a carrier-to-noise ratio of about
/// 24 dB in the sampled band, a good signal.
pub const BLEND_LO_DB: f64 = 26.0;
pub const BLEND_HI_DB: f64 = 40.0;
/// The noise estimate's band: centre and half width.
const HF_HZ: f64 = 74_000.0;
const HF_HALF: f64 = 10_000.0;

#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// De-emphasis time constant, µs: 50 (Europe), 75 (the Americas), 0 off.
    pub deemph_us: f64,
    /// Decode stereo when there is a pilot; false is always mono.
    pub stereo: bool,
    /// Fade to mono as the signal weakens (the blend); false keeps full
    /// stereo whenever there is a pilot, however noisy.
    pub blend: bool,
    pub rds: bool,
    /// Make audio at all (the scanner wants only RDS and the pilot).
    pub audio: bool,
    /// 48000, 24000 or 12000.
    pub audio_rate: u32,
    /// 2, or 1 for (L+R)/2.
    pub channels: u8,
}

impl Default for Config {
    fn default() -> Self {
        Config { deemph_us: 50.0, stereo: true, blend: true, rds: true, audio: true, audio_rate: AUDIO_RATE, channels: 2 }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// Audio needs R a whole multiple of 48 kHz, at least 144 kS/s; RDS
    /// alone needs at least 128 kS/s.
    Rate,
    AudioRate,
    Channels,
    Deemphasis,
}

impl ConfigError {
    pub fn code(&self) -> &'static str {
        match self {
            ConfigError::Rate => "rate",
            ConfigError::AudioRate => "audio_rate",
            ConfigError::Channels => "channels",
            ConfigError::Deemphasis => "deemphasis",
        }
    }
}

/// What the receiver says about the signal now.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Status {
    pub stereo: bool,
    /// 0 mono .. 1 full stereo.
    pub blend: f32,
    /// The pilot's amplitude, a fraction of full deviation.
    pub pilot: f32,
    /// The stereo channel's estimated SNR, dB; None where R is too low to
    /// see above 84 kHz.
    pub snr_db: Option<f32>,
    /// Channel power, dBFS (IQ input only).
    pub level_db: Option<f32>,
    /// The carrier's offset from the tuning, Hz (the composite's mean).
    pub offset_hz: f32,
    pub rds_sync: bool,
    pub rds_snr_db: f32,
    pub rds_counts: super::rds::Counts,
}

struct Pilot {
    phase: u32,
    step0: u32,
    freq: f64,
    i1: f32,
    i2: f32,
    q1: f32,
    q2: f32,
    arm: f32,
    kp: f64,
    ki: f64,
    max_freq: f64,
    amp: f64,
    amp_k: f64,
}

impl Pilot {
    fn new(rate: f64) -> Self {
        let wn = 2.0 * std::f64::consts::PI * 8.0 / rate;
        Pilot {
            phase: 0,
            step0: step(PILOT_HZ, rate),
            freq: 0.0,
            i1: 0.0,
            i2: 0.0,
            q1: 0.0,
            q2: 0.0,
            arm: one_pole(300.0, rate),
            // In turns per sample per radian of error.
            kp: 2.0 * 0.707 * wn / (2.0 * std::f64::consts::PI),
            ki: wn * wn / (2.0 * std::f64::consts::PI),
            // ±20 Hz: the pilot is held to ±2 Hz, a receiver's clock adds a few.
            max_freq: 20.0 / rate,
            amp: 0.0,
            amp_k: one_pole(1.0, rate) as f64,
        }
    }
}

struct Hf {
    phase: u32,
    step: u32,
    a: f32,
    z1: Complex32,
    z2: Complex32,
    p: f64,
    k: f64,
}

/// Output-rate conversion after 48 kHz, on (L, R) pairs.
struct Post {
    dec: Decimator,
}

pub struct WfmReceiver {
    rate: f64,
    cfg: Config,
    sc: SinCos,
    prev: Complex32,
    scale: f32,
    pilot: Pilot,
    stereo: bool,
    cond_samples: u64,
    hold_on: u64,
    hold_off: u64,
    hf: Option<Hf>,
    snr_db: Option<f64>,
    blend: f32,
    blend_up: f32,
    blend_down: f32,
    de_k: f32,
    ys: f32,
    yd: f32,
    dec_s: Option<RealDecim>,
    dec_d: Option<RealDecim>,
    post: Option<Post>,
    dc: [f32; 4],
    dc_r: f32,
    level: Option<f64>,
    level_k: f64,
    mean: f64,
    mean_k: f64,
    rds: Option<(RdsDemod, RdsDecoder)>,
    bits: Vec<(bool, f32)>,
    samples: u64,
    /// Ours rises on any RDS change, the stereo flag's, and a reset; the
    /// decoder's (`dec_rev`, last seen) only on its own fields.
    rds_rev: u64,
    dec_rev: u64,
    rds_ts: u64,
    mpx: Vec<f32>,
    sbuf: Vec<f32>,
    dbuf: Vec<f32>,
    s48: Vec<f32>,
    d48: Vec<f32>,
    lr: Vec<Complex32>,
    lr2: Vec<Complex32>,
}

impl WfmReceiver {
    pub fn new(rate: f64, cfg: Config) -> Result<Self, ConfigError> {
        if !(rate.is_finite() && rate >= 128_000.0) {
            return Err(ConfigError::Rate);
        }
        let d = rate / AUDIO_RATE as f64;
        if cfg.audio && (rate < 144_000.0 || (d - d.round()).abs() > 1e-9) {
            return Err(ConfigError::Rate);
        }
        if ![48_000, 24_000, 12_000].contains(&cfg.audio_rate) {
            return Err(ConfigError::AudioRate);
        }
        if cfg.channels != 1 && cfg.channels != 2 {
            return Err(ConfigError::Channels);
        }
        if !(0.0..=200.0).contains(&cfg.deemph_us) {
            return Err(ConfigError::Deemphasis);
        }
        let (dec_s, dec_d) = if cfg.audio {
            let n = ((4.6 * rate / 4_000.0).ceil() as usize) | 1;
            let h = kaiser_lowpass(n, 17_000.0, rate, 7.0);
            (Some(RealDecim::new(h.clone(), d.round() as usize)), Some(RealDecim::new(h, d.round() as usize)))
        } else {
            (None, None)
        };
        let post = if cfg.audio && cfg.audio_rate != AUDIO_RATE {
            let out = cfg.audio_rate as f64;
            let n = ((5.5 * 48_000.0 / (0.08 * out)).ceil() as usize) | 1;
            Some(Post { dec: Decimator::new(lowpass(n, 0.46 * out, 48_000.0), (48_000 / cfg.audio_rate) as usize) })
        } else {
            None
        };
        let hf = (rate / 2.0 > HF_HZ + HF_HALF + 2_000.0).then(|| Hf {
            phase: 0,
            step: step(-HF_HZ, rate),
            a: one_pole(HF_HALF * 0.8, rate),
            z1: Complex32::new(0.0, 0.0),
            z2: Complex32::new(0.0, 0.0),
            p: 0.0,
            k: one_pole(3.0, rate) as f64,
        });
        let mut r = WfmReceiver {
            rate,
            sc: SinCos::new(),
            prev: Complex32::new(1.0, 0.0),
            scale: (rate / (2.0 * std::f64::consts::PI) / DEVIATION) as f32,
            pilot: Pilot::new(rate),
            stereo: false,
            cond_samples: 0,
            hold_on: (PILOT_ON_S * rate) as u64,
            hold_off: (PILOT_OFF_S * rate) as u64,
            hf,
            snr_db: None,
            blend: 0.0,
            blend_up: (2.0 / AUDIO_RATE as f64) as f32,
            blend_down: (10.0 / AUDIO_RATE as f64) as f32,
            de_k: 1.0,
            ys: 0.0,
            yd: 0.0,
            dec_s,
            dec_d,
            post,
            dc: [0.0; 4],
            dc_r: (-2.0 * std::f64::consts::PI * 20.0 / AUDIO_RATE as f64).exp() as f32,
            level: None,
            level_k: one_pole(5.0, rate) as f64,
            mean: 0.0,
            mean_k: one_pole(0.5, rate) as f64,
            rds: cfg.rds.then(|| (RdsDemod::new(rate), RdsDecoder::new())),
            bits: Vec::new(),
            samples: 0,
            rds_rev: 0,
            dec_rev: 0,
            rds_ts: 0,
            mpx: Vec::new(),
            sbuf: Vec::new(),
            dbuf: Vec::new(),
            s48: Vec::new(),
            d48: Vec::new(),
            lr: Vec::new(),
            lr2: Vec::new(),
            cfg: cfg.clone(),
        };
        r.set_deemphasis(cfg.deemph_us)?;
        Ok(r)
    }

    pub fn rate(&self) -> f64 {
        self.rate
    }

    pub fn config(&self) -> &Config {
        &self.cfg
    }

    /// Live: the next sample uses the new time constant.
    pub fn set_deemphasis(&mut self, us: f64) -> Result<(), ConfigError> {
        if !(0.0..=200.0).contains(&us) {
            return Err(ConfigError::Deemphasis);
        }
        self.cfg.deemph_us = us;
        self.de_k = if us > 0.0 { 1.0 - (-1.0 / (us * 1e-6 * self.rate)).exp() as f32 } else { 1.0 };
        Ok(())
    }

    /// Live: false is always mono (the blend fades out).
    pub fn set_stereo(&mut self, on: bool) {
        self.cfg.stereo = on;
    }

    /// Live: whether to fade to mono on a weak signal.
    pub fn set_blend(&mut self, on: bool) {
        self.cfg.blend = on;
    }

    /// A retune: every filter and loop starts again and RDS is forgotten.
    pub fn reset(&mut self) {
        let cfg = self.cfg.clone();
        let syncs = self.rds.as_ref().map(|r| r.1.counts.syncs);
        let rev = self.rds_rev;
        *self = WfmReceiver::new(self.rate, cfg).expect("the same config was accepted before");
        // The revision keeps rising, so a reader sees the clearing as a change.
        self.rds_rev = rev + 1;
        if let (Some(s), Some((_, dec))) = (syncs, self.rds.as_mut()) {
            dec.counts.syncs = s;
        }
    }

    /// Samples taken since the start (or the last reset).
    pub fn samples(&self) -> u64 {
        self.samples
    }

    pub fn status(&self) -> Status {
        let (rds_sync, rds_snr_db, rds_counts) = match &self.rds {
            Some((d, r)) => (r.synced(), d.snr_db() as f32, r.counts),
            None => (false, 0.0, Default::default()),
        };
        Status {
            stereo: self.stereo,
            blend: self.blend,
            pilot: self.pilot.amp as f32,
            snr_db: self.snr_db.map(|s| s as f32),
            level_db: self.level.map(|p| (10.0 * p.max(1e-20).log10()) as f32),
            offset_hz: (self.mean * DEVIATION) as f32,
            rds_sync,
            rds_snr_db,
            rds_counts,
        }
    }

    /// RDS as published so far (stable fields only).
    pub fn rds(&self) -> Option<&Fields> {
        self.rds.as_ref().map(|r| r.1.fields())
    }

    /// Rises whenever an RDS field (or the stereo flag) changes; and the
    /// sample count at which it last did.
    pub fn rds_rev(&self) -> (u64, u64) {
        (self.rds_rev, self.rds_ts)
    }

    pub fn rds_demod(&self) -> Option<&RdsDemod> {
        self.rds.as_ref().map(|r| &r.0)
    }

    pub fn rds_decoder(&mut self) -> Option<&mut RdsDecoder> {
        self.rds.as_mut().map(|r| &mut r.1)
    }

    /// Channel IQ in; audio appended to `out`, interleaved when stereo.
    pub fn process_iq(&mut self, iq: &[Complex32], out: &mut Vec<f32>) {
        let mut mpx = std::mem::take(&mut self.mpx);
        mpx.clear();
        let mut p = 0.0f64;
        let mut prev = self.prev;
        for &z in iq {
            let d = z * prev.conj();
            mpx.push(d.im.atan2(d.re) * self.scale);
            p += z.norm_sqr() as f64;
            prev = z;
        }
        self.prev = prev;
        if !iq.is_empty() {
            let mean = p / iq.len() as f64;
            let k = 1.0 - (1.0 - self.level_k).powi(iq.len() as i32);
            self.level = Some(match self.level {
                Some(l) => l + k * (mean - l),
                None => mean,
            });
        }
        self.run(&mpx, out);
        self.mpx = mpx;
    }

    /// Composite in (75 kHz = 1.0); audio appended to `out`.
    pub fn process_mpx(&mut self, mpx: &[f32], out: &mut Vec<f32>) {
        self.run(mpx, out);
    }

    fn run(&mut self, mpx: &[f32], out: &mut Vec<f32>) {
        let audio = self.cfg.audio;
        self.sbuf.clear();
        self.dbuf.clear();
        let pl = &mut self.pilot;
        let sc = self.sc;
        let mut stereo = self.stereo;
        let mut cond = self.cond_samples;
        let mut flipped = false;
        let (de_k, mut ys, mut yd) = (self.de_k, self.ys, self.yd);
        let mut mean = self.mean;
        for &x in mpx {
            // The pilot loop.
            let (s, c) = sc.at(pl.phase);
            pl.i1 += pl.arm * (x * s - pl.i1);
            pl.i2 += pl.arm * (pl.i1 - pl.i2);
            pl.q1 += pl.arm * (x * c - pl.q1);
            pl.q2 += pl.arm * (pl.q1 - pl.q2);
            let err = pl.q2.atan2(pl.i2) as f64;
            pl.freq = (pl.freq + pl.ki * err).clamp(-pl.max_freq, pl.max_freq);
            pl.phase = pl.phase.wrapping_add(pl.step0).wrapping_add(turns(pl.freq + pl.kp * err));
            pl.amp += pl.amp_k * (2.0 * pl.i2 as f64 - pl.amp);
            // The detector, with its hysteresis in time and level.
            let want = if stereo { pl.amp > PILOT_OFF } else { pl.amp > PILOT_ON };
            if want != stereo {
                cond += 1;
                if cond >= if stereo { self.hold_off } else { self.hold_on } {
                    stereo = want;
                    flipped = true;
                    cond = 0;
                }
            } else {
                cond = 0;
            }
            mean += self.mean_k * (x as f64 - mean);
            if audio {
                // L-R from the pilot's second harmonic: sin 2th = 2 sin th cos th.
                let d = x * 4.0 * s * c;
                ys += de_k * (x - ys);
                yd += de_k * (d - yd);
                self.sbuf.push(ys);
                self.dbuf.push(yd);
            }
        }
        self.stereo = stereo;
        self.cond_samples = cond;
        self.ys = ys;
        self.yd = yd;
        self.mean = mean;
        if flipped {
            self.rds_rev += 1;
            self.rds_ts = self.samples + mpx.len() as u64;
        }
        if let Some(hf) = self.hf.as_mut() {
            for &x in mpx {
                let (s, c) = sc.at(hf.phase);
                hf.phase = hf.phase.wrapping_add(hf.step);
                let z = Complex32::new(x * c, x * s);
                hf.z1 += (z - hf.z1) * hf.a;
                hf.z2 += (hf.z1 - hf.z2) * hf.a;
                hf.p += hf.k * (hf.z2.norm_sqr() as f64 - hf.p);
            }
            self.snr_db = Some(snr_from_hf(hf.p));
        }
        if let Some((demod, dec)) = self.rds.as_mut() {
            self.bits.clear();
            demod.process(mpx, &mut self.bits);
            for &(b, c) in &self.bits {
                dec.push_soft(b, c);
            }
            let rev = dec.rev();
            if rev != self.dec_rev {
                self.dec_rev = rev;
                self.rds_rev += 1;
                self.rds_ts = self.samples + mpx.len() as u64;
            }
        }
        self.samples += mpx.len() as u64;
        if audio {
            self.audio(out);
        }
    }

    fn audio(&mut self, out: &mut Vec<f32>) {
        self.s48.clear();
        self.d48.clear();
        self.dec_s.as_mut().unwrap().process(&self.sbuf, &mut self.s48);
        self.dec_d.as_mut().unwrap().process(&self.dbuf, &mut self.d48);
        let target = match (self.stereo && self.cfg.stereo, self.snr_db) {
            (false, _) => 0.0,
            (true, _) if !self.cfg.blend => 1.0,
            (true, None) => 1.0,
            (true, Some(snr)) => ((snr - BLEND_LO_DB) / (BLEND_HI_DB - BLEND_LO_DB)).clamp(0.0, 1.0) as f32,
        };
        let mut b = self.blend;
        self.lr.clear();
        for (&s, &d) in self.s48.iter().zip(&self.d48) {
            b += if target > b { (target - b).min(self.blend_up) } else { -((b - target).min(self.blend_down)) };
            // The DC high-pass: a tuning offset is DC in the composite, and
            // only the sum keeps it at DC, but the difference goes through
            // the same filter: its 1° of phase at 1 kHz on the sum alone
            // limited separation to 40 dB.
            let y = s - self.dc[0] + self.dc_r * self.dc[1];
            let yd = d - self.dc[2] + self.dc_r * self.dc[3];
            self.dc = [s, y, d, yd];
            self.lr.push(Complex32::new(LEVEL * (y + b * yd), LEVEL * (y - b * yd)));
        }
        self.blend = b;
        let lr = if let Some(post) = self.post.as_mut() {
            self.lr2.clear();
            post.dec.process(&self.lr, &mut self.lr2);
            &self.lr2
        } else {
            &self.lr
        };
        if self.cfg.channels == 2 {
            out.reserve(2 * lr.len());
            for z in lr {
                out.push(z.re);
                out.push(z.im);
            }
        } else {
            out.extend(lr.iter().map(|z| 0.5 * (z.re + z.im)));
        }
    }
}

/// The stereo channel's SNR from the composite's noise power in the
/// 64-84 kHz probe (`Hf`). The discriminator's noise density rises as f²,
/// and the difference channel gathers it from 23-53 kHz with a 50 µs
/// de-emphasis after; measured against the stereo channel's own noise
/// from 8 to 40 dB of CNR (tests/wfm.rs
/// `the_snr_estimate_tracks_the_measured_stereo_noise`), the probe reads
/// 1 dB high throughout, which K takes off.
fn snr_from_hf(p: f64) -> f64 {
    const K: f64 = 1.26;
    -10.0 * (p * K).max(1e-20).log10()
}
