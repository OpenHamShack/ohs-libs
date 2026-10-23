//! PSK31, PSK63 and PSK125 (RADIO-SDR-PLAN §8.5 M2): differential BPSK
//! with Varicode, several channels in one passband. IQ natively; the host
//! adapts audio (dsp::AudioToIq) as for RTTY, so one decoder serves Radio's
//! channel IQ and Icom's or Flex's receive audio.
//!
//! It replaces openhamshack-nb `ohscore/modes/psk.py`'s Decoder and is held
//! to "never worse" than it on the golden numbers captured from it
//! (testdata/psk/, tools/psk_golden.py). It keeps psk.py's shape - mix to
//! baseband, matched filter with a zero-forcing correction, a coarse AFC
//! that squares away the data and finds the tone in an FFT, symbol timing
//! from the envelope - and changes what decides the bits:
//!
//! - **Coherent detection where it helps.** psk.py compares each symbol's
//!   phase with the last one's, so both symbols' noise lands in every
//!   decision. Here a Costas loop follows the carrier (and, through the AFC,
//!   its frequency), and a centred average of the squared symbols either
//!   side - 0.2 s each way, the symbol itself left out - is the phase
//!   reference each symbol is read against; the bit is whether two
//!   neighbours agree. Where the reference's own symbols disagree - the phase turning
//!   through the null of a fast fade - the bit is read differentially, as
//!   psk.py does, and between the two a mix.
//! - **Soft bits into a Varicode trellis** (varicode.rs) instead of cutting
//!   hard bits at "00": a weak bit is settled by what the characters around
//!   it make likely.
//! - **The squelch decides, for every character, whether a signal was
//!   there.** psk.py's quality average lags by a second, so it printed
//!   noise for a second after every over - nearly all of its errors on a
//!   strong signal - and missed weak overs entirely. Here each bit says how
//!   much likelier it is with a signal of the squelch's strength than with
//!   noise alone, and a two-state model (on or off, rarely changing) weighs
//!   every bit before a character and the trellis's second of bits after it.
//!   A character prints only if "on" is likelier on every one of its bits:
//!   a weak over is carried by all of it, and a character half in the noise
//!   at either end is refused. Fifteen minutes of noise print nothing.
//! - **A look-back.** The decimated samples of the last three seconds are
//!   kept; when the coarse AFC finds the signal somewhere else, they are
//!   decoded again at the new frequency, so the first characters of an over
//!   that began off-tune are not lost (RTTY's replay, rtty/mod.rs).
//!
//! Together: the character error rate psk.py reaches at a given SNR, this
//! reaches 2-3 dB lower, at every speed (tests/psk.rs prints the sweep).
//!
//! Several channels share one session (`channels`, Hz from the session's
//! centre): a waterfall-click decoder's signals, each its own mixer,
//! decimator, loops and trellis. Every character is its own result, with
//! the time of its first symbol and the channel's frequency, so the app
//! routes it to the channel nearest `offsetHz`.
//!
//! - `varicode`: the alphabet and its soft-decision decoder.
//! - `tx`: the test transmitter and channel the decoder is measured on.

pub mod tx;
pub mod varicode;

#[cfg(feature = "python")]
pub(crate) mod py;

use crate::decoder::{
    check_params, DecodeError, Decoder, Family, Info, Input, InputKind, Output, OutputKind, Panel, ParamDefault,
    ParamKind, ParamSpec, Params, Sink, Status, Value,
};
use crate::dsp::{lowpass, Decimator};
use rustfft::num_complex::{Complex32, Complex64};
use rustfft::{Fft, FftPlanner};
use std::collections::VecDeque;
use std::f64::consts::PI;
use std::sync::Arc;
use varicode::Viterbi;

pub const BAUDS: &[f64] = &[31.25, 62.5, 125.0];
/// The most channels one session decodes.
pub const MAX_CHANNELS: usize = 16;

const PARAMS: &[ParamSpec] = &[
    ParamSpec { key: "baud", kind: ParamKind::Choice(BAUDS), default: ParamDefault::Num(31.25) },
    // Comma-separated carrier frequencies, Hz from the session's centre
    // (one line: an app setting cannot hold a line break).
    ParamSpec { key: "channels", kind: ParamKind::Text, default: ParamDefault::Text("0") },
    ParamSpec { key: "afc", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
    ParamSpec { key: "afcRangeHz", kind: ParamKind::Range { min: 0.0, max: 100.0 }, default: ParamDefault::Num(15.0) },
    // dB the signal must stand over the noise in its own bandwidth (the
    // baud) before characters print; 0 turns it off.
    ParamSpec { key: "squelch", kind: ParamKind::Range { min: 0.0, max: 20.0 }, default: ParamDefault::Num(SQUELCH_DB) },
];

pub static INFO: Info = Info {
    id: "psk",
    family: Family::Amateur,
    inputs: &[InputKind::Iq],
    rate_hz: 12_000.0,
    // A passband of channels; one PSK31 signal needs about 60 Hz.
    bandwidth_hz: 3_000.0,
    outputs: &[OutputKind::Text, OutputKind::Meta],
    panel: Panel::Text,
    params: PARAMS,
    needs: &[],
    needs_for: &[],
    min_rate_hz: 0.0,
};

/// The squelch's default: the strength, dB of signal over noise in the
/// symbol's own bandwidth (Es/N0), of the signal its on/off model listens
/// for: not a hard floor, but how decisively each bit counts. 2 to 5
/// measure alike on the sweep; 3 copies the most (tests/psk.rs).
pub const SQUELCH_DB: f64 = 3.0;
/// The working rate is at least this many samples a symbol.
const SPS_MIN: f64 = 16.0;
/// Zero-forcing taps either side of the matched filter, a symbol apart.
const ZF_K: usize = 2;
/// Half the phase smoother's window, seconds: 6 symbols either side at
/// 31.25 Bd, 25 at 125.
const SMOOTH_S: f64 = 0.2;
/// The smoother's coherence below which a bit is read differentially, and
/// above which coherently; between, a mix. Measured on the sweep: fading
/// "poor" wants the differential reading in its nulls, AWGN never does.
const COHERENT_FROM: f64 = 0.7;
const COHERENT_FULL: f64 = 0.95;
/// Bits either side of a character its SNR is measured over.
const SNR_W: u64 = 16;
/// The trellis decides this many bits late.
const DEPTH: usize = 32;
/// Time constants, in symbols: symbol timing, and the amplitude and noise
/// estimates.
const TIMING_TAU: f64 = 16.0;
const STATS_TAU: f64 = 16.0;
/// The Costas loop's noise bandwidth, Hz (the same in seconds at every
/// speed: a drift of 0.5 Hz/s lags it by an eighth of a radian), and its
/// damping. The smoother takes out the jitter that bandwidth lets in.
const COSTAS_BN_HZ: f64 = 2.0;
const COSTAS_ZETA: f64 = 0.707;
/// What the Costas loop keeps of its frequency each symbol while there is no signal.
const NOISE_LEAK: f64 = 0.8;
/// The share of the Costas loop's frequency moved to the AFC each symbol.
const AFC_TRANSFER: f64 = 0.05;
/// Coarse AFC (psk.py's constants): an FFT of this many symbols, every this
/// many, believed when its peak stands this far over the median.
const ACQ_SYMS: usize = 32;
const ACQ_EVERY: usize = 8;
const ACQ_PROMINENCE: f32 = 20.0;
/// Symbols under the squelch before the coarse AFC may move again.
const LOST_SYMS: u32 = 64;
/// Seconds of decimated samples kept for the look-back.
const REPLAY_S: f64 = 3.0;
/// IMD is measured on runs of idle at least this long, over this many symbols.
const IMD_RUN: u32 = 20;
const IMD_SPAN: usize = 16;
/// Meta results at most this often, seconds of samples.
const META_EVERY_S: f64 = 1.0;

#[derive(Clone, Debug, PartialEq)]
struct Config {
    baud: f64,
    channels: Vec<f64>,
    afc: bool,
    afc_range: f64,
    squelch_db: f64,
}

impl Default for Config {
    fn default() -> Self {
        Config { baud: 31.25, channels: vec![0.0], afc: true, afc_range: 15.0, squelch_db: SQUELCH_DB }
    }
}

fn parse_channels(s: &str) -> Result<Vec<f64>, DecodeError> {
    let mut out = Vec::new();
    for part in s.split(',') {
        let x: f64 = part.trim().parse().map_err(|_| DecodeError::BadInput("bad_param"))?;
        if !x.is_finite() || out.contains(&x) {
            return Err(DecodeError::BadInput("bad_param"));
        }
        out.push(x);
    }
    if out.is_empty() || out.len() > MAX_CHANNELS {
        return Err(DecodeError::BadInput("bad_param"));
    }
    Ok(out)
}

/// The rate-dependent figures every channel of a session shares.
#[derive(Clone)]
struct Geometry {
    rate: f64,
    /// Decimation to the working rate.
    d: usize,
    work: f64,
    /// Samples a symbol at the working rate.
    sps: f64,
    /// The decimating filter, and its delay in input samples.
    taps: Vec<f32>,
    decim_delay: f64,
    /// Matched filter with its zero-forcing correction, and its delay in
    /// working samples.
    mf: Vec<f32>,
    mf_delay: f64,
    /// Sum of the matched filter's squared taps: its noise gain.
    mf_power: f64,
    /// Half the phase smoother's window, symbols.
    smooth: usize,
    fft: Arc<dyn Fft<f32>>,
    nfft: usize,
}

impl Geometry {
    fn new(rate: f64, cfg: &Config) -> Result<Geometry, DecodeError> {
        let baud = cfg.baud;
        let d = ((rate / (SPS_MIN * baud)).floor() as usize).max(1);
        let work = rate / d as f64;
        if work < 8.0 * baud {
            return Err(DecodeError::BadInput("rate_too_low"));
        }
        // Pass the signal wherever the AFC may take it; anything that folds
        // into that band on decimation is stopped.
        let pass = cfg.afc_range + 2.0 * baud;
        if cfg.channels.iter().any(|c| c.abs() + pass > 0.45 * rate) {
            return Err(DecodeError::BadInput("channel_outside"));
        }
        let tw = (work - 2.0 * pass).max(0.1 * work);
        let n = ((5.5 * rate / tw).ceil() as usize).clamp(15, 4001) | 1;
        let taps = if d == 1 { vec![1.0] } else { lowpass(n, work / 2.0, rate) };
        let decim_delay = (taps.len() - 1) as f64 / 2.0;
        let sps = work / baud;
        // The pulse is a Hann two symbols long (tx.rs). Its autocorrelation
        // is 3T/4 at 0 and T/8 a symbol away, nothing beyond, so the matched
        // filter alone leaves a sixth of each neighbour at the sampling
        // instant. Five taps a symbol apart, solved from those, take that
        // out for about 0.2 dB of noise (psk.py's correction, for this pulse).
        let r = [0.75, 0.125];
        let k = 2 * ZF_K + 1;
        let mut g = vec![vec![0.0; k]; k];
        for (i, row) in g.iter_mut().enumerate() {
            for (j, v) in row.iter_mut().enumerate() {
                let lag = i.abs_diff(j);
                *v = if lag < 2 { r[lag] } else { 0.0 };
            }
        }
        let c = solve(g, ZF_K);
        let span = (ZF_K as f64 + 1.0) * sps;
        let len = (2.0 * span).ceil() as usize | 1;
        let mid = (len - 1) as f64 / 2.0;
        let pulse = |t: f64| if t.abs() >= 1.0 { 0.0 } else { 0.5 * (1.0 + (PI * t).cos()) };
        let mut mf: Vec<f64> = (0..len)
            .map(|i| {
                let t = (i as f64 - mid) / sps; // symbols
                c.iter().enumerate().map(|(j, cj)| cj * pulse(t - (j as f64 - ZF_K as f64))).sum()
            })
            .collect();
        let sum: f64 = mf.iter().sum();
        mf.iter_mut().for_each(|h| *h /= sum);
        let mf_power = mf.iter().map(|h| h * h).sum();
        let smooth = ((SMOOTH_S * baud).round() as usize).max(2);
        let acq = (ACQ_SYMS as f64 * sps) as usize;
        let nfft = (4 * acq).next_power_of_two();
        let fft = FftPlanner::new().plan_fft_forward(nfft);
        Ok(Geometry {
            rate,
            d,
            work,
            sps,
            taps,
            decim_delay,
            mf: mf.into_iter().map(|h| h as f32).collect(),
            mf_delay: mid,
            mf_power,
            smooth,
            fft,
            nfft,
        })
    }
}

/// Solve g x = e_k (g symmetric positive definite, small) by Gaussian elimination.
fn solve(mut g: Vec<Vec<f64>>, k: usize) -> Vec<f64> {
    let n = g.len();
    let mut b: Vec<f64> = (0..n).map(|i| if i == k { 1.0 } else { 0.0 }).collect();
    for col in 0..n {
        for row in col + 1..n {
            let f = g[row][col] / g[col][col];
            for j in col..n {
                g[row][j] -= f * g[col][j];
            }
            b[row] -= f * b[col];
        }
    }
    let mut x = vec![0.0; n];
    for i in (0..n).rev() {
        let s: f64 = (i + 1..n).map(|j| g[i][j] * x[j]).sum();
        x[i] = (b[i] - s) / g[i][i];
    }
    x
}

/// A character to print: its first symbol's working sample, the character,
/// its SNR (dB in 2.5 kHz) and the channel's frequency then.
#[derive(Clone, Copy, Debug)]
struct Emit {
    at: f64,
    ch: u8,
    snr_db: f32,
    freq: f32,
}

/// What a decided bit leaves for the squelch: its symbol's time, the
/// coherent decision's squared parts, the log-likelihood ratio of "a signal"
/// against "noise" this bit alone gives, and the forward log-odds of a
/// signal given every bit to here.
#[derive(Clone, Copy, Debug)]
struct Bit {
    at: f64,
    re2: f32,
    im2: f32,
    ell: f32,
    fwd: f32,
    /// The noise was known when it was decided.
    settled: bool,
}

/// The squelch's model: a signal is on or off, and changes about this
/// often a bit (an over lasts many seconds). Small: a burst of noise must
/// make a strong case before it is believed (fifteen minutes of noise print
/// nothing), and a real over makes it within a few bits.
const SWITCH: f64 = 1e-6;
/// The most one bit can say for "a signal".
const ELL_CAP: f64 = 3.0;
/// The log-odds of "on" before anything is heard.
const PRIOR_ON: f64 = -5.0;
/// Time constant of the noise estimate, bits.
const NOISE_TAU: f64 = 32.0;

/// The log-odds of a state after one step of the on/off chain.
fn hop(x: f64) -> f64 {
    let p = 1.0 / (1.0 + (-x).exp());
    let q = p * (1.0 - SWITCH) + (1.0 - p) * SWITCH;
    (q / (1.0 - q)).ln()
}

fn log_cosh(x: f64) -> f64 {
    let a = x.abs();
    a + (-2.0 * a).exp().ln_1p() - std::f64::consts::LN_2
}

/// Everything after the decimator: AFC, matched filter, timing, Costas
/// loop, smoother, soft bits and trellis. Restarted whole on a replay.
struct Demod {
    afc_hz: f64,
    phase: f64,
    /// The matched filter's input, written twice so a window is one slice.
    hist: Vec<Complex32>,
    pos: usize,
    /// Working-sample index of the next sample in.
    m: u64,
    /// Matched-filter outputs from index `y0`.
    y: VecDeque<Complex32>,
    y0: u64,
    timing: Complex64,
    timing_rot: Complex64,
    timing_ph: Complex64,
    timing_n: u64,
    /// The next symbol's working-sample index (the matched filter's output).
    strobe: f64,
    theta: f64,
    nu: f64,
    /// The loop's frequency, integrated: the smoother's frame.
    psi: f64,
    /// Symbols waiting for the smoother: derotated, with their time.
    win: VecDeque<(Complex64, f64)>,
    delta: f64,
    er2: f64,
    ei2: f64,
    stats_n: u64,
    prev: Option<(f64, f64)>,
    prev_x: Complex64,
    vit: Viterbi,
    bits: VecDeque<Bit>,
    bits0: u64,
    /// Causal Es/N0, for the SNR shown.
    esn0: f64,
    /// Noise in the quadrature part, and the squelch's forward log-odds.
    noise: f64,
    noise_n: u64,
    on: f64,
    // -- IMD, from the derotated input on idle
    z2: VecDeque<Complex32>,
    z20: u64,
    idle_run: u32,
    imd: Option<(f64, f64, f64)>,
}

impl Demod {
    /// `noise`: the quadrature noise already measured (a replay at another
    /// frequency hears the same noise), so its squelch needs no settling.
    fn new(g: &Geometry, afc_hz: f64, m0: u64, noise: Option<f64>) -> Self {
        let l = g.mf.len();
        let rot = Complex64::from_polar(1.0, -2.0 * PI / g.sps);
        Demod {
            afc_hz,
            phase: 0.0,
            hist: vec![Complex32::new(0.0, 0.0); 2 * l],
            pos: 0,
            m: m0,
            y: VecDeque::new(),
            y0: m0,
            timing: Complex64::new(0.0, 0.0),
            timing_rot: rot,
            timing_ph: Complex64::from_polar(1.0, -2.0 * PI * (m0 as f64 % g.sps) / g.sps),
            timing_n: 0,
            strobe: m0 as f64 + g.sps,
            theta: 0.0,
            nu: 0.0,
            psi: 0.0,
            win: VecDeque::new(),
            delta: 0.0,
            er2: 0.0,
            ei2: 0.0,
            stats_n: 0,
            prev: None,
            prev_x: Complex64::new(0.0, 0.0),
            vit: Viterbi::new(DEPTH),
            bits: VecDeque::new(),
            bits0: 0,
            esn0: 0.0,
            noise: noise.unwrap_or(0.0),
            noise_n: if noise.is_some() { NOISE_TAU as u64 } else { 0 },
            on: PRIOR_ON,
            z2: VecDeque::new(),
            z20: m0,
            idle_run: 0,
            imd: None,
        }
    }

    fn freq(&self, baud: f64) -> f64 {
        self.afc_hz + self.nu * baud / (2.0 * PI)
    }
}

/// What a channel needs from the session's settings, per sample.
struct Ctx<'a> {
    g: &'a Geometry,
    cfg: &'a Config,
    locked: bool,
}

impl Ctx<'_> {
    /// The squelch's signal strength (Es/N0, dB); its default when it is
    /// off, for the sync state and the AFC.
    fn squelch_db(&self) -> f64 {
        if self.cfg.squelch_db > 0.0 { self.cfg.squelch_db } else { SQUELCH_DB }
    }
}

impl Demod {
    /// One working sample. Returns true when it completed a symbol.
    fn step(&mut self, z: Complex32, cx: &Ctx, printed: &mut f64, out: &mut Vec<Emit>) -> bool {
        let g = cx.g;
        let (s, c) = (2.0 * PI * self.phase).sin_cos();
        self.phase = (self.phase + self.afc_hz / g.work).rem_euclid(1.0);
        let z = z * Complex32::new(c as f32, -s as f32);
        self.z2.push_back(z);
        let keep = ((g.smooth + IMD_SPAN + 8) as f64 * g.sps) as usize + g.mf.len();
        while self.z2.len() > keep {
            self.z2.pop_front();
            self.z20 += 1;
        }
        let l = g.mf.len();
        self.hist[self.pos] = z;
        self.hist[self.pos + l] = z;
        self.pos = (self.pos + 1) % l;
        let w = &self.hist[self.pos..self.pos + l];
        let mut y = Complex32::new(0.0, 0.0);
        for (x, h) in w.iter().zip(&g.mf) {
            y += x * *h;
        }
        let m = self.m;
        self.m += 1;
        // Symbol timing: the envelope's component at the symbol rate. Idle
        // reversals dip to zero between symbols, so its phase is where the
        // symbols' centres are; runs without reversals are flat and add
        // nothing to it (psk.py's histogram, as one complex average).
        let a = (1.0 / (self.timing_n as f64 + 1.0)).max(1.0 / (TIMING_TAU * g.sps));
        self.timing = self.timing * (1.0 - a) + self.timing_ph * (y.norm_sqr() as f64 * a);
        self.timing_ph *= self.timing_rot;
        self.timing_n += 1;
        if self.timing_n % 4096 == 0 {
            self.timing_ph /= self.timing_ph.norm();
        }
        self.y.push_back(y);
        let mut produced = false;
        while self.strobe + 1.0 <= m as f64 {
            let i = self.strobe.floor();
            let f = (self.strobe - i) as f32;
            let k = (i as u64).saturating_sub(self.y0) as usize;
            let (Some(a0), Some(a1)) = (self.y.get(k), self.y.get(k + 1)) else {
                self.strobe = m as f64;
                break;
            };
            let yk = a0 + (a1 - a0) * f;
            let at = self.strobe - g.mf_delay;
            let peak = (-self.timing.arg() / (2.0 * PI) * g.sps).rem_euclid(g.sps);
            let err = (peak - self.strobe.rem_euclid(g.sps) + 1.5 * g.sps).rem_euclid(g.sps) - 0.5 * g.sps;
            self.strobe += g.sps + 0.5 * err;
            self.symbol(Complex64::new(yk.re as f64, yk.im as f64), at, cx, printed, out);
            produced = true;
        }
        let keep_from = (self.strobe.floor() as u64).saturating_sub(1);
        while self.y0 < keep_from && !self.y.is_empty() {
            self.y.pop_front();
            self.y0 += 1;
        }
        produced
    }

    fn symbol(&mut self, y: Complex64, at: f64, cx: &Ctx, printed: &mut f64, out: &mut Vec<Emit>) {
        let baud = cx.cfg.baud;
        // The Costas loop, decision-directed, its error normalised by the
        // signal's amplitude so its bandwidth does not depend on level.
        let s = y * Complex64::from_polar(1.0, -self.theta);
        let amp = if self.stats_n > 0 { (self.er2 - self.ei2).max(1e-3 * self.er2).sqrt() } else { s.norm() };
        let e = (s.im * s.re.signum() / amp.max(1e-30)).clamp(-1.0, 1.0);
        let tn = COSTAS_BN_HZ / baud / (COSTAS_ZETA + 0.25 / COSTAS_ZETA);
        let den = 1.0 + 2.0 * COSTAS_ZETA * tn + tn * tn;
        self.theta += self.nu + 4.0 * COSTAS_ZETA * tn / den * e;
        self.nu += 4.0 * tn * tn / den * e;
        // Until the coarse AFC has found a signal the loop may only follow a
        // small offset: on noise it would wander off to the edge.
        let span = if cx.cfg.afc && cx.locked { baud / 4.0 } else { baud / 8.0 };
        let lim = 2.0 * PI * span / baud;
        self.nu = self.nu.clamp(-lim, lim);
        // And while the squelch hears only noise its frequency drains back
        // to the AFC's: an over that starts there must not first pull the
        // loop back from where noise left it, decoding its preamble's first
        // half second through a reference turning by a radian a symbol.
        if self.on < 0.0 {
            self.nu *= NOISE_LEAK;
        }
        if cx.cfg.afc && cx.locked {
            // Hand the loop's frequency to the AFC a little at a time, so the
            // matched filter stays centred on a drifting signal.
            let t = AFC_TRANSFER * self.nu;
            let f = (self.afc_hz + t * baud / (2.0 * PI)).clamp(-cx.cfg.afc_range, cx.cfg.afc_range);
            self.nu -= (f - self.afc_hz) * 2.0 * PI / baud;
            self.afc_hz = f;
        }
        self.theta = (self.theta + PI).rem_euclid(2.0 * PI) - PI;
        // What the smoother sees is the symbol turned by the loop's
        // frequency alone, not by its phase: the loop's phase answers each
        // symbol's own noise at once, so the symbols after it would carry
        // that noise into the reference the smoother makes for it.
        self.win.push_back((y * Complex64::from_polar(1.0, -self.psi), at));
        self.psi = (self.psi + self.nu + PI).rem_euclid(2.0 * PI) - PI;
        let n = cx.g.smooth;
        if self.win.len() < 2 * n + 1 {
            return;
        }
        // The smoother: the residual phase over the window, data squared
        // away, taken at its centre; on the branch nearest the last one, so
        // the reference never jumps half a turn on its own. Without the
        // centre symbol itself: a reference fitted to the symbol it judges
        // leans towards that symbol's noise, and then noise alone reads as
        // part signal (the in-phase part larger than the quadrature), which
        // is exactly what the squelch must not see.
        let (sc, at) = self.win[n];
        // Weighted by nearness (a triangle): under fast fading the phase
        // moves within the window, and the nearest symbols are the best
        // guide; the farther ones still average the noise down.
        let weight = |j: usize| (n + 1 - j.abs_diff(n)) as f64;
        let (mut acc, mut power, mut wsum) = (Complex64::new(0.0, 0.0), 0.0, 0.0);
        for (j, (s, _)) in self.win.iter().enumerate() {
            if j != n {
                acc += s * s * weight(j);
                power += s.norm_sqr() * weight(j);
                wsum += weight(j);
            }
        }
        let mut d = 0.5 * acc.arg();
        while d - self.delta > PI / 2.0 {
            d -= PI;
        }
        while d - self.delta < -PI / 2.0 {
            d += PI;
        }
        self.delta = (d + PI).rem_euclid(2.0 * PI) - PI;
        self.win.pop_front();
        let x = sc * Complex64::from_polar(1.0, -d);
        // How steady the carrier's phase was across the window, noise
        // allowed for: 1 when every symbol agrees, less when the phase
        // turned within it, as it does through a fast fade's null.
        let coherence = (acc.norm() / (power - 2.0 * self.noise * wsum).max(1e-30)).min(1.0);
        self.decide(x, acc.norm() / wsum, coherence, at, cx, printed, out);
    }

    /// One symbol read against its reference: `local` is the channel's
    /// power there and `coherence` how far the reference can be trusted
    /// (both from the smoother).
    #[allow(clippy::too_many_arguments)]
    fn decide(
        &mut self,
        x: Complex64,
        local: f64,
        coherence: f64,
        at: f64,
        cx: &Ctx,
        printed: &mut f64,
        out: &mut Vec<Emit>,
    ) {
        let (re, im) = (x.re, x.im);
        let a = (1.0 / (self.stats_n as f64 + 1.0)).max(1.0 / STATS_TAU);
        self.er2 += a * (re * re - self.er2);
        self.ei2 += a * (im * im - self.ei2);
        self.stats_n += 1;
        let a2 = (self.er2 - self.ei2).max(1e-3 * self.er2).max(1e-30);
        let var = self.ei2.max(1e-30);
        self.esn0 = a2 / (2.0 * var);
        // The bit, two ways. Coherently: each symbol's sign as a
        // log-likelihood at the channel's amplitude there, and whether the
        // symbol and its neighbour agree (1, no reversal). Differentially,
        // as psk.py: the phase change from the last symbol, which needs no
        // reference at all. Coherent wins in noise (about 1 dB); differential
        // wins where the phase turns faster than any reference can follow,
        // through the null of a fast fade (Watterson "poor" at 31.25 Bd:
        // half the errors). The reference's own coherence says which.
        let amp_l = (local - var).max(0.05 * local).sqrt();
        let lam = (2.0 * amp_l * re / var).clamp(-50.0, 50.0);
        let coh = match self.prev {
            Some((pl, _)) => {
                let p = ((lam / 2.0).tanh() * (pl / 2.0).tanh()).clamp(-1.0 + 1e-12, 1.0 - 1e-12);
                2.0 * p.atanh()
            }
            None => 0.0,
        };
        let diff = (x * self.prev_x.conj()).re / var;
        self.prev_x = x;
        let w = ((coherence - COHERENT_FROM) / (COHERENT_FULL - COHERENT_FROM)).clamp(0.0, 1.0);
        let llr = w * coh + (1.0 - w) * diff;
        let reversal = self.prev.is_some_and(|(_, pre)| pre * re < 0.0);
        self.prev = Some((lam, re));
        // The squelch, bit by bit: how much likelier this decision is if a
        // signal of the squelch's strength is there (+-A in noise) than if
        // only noise is. The reference excludes the bit itself, so on noise
        // both parts are the same noise. Capped, so that a few loud noise
        // spikes cannot outvote the quiet around them.
        let k = (1.0 / (self.noise_n as f64 + 1.0)).max(1.0 / NOISE_TAU);
        self.noise += k * (im * im - self.noise);
        self.noise_n += 1;
        let var = self.noise.max(1e-30);
        let amp = (2.0 * var * 10f64.powf(cx.squelch_db() / 10.0)).sqrt();
        // No verdict until the noise is known: the first bits after a start
        // are measured against an estimate of a few bits, and the filters
        // and loops are still filling.
        let settled = self.noise_n as f64 > NOISE_TAU;
        let ell = if settled { (log_cosh(amp * re / var) - amp * amp / (2.0 * var)).min(ELL_CAP) } else { 0.0 };
        self.on = (ell + hop(self.on)).clamp(-40.0, 40.0);
        self.bits.push_back(Bit {
            at,
            re2: (re * re) as f32,
            im2: (im * im) as f32,
            ell: ell as f32,
            fwd: self.on as f32,
            settled,
        });
        let keep = DEPTH + 2 * SNR_W as usize + 64;
        while self.bits.len() > keep {
            self.bits.pop_front();
            self.bits0 += 1;
        }
        self.idle_run = if reversal { self.idle_run + 1 } else { 0 };
        if self.idle_run >= IMD_RUN && (self.idle_run - IMD_RUN) as usize % IMD_SPAN == 0 {
            self.measure_imd(cx.g, cx.cfg.baud);
        }
        if let Some(dc) = self.vit.push(llr as f32) {
            self.release(dc, cx, printed, out);
        }
    }

    /// A character the trellis decided: print it if the signal around it
    /// clears the squelch.
    fn release(&mut self, dc: varicode::Decided, cx: &Ctx, printed: &mut f64, out: &mut Vec<Emit>) {
        let len = varicode::TABLE[dc.ch as usize].len() as u64;
        let end = dc.first_bit + len + 2;
        let top = self.bits0 + self.bits.len() as u64;
        if dc.first_bit < self.bits0 || end > top {
            return;
        }
        let at = |b: u64| self.bits[(b - self.bits0) as usize];
        if cx.cfg.squelch_db > 0.0 {
            // Whether a signal was there, on every bit of the character:
            // the forward odds (everything before) with the backward message
            // (the bits decided since, the trellis's DEPTH of them). A
            // character half in noise at the edge of an over is refused, and
            // one in the middle of a weak over is carried by the whole over
            // rather than judged on its own few bits.
            let mut back = 0.0f64;
            let mut worst = f64::INFINITY;
            // From the separator before it: a character's first bit is the
            // phase change from the symbol before, so that symbol must be
            // the signal's too, and every character follows a separator.
            let from = dc.first_bit.saturating_sub(2).max(self.bits0);
            for b in (from..top).rev() {
                if b < end {
                    worst = worst.min(if at(b).settled { at(b).fwd as f64 + back } else { f64::NEG_INFINITY });
                }
                back = hop(at(b).ell as f64 + back);
            }
            if worst <= 0.0 {
                return;
            }
        }
        // The SNR shown: the bits around it.
        let (from, to) = (dc.first_bit.saturating_sub(SNR_W).max(self.bits0), (end + SNR_W).min(top));
        let (r2, i2) = (from..to).fold((0.0f64, 0.0f64), |(r, i), b| (r + at(b).re2 as f64, i + at(b).im2 as f64));
        let db = 10.0 * ((r2 - i2) / (2.0 * i2.max(1e-30))).max(1e-3).log10();
        let first = at(dc.first_bit);
        // Not twice: a replay decodes again what may already be printed.
        if first.at <= *printed + 0.5 * cx.g.sps {
            return;
        }
        *printed = first.at;
        let snr = db + 10.0 * (cx.cfg.baud / crate::testsig::SNR_BANDWIDTH).log10();
        out.push(Emit { at: first.at, ch: dc.ch, snr_db: snr as f32, freq: self.freq(cx.cfg.baud) as f32 });
    }

    /// Third-order IMD on a run of idle: the tones at +-1.5 baud against
    /// those at +-0.5 baud, over the run's last symbols but two, less the
    /// noise each correlation carries.
    fn measure_imd(&mut self, g: &Geometry, baud: f64) {
        let n = self.bits.len();
        if n < IMD_SPAN + 3 {
            return;
        }
        let start = self.bits[n - IMD_SPAN - 3].at.round() as u64;
        let len = (IMD_SPAN as f64 * g.sps).round() as usize;
        let Some(off) = start.checked_sub(self.z20) else { return };
        if off as usize + len > self.z2.len() {
            return;
        }
        let tone = |f: f64| -> f64 {
            let step = Complex64::from_polar(1.0, -2.0 * PI * f / g.work);
            let mut ph = Complex64::new(1.0, 0.0);
            let mut acc = Complex64::new(0.0, 0.0);
            for z in self.z2.iter().skip(off as usize).take(len) {
                acc += Complex64::new(z.re as f64, z.im as f64) * ph;
                ph *= step;
            }
            acc.norm_sqr()
        };
        let p1 = tone(0.5 * baud) + tone(-0.5 * baud);
        let p3 = tone(1.5 * baud) + tone(-1.5 * baud);
        // Noise per working sample from the decisions' quadrature part
        // through the matched filter's noise gain; two bins each.
        let noise = 2.0 * len as f64 * 2.0 * self.ei2 / g.mf_power;
        let k = 0.2;
        self.imd = Some(match self.imd {
            None => (p1, p3, noise),
            Some((a, b, c)) => (a + k * (p1 - a), b + k * (p3 - b), c + k * (noise - c)),
        });
    }

    fn imd_db(&self) -> Option<f64> {
        let (p1, p3, n) = self.imd?;
        // Only when the tones stand 20 dB over the noise and the products
        // can be told from it; otherwise there is nothing to say.
        if p1 - n < 100.0 * n || p3 - n < n {
            return None;
        }
        Some(10.0 * ((p3 - n) / (p1 - n)).log10())
    }
}

/// One signal in the passband.
struct Channel {
    nominal: f64,
    mix: Complex64,
    mix_step: Complex64,
    mixed: u64,
    decim: Decimator,
    /// Decimated samples (nominal frame) from working sample `ring0`.
    ring: VecDeque<Complex32>,
    ring0: u64,
    n: u64,
    demod: Demod,
    since_acq: usize,
    locked: bool,
    lost: u32,
    acquisitions: u64,
    replays: u64,
    /// Working sample of the last printed character's first symbol.
    printed: f64,
    /// The input sample (since the session's time base) its working sample 0 is.
    origin: u64,
}

impl Channel {
    fn new(nominal: f64, g: &Geometry) -> Self {
        Channel {
            nominal,
            mix: Complex64::new(1.0, 0.0),
            mix_step: Complex64::from_polar(1.0, -2.0 * PI * nominal / g.rate),
            mixed: 0,
            decim: Decimator::new(g.taps.clone(), g.d),
            ring: VecDeque::new(),
            ring0: 0,
            n: 0,
            demod: Demod::new(g, 0.0, 0, None),
            since_acq: 0,
            locked: false,
            lost: 0,
            acquisitions: 0,
            replays: 0,
            printed: f64::NEG_INFINITY,
            origin: 0,
        }
    }

    /// Forget the signal (a gap): keep the AFC and lock.
    fn restart(&mut self, g: &Geometry) {
        self.mix = Complex64::new(1.0, 0.0);
        self.mixed = 0;
        self.decim.reset();
        self.ring.clear();
        self.ring0 = 0;
        self.n = 0;
        self.demod = Demod::new(g, self.demod.afc_hz, 0, None);
        self.printed = f64::NEG_INFINITY;
        self.since_acq = 0;
        self.origin = 0;
    }

    fn feed(&mut self, x: &[Complex32], g: &Geometry, cfg: &Config, out: &mut Vec<Emit>) {
        let mut mixed = Vec::with_capacity(x.len());
        for z in x {
            let m = Complex32::new(self.mix.re as f32, self.mix.im as f32);
            mixed.push(z * m);
            self.mix *= self.mix_step;
            self.mixed += 1;
            if self.mixed % 1024 == 0 {
                self.mix /= self.mix.norm();
            }
        }
        let mut work = Vec::with_capacity(x.len() / g.d + 1);
        self.decim.process(&mixed, &mut work);
        let keep = (REPLAY_S * g.work).max(2.0 * ACQ_SYMS as f64 * g.sps) as usize;
        for z in work {
            self.ring.push_back(z);
            if self.ring.len() > keep {
                self.ring.pop_front();
                self.ring0 += 1;
            }
            self.n += 1;
            let cx = Ctx { g, cfg, locked: self.locked || !cfg.afc };
            if self.demod.step(z, &cx, &mut self.printed, out) {
                self.lost = if self.demod.on < 0.0 { self.lost + 1 } else { 0 };
                if self.lost >= LOST_SYMS {
                    self.locked = false;
                }
                self.since_acq += 1;
                if cfg.afc && !self.locked && self.since_acq >= ACQ_EVERY {
                    self.since_acq = 0;
                    self.acquire(g, cfg, out);
                }
            }
        }
    }

    /// The coarse AFC (psk.py's `_acquire`): square the decimated signal to
    /// take the data out, leaving a tone at twice the carrier's offset, and
    /// find it in an FFT of the last ACQ_SYMS symbols. The ring is in the
    /// channel's nominal frame, so the AFC's own movements do not smear it.
    fn acquire(&mut self, g: &Geometry, cfg: &Config, out: &mut Vec<Emit>) {
        let len = (ACQ_SYMS as f64 * g.sps) as usize;
        if self.ring.len() < len {
            return;
        }
        let from = self.ring.len() - len;
        let mut buf: Vec<Complex32> = self
            .ring
            .iter()
            .skip(from)
            .enumerate()
            .map(|(i, z)| {
                let w = 0.5 - 0.5 * (2.0 * std::f32::consts::PI * (i as f32 + 0.5) / len as f32).cos();
                z * z * w
            })
            .collect();
        buf.resize(g.nfft, Complex32::new(0.0, 0.0));
        g.fft.process(&mut buf);
        let bin = g.work / g.nfft as f64 / 2.0; // offset each bin means
        let reach = cfg.afc_range + 2.0;
        let span = (reach / bin) as i64;
        let power = |k: i64| buf[k.rem_euclid(g.nfft as i64) as usize].norm_sqr();
        let mut band: Vec<f32> = (-span..=span).map(power).collect();
        let (mut best, mut kb) = (0.0f32, 0i64);
        for k in -span..=span {
            if power(k) > best {
                best = power(k);
                kb = k;
            }
        }
        band.sort_by(f32::total_cmp);
        if best < ACQ_PROMINENCE * band[band.len() / 2] {
            return;
        }
        let (a, b, c) = (power(kb - 1).max(1e-30).ln(), best.max(1e-30).ln(), power(kb + 1).max(1e-30).ln());
        let frac = if a - 2.0 * b + c < 0.0 { 0.5 * (a - c) / (a - 2.0 * b + c) } else { 0.0 };
        let offset = ((kb as f64 + frac as f64) * bin).clamp(-cfg.afc_range, cfg.afc_range);
        self.locked = true;
        self.lost = 0;
        self.acquisitions += 1;
        if (offset - self.demod.afc_hz).abs() > cfg.baud / 10.0 {
            self.replay(offset, g, cfg, out);
        }
    }

    /// Decode the look-back again at the AFC's new frequency.
    fn replay(&mut self, afc_hz: f64, g: &Geometry, cfg: &Config, out: &mut Vec<Emit>) {
        let noise = (self.demod.noise_n as f64 >= NOISE_TAU).then_some(self.demod.noise);
        self.demod = Demod::new(g, afc_hz, self.ring0, noise);
        let cx = Ctx { g, cfg, locked: true };
        let ring: Vec<Complex32> = self.ring.iter().copied().collect();
        for z in ring {
            self.demod.step(z, &cx, &mut self.printed, out);
        }
        self.replays += 1;
    }

    fn sync(&self, cfg: &Config) -> bool {
        (self.locked || !cfg.afc) && self.demod.on > 0.0
    }

    fn snr_db(&self, cfg: &Config) -> f64 {
        10.0 * self.demod.esn0.max(1e-6).log10() + 10.0 * (cfg.baud / crate::testsig::SNR_BANDWIDTH).log10()
    }
}

pub struct Psk {
    cfg: Config,
    geo: Option<Geometry>,
    channels: Vec<Channel>,
    next_index: Option<u64>,
    base_t: f64,
    /// Input samples since the time base.
    fed: u64,
    since_meta: f64,
    meta_synced: bool,
    chars: u64,
    gaps: u64,
}

impl Default for Psk {
    fn default() -> Self {
        Self::new()
    }
}

impl Psk {
    pub fn new() -> Self {
        Psk {
            cfg: Config::default(),
            geo: None,
            channels: Vec::new(),
            next_index: None,
            base_t: 0.0,
            fed: 0,
            since_meta: 0.0,
            meta_synced: false,
            chars: 0,
            gaps: 0,
        }
    }

    fn build(&mut self, rate: f64) -> Result<(), DecodeError> {
        let g = Geometry::new(rate, &self.cfg)?;
        // Channels that stay keep their state when only the list changed.
        let same = self.geo.as_ref().is_some_and(|o| o.rate == g.rate && o.mf.len() == g.mf.len() && o.taps == g.taps);
        let mut old: Vec<Channel> = if same { std::mem::take(&mut self.channels) } else { Vec::new() };
        self.channels = self
            .cfg
            .channels
            .iter()
            .map(|&f| match old.iter().position(|c| c.nominal == f) {
                Some(i) => old.swap_remove(i),
                None => {
                    // Joining a running session: its first sample is the next one.
                    let mut c = Channel::new(f, &g);
                    c.origin = self.fed;
                    c
                }
            })
            .collect();
        self.geo = Some(g);
        Ok(())
    }

    fn meta(&self) -> Output {
        let cfg = &self.cfg;
        let list = self
            .channels
            .iter()
            .map(|c| {
                let sync = c.sync(cfg);
                let num = |x: Option<f64>| x.map(|v| Value::Num((v * 10.0).round() / 10.0)).unwrap_or(Value::Null);
                Value::Map(vec![
                    ("offsetHz".into(), Value::Num(c.nominal)),
                    ("freqHz".into(), num(Some(c.nominal + c.demod.freq(cfg.baud)))),
                    ("sync".into(), Value::Bool(sync)),
                    ("snrDb".into(), num(sync.then(|| c.snr_db(cfg)))),
                    ("imdDb".into(), num(if sync { c.demod.imd_db() } else { None })),
                ])
            })
            .collect();
        Output::Meta { fields: vec![("channels".into(), Value::List(list))] }
    }
}

impl Decoder for Psk {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        let mut cfg = self.cfg.clone();
        if let Some(x) = params.num("baud") {
            cfg.baud = x;
        }
        if let Some(s) = params.text("channels") {
            cfg.channels = parse_channels(s)?;
        }
        if let Some(x) = params.flag("afc") {
            cfg.afc = x;
        }
        if let Some(x) = params.num("afcRangeHz") {
            cfg.afc_range = x;
        }
        if let Some(x) = params.num("squelch") {
            cfg.squelch_db = x;
        }
        let rebuild = cfg.baud != self.cfg.baud || cfg.afc_range != self.cfg.afc_range || cfg.channels != self.cfg.channels;
        if let (true, Some(rate)) = (rebuild, self.geo.as_ref().map(|g| g.rate)) {
            // Checked before anything changes: a refused list keeps the old one.
            Geometry::new(rate, &cfg)?;
        }
        let old = std::mem::replace(&mut self.cfg, cfg);
        if !self.cfg.afc {
            for c in &mut self.channels {
                c.demod.afc_hz = 0.0;
            }
        }
        if rebuild {
            if let Some(rate) = self.geo.as_ref().map(|g| g.rate) {
                if old.baud != self.cfg.baud || old.afc_range != self.cfg.afc_range {
                    self.channels.clear();
                    self.geo = None;
                }
                self.build(rate)?;
            }
        }
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let Input::Iq { samples, rate, t0, index } = input else {
            return Err(DecodeError::BadInput("iq_only"));
        };
        if self.geo.as_ref().is_none_or(|g| g.rate != rate) {
            self.channels.clear();
            self.build(rate)?;
        }
        if let Some(next) = self.next_index {
            if next != index {
                self.gaps += 1;
                let g = self.geo.as_ref().unwrap();
                for c in &mut self.channels {
                    c.restart(g);
                }
                self.next_index = None;
            }
        }
        if self.next_index.is_none() {
            self.base_t = t0;
            self.fed = 0;
        }
        self.next_index = Some(index + samples.len() as u64);
        let g = self.geo.as_ref().unwrap();
        let mut emits = Vec::new();
        for c in &mut self.channels {
            let from = emits.len();
            c.feed(samples, g, &self.cfg, &mut emits);
            for e in &mut emits[from..] {
                // Working sample to input sample: every D-th, delayed by half
                // the decimating filter, from where the channel joined. Time
                // is the samples', never the clock's.
                e.at = c.origin as f64 + e.at * g.d as f64 - g.decim_delay;
            }
        }
        self.fed += samples.len() as u64;
        emits.sort_by(|a, b| a.at.total_cmp(&b.at));
        for e in emits {
            let t = self.base_t + e.at / g.rate;
            out.push(t, Output::Text { text: (e.ch as char).to_string(), offset_hz: e.freq, snr_db: Some(e.snr_db) });
            self.chars += 1;
        }
        self.since_meta += samples.len() as f64 / rate;
        let synced = self.channels.iter().any(|c| c.sync(&self.cfg));
        if self.since_meta >= META_EVERY_S && (synced || self.meta_synced) {
            self.since_meta = 0.0;
            self.meta_synced = synced;
            out.push(t0 + samples.len() as f64 / rate, self.meta());
        }
        Ok(())
    }

    fn reset(&mut self) {
        self.fed = 0;
        if let Some(g) = self.geo.clone() {
            self.channels = self.cfg.channels.iter().map(|&f| Channel::new(f, &g)).collect();
        }
        self.next_index = None;
        self.meta_synced = false;
    }

    fn status(&self) -> Status {
        let cfg = &self.cfg;
        let best = self.channels.iter().filter(|c| c.sync(cfg)).map(|c| c.snr_db(cfg)).fold(None, |a: Option<f64>, x| {
            Some(a.map_or(x, |a| a.max(x)))
        });
        Status {
            sync: self.channels.iter().any(|c| c.sync(cfg)),
            snr_db: best.map(|x| x as f32),
            offset_hz: self.channels.first().map(|c| (c.nominal + c.demod.freq(cfg.baud)) as f32),
            baud: Some(cfg.baud as f32),
            counters: vec![
                ("chars", self.chars),
                ("channels", self.channels.len() as u64),
                ("acquisitions", self.channels.iter().map(|c| c.acquisitions).sum()),
                ("replays", self.channels.iter().map(|c| c.replays).sum()),
                ("gaps", self.gaps),
            ],
        }
    }
}
