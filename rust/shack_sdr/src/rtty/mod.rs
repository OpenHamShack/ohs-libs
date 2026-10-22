//! RTTY: asynchronous start-stop FSK, 45.45, 50 or 75 baud, 170, 425, 450
//! or 850 Hz shift, normal or reversed, ITA2 with the US-TTY figures
//! (ita2.rs), unshift-on-space, AFC and squelch (RADIO-SDR-PLAN §8.1
//! item 6). IQ natively; the host adapts audio (dsp::AudioToIq), so the
//! same decoder serves Radio's channel IQ and Icom's or Flex's audio.
//!
//! It replaces openhamshack-nb `ohscore/modes/rtty.py`'s Decoder, and is
//! held to "never worse" than it on the golden numbers captured from it
//! before its removal (testdata/rtty/golden.json, tools/rtty_golden.py).
//! What differs from it, and why:
//!
//! - **Matched filters.** Each tone is mixed to 0 Hz and summed over exactly
//!   one bit (a boxcar, the matched filter of a rectangular bit), and a bit
//!   is read where that sum ends. rtty.py read two quarter-bit Goertzels a
//!   bit, half its energy, through bins 182 Hz wide that overlap at 170 Hz
//!   shift: about 3 dB of sensitivity lost before anything else.
//! - **The squelch is a detector of two tones, not of one frame.** rtty.py
//!   judged each frame by its own mark/space contrast, the only evidence it
//!   had, and noise passes that a few times a minute. Here a spectrum
//!   averaged over about a second (AFC below) asks whether there are tones
//!   at both mark and space, `shift` apart, standing above the noise in
//!   their own bandwidth: noise almost never does, and a signal well below
//!   the bits' own threshold does. Because that takes time to build up, the
//!   frames decoded in the last few seconds are held, and when the squelch
//!   opens the clean ones among them are printed: the first characters of
//!   an over are not lost to the detector's start-up.
//! - **AFC.** The same spectrum finds the tone pair within `afcRangeHz` and
//!   the mixers follow it, so a signal tuned 30 Hz off copies as if on
//!   frequency. Only the pair counts (the geometric mean of mark and space
//!   power): a carrier sitting on idle mark has one tone, and moving the
//!   decoder onto it could put space where mark is.
//! - **Frame timing is fitted, not taken from the first edge.** The start
//!   edge found in the filtered decisions is refined by trying timings
//!   around it and keeping the one where the seven bits are read most
//!   decisively (start space, stop mark).
//!
//! What stays as rtty.py: the frame (start, five bits, the first stop bit
//! read), ITA2 and the printing rules, unshift-on-space on by default, and
//! "noise must not leave the shift state in FIGS": a frame that does not
//! pass the squelch never reaches the printer, and the printer goes back to
//! letters when the squelch has been shut for a while.

pub mod ita2;

use crate::decoder::{
    check_params, DecodeError, Decoder, Family, Info, Input, InputKind, Output, OutputKind, Panel, ParamDefault,
    ParamKind, ParamSpec, Params, Sink, Status,
};
use crate::dsp::{lowpass, Decimator};
use rustfft::num_complex::{Complex32, Complex64};
use rustfft::{Fft, FftPlanner};
use std::collections::VecDeque;
use std::sync::Arc;

pub const BAUDS: &[f64] = &[45.45, 50.0, 75.0];
pub const SHIFTS: &[f64] = &[170.0, 425.0, 450.0, 850.0];

const PARAMS: &[ParamSpec] = &[
    ParamSpec { key: "baud", kind: ParamKind::Choice(BAUDS), default: ParamDefault::Num(45.45) },
    ParamSpec { key: "shift", kind: ParamKind::Choice(SHIFTS), default: ParamDefault::Num(170.0) },
    ParamSpec { key: "reverse", kind: ParamKind::Bool, default: ParamDefault::Bool(false) },
    ParamSpec { key: "afc", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
    ParamSpec { key: "afcRangeHz", kind: ParamKind::Range { min: 0.0, max: 250.0 }, default: ParamDefault::Num(100.0) },
    // The squelch: how far (dB) both tones must stand above the noise in
    // their own bandwidth before frames print. 0 turns it off.
    ParamSpec { key: "squelch", kind: ParamKind::Range { min: 0.0, max: 20.0 }, default: ParamDefault::Num(SQUELCH_DB) },
    ParamSpec { key: "usos", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
];

pub static INFO: Info = Info {
    id: "rtty",
    family: Family::Amateur,
    inputs: &[InputKind::Iq],
    rate_hz: 12_000.0,
    bandwidth_hz: 3_000.0,
    outputs: &[OutputKind::Text],
    panel: Panel::Text,
    params: PARAMS,
    needs: &[],
};

/// The squelch's default, dB of tone over noise in the tone's own band.
/// Noise alone, averaged as below, peaks at about 1.5 dB over the whole AFC
/// range; a 45.45 Bd signal at -10 dB SNR in 2.5 kHz stands about 5 dB up.
/// Measured: tests/rtty.rs `noise_prints_almost_nothing` and the sweep.
pub const SQUELCH_DB: f64 = 3.0;
/// The squelch counts the tones as heard down to this far below where it
/// opens, and stays open this long after it last heard them: a fade of a
/// second or two on an HF path should not close it mid-over.
const SQUELCH_HYST_DB: f64 = 1.5;
const SQUELCH_HANG_S: f64 = 2.0;
/// Frames are held this long for the squelch's look-back.
const LOOKBACK_S: f64 = 3.0;
/// A held frame must be at least this clean to print when the squelch
/// opens on it; noise frames are rarely above 0.45 (rtty.py's figures, and
/// ours: the contrast is the same measure).
const LOOKBACK_Q: f32 = 0.55;
/// While open, frames print down to this contrast.
const OPEN_Q: f32 = 0.25;
/// With the squelch off, frames print from this contrast (rtty.py's bar).
const NOSQUELCH_Q: f32 = 0.45;
/// After this long shut, the printer returns to letters.
const UNSHIFT_AFTER_S: f64 = 2.0;
/// The working rate the channel is decimated to, roughly.
const WORK_RATE: f64 = 3_000.0;
/// The detector's spectrum: FFT length and hop at the working rate, and
/// the time constant of its average.
const FFT_N: usize = 1024;
const FFT_HOP: usize = 256;
const SPECTRUM_TAU_S: f64 = 1.0;
/// Spectra averaged before the squelch may open (half a second).
const MIN_SPECTRA: u32 = 6;
/// The detector's noise is the median of the bins within this fraction of
/// the working rate either side of the centre.
const NOISE_SPAN: f64 = 0.33;
/// A frame's bits must carry this many times the power noise alone gives
/// the stronger tone's matched filter (1.5 x on average in noise, but the frames found in noise are the
/// strongest-looking ones: 3 to 4 x), so
/// noise frames are not printed in the squelch's hold or look-back.
const ENERGY_K: f32 = 5.0;
/// A frame printed in the look-back must have its weakest bit (the stronger
/// tone's raw magnitude) at least this fraction of its strongest.
const ENVELOPE: f32 = 0.3;

#[derive(Clone, Copy, Debug)]
struct Config {
    baud: f64,
    shift: f64,
    reverse: bool,
    afc: bool,
    afc_range: f64,
    squelch_db: f64,
    usos: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config { baud: 45.45, shift: 170.0, reverse: false, afc: true, afc_range: 100.0, squelch_db: SQUELCH_DB, usos: true }
    }
}

/// One working sample's measures: the two tones' matched-filter
/// magnitudes, the ATC's decision `v` (mark positive) and how decisive it
/// is (`q`, 0-1).
#[derive(Clone, Copy, Debug)]
struct Dec {
    m: f32,
    s: f32,
    v: f32,
    q: f32,
}

/// A frame decoded and waiting for the squelch.
#[derive(Clone, Copy, Debug)]
struct Held {
    code: u8,
    q: f32,
    /// Mean over its bits of the stronger tone's matched-filter power.
    energy: f32,
    /// Its weakest bit's level over its strongest (the raw levels).
    flat: f32,
    /// Working-rate sample of its start edge.
    at: f64,
}

/// The rate-dependent machinery, rebuilt when the input rate changes.
struct Chain {
    rate: f64,
    work: f64,
    decim: Decimator,
    /// Samples per bit at the working rate, and the matched filter's length.
    bit: f64,
    len: usize,
    fft: Arc<dyn Fft<f32>>,
}

pub struct Rtty {
    cfg: Config,
    chain: Option<Chain>,
    // -- the time line
    /// Input index of the next sample expected; a jump is a gap.
    next_index: Option<u64>,
    /// Input index and time of working sample 0.
    base_index: u64,
    base_t: f64,
    /// Working samples produced so far.
    n: u64,
    // -- mixers and matched filters
    phase_m: f64,
    phase_s: f64,
    box_m: VecDeque<Complex32>,
    box_s: VecDeque<Complex32>,
    sum_m: Complex64,
    sum_s: Complex64,
    /// Decisions history, one per working sample from `hist0`.
    hist: VecDeque<Dec>,
    /// Each tone's envelope (its level when on) and floor (when off), for
    /// the ATC.
    env_m: f32,
    env_s: f32,
    floor_m: f32,
    floor_s: f32,
    hist0: u64,
    /// Where the next start edge is searched from (working sample).
    scan: u64,
    // -- the detector and AFC
    ring: VecDeque<Complex32>,
    /// The working samples of the look-back, from working sample `raw0`.
    raw: VecDeque<Complex32>,
    raw0: u64,
    /// The AFC moved far: replay the look-back before the next search.
    replay_due: bool,
    /// Where the last printed frame started.
    printed_upto: Option<f64>,
    /// The last working sample the detector heard the tones.
    heard_at: u64,
    since_fft: usize,
    spectrum: Vec<f32>,
    spectra: u32,
    afc_hz: f64,
    tone_db: f64,
    /// Noise power per working sample, from the detector; 0 until known.
    noise_var: f64,
    snr_db: Option<f64>,
    open: bool,
    shut_since: f64,
    held: VecDeque<Held>,
    printer: ita2::Printer,
    // -- counters
    frames: u64,
    chars: u64,
    rejected: u64,
    gaps: u64,
    replays: u64,
}

impl Default for Rtty {
    fn default() -> Self {
        Self::new()
    }
}

impl Rtty {
    pub fn new() -> Self {
        let cfg = Config::default();
        Rtty {
            cfg,
            chain: None,
            next_index: None,
            base_index: 0,
            base_t: 0.0,
            n: 0,
            phase_m: 0.0,
            phase_s: 0.0,
            box_m: VecDeque::new(),
            box_s: VecDeque::new(),
            sum_m: Complex64::new(0.0, 0.0),
            sum_s: Complex64::new(0.0, 0.0),
            hist: VecDeque::new(),
            env_m: 0.0,
            env_s: 0.0,
            floor_m: 0.0,
            floor_s: 0.0,
            hist0: 0,
            scan: 1,
            ring: VecDeque::new(),
            raw: VecDeque::new(),
            raw0: 0,
            replay_due: false,
            printed_upto: None,
            heard_at: 0,
            since_fft: 0,
            spectrum: Vec::new(),
            spectra: 0,
            afc_hz: 0.0,
            tone_db: f64::NEG_INFINITY,
            noise_var: 0.0,
            snr_db: None,
            open: false,
            shut_since: 0.0,
            held: VecDeque::new(),
            printer: ita2::Printer::new(cfg.usos),
            frames: 0,
            chars: 0,
            rejected: 0,
            gaps: 0,
            replays: 0,
        }
    }

    fn build(&mut self, rate: f64) -> Result<(), DecodeError> {
        let need = self.cfg.shift / 2.0 + self.cfg.baud + self.cfg.afc_range;
        if rate < 2.0 * need {
            return Err(DecodeError::BadInput("rate_too_low"));
        }
        let d = ((rate / WORK_RATE).floor() as usize).max(1);
        let work = rate / d as f64;
        // The whole working band, not just the signal's: the detector takes
        // the noise from the median of this band's bins, so it must be flat
        // where the median is taken (NOISE_SPAN), not shaped by this filter.
        let cutoff = 0.42 * work;
        if need > 0.4 * work {
            return Err(DecodeError::BadInput("rate_too_low"));
        }
        let ntaps = ((6.0 * rate / (0.5 * work - cutoff).max(50.0)) as usize).clamp(15, 401);
        let bit = work / self.cfg.baud;
        let len = bit.round() as usize;
        let fft = FftPlanner::new().plan_fft_forward(FFT_N);
        self.chain = Some(Chain { rate, work, decim: Decimator::new(lowpass(ntaps, cutoff, rate), d), bit, len, fft });
        self.reset_signal();
        Ok(())
    }

    /// Forget the signal path's state (a gap, a retune) but not what the
    /// detector learnt of the noise, nor the AFC.
    fn reset_signal(&mut self) {
        if let Some(c) = self.chain.as_mut() {
            c.decim.reset();
        }
        self.n = 0;
        self.next_index = None;
        self.box_m.clear();
        self.box_s.clear();
        self.sum_m = Complex64::new(0.0, 0.0);
        self.sum_s = Complex64::new(0.0, 0.0);
        self.hist.clear();
        self.hist0 = 0;
        self.scan = 1;
        self.ring.clear();
        self.raw.clear();
        self.raw0 = 0;
        self.replay_due = false;
        self.printed_upto = None;
        self.since_fft = 0;
        self.held.clear();
    }

    fn tone_hz(&self) -> f64 {
        self.cfg.shift / 2.0 * if self.cfg.reverse { -1.0 } else { 1.0 }
    }

    /// One working sample through the mixers and matched filters.
    /// One working sample: through the signal path, and into the detector's
    /// ring and the raw history the AFC replays from.
    fn step(&mut self, z: Complex32) {
        self.step_signal(z);
        self.raw.push_back(z);
        let keep = self.raw_keep();
        while self.raw.len() > keep {
            self.raw.pop_front();
            self.raw0 += 1;
        }
        self.ring.push_back(z);
        if self.ring.len() > FFT_N {
            self.ring.pop_front();
        }
        self.since_fft += 1;
    }

    fn raw_keep(&self) -> usize {
        let c = self.chain.as_ref().unwrap();
        (LOOKBACK_S * c.work + 2.0 * c.bit) as usize
    }

    /// The AFC has just found the signal somewhere else: run the samples
    /// held for the look-back through the mixers again at the new offset,
    /// so the over's first characters, decoded while it was still mistuned,
    /// are decoded again as they should have been rather than printed
    /// garbled (or lost). What has already been printed is not decoded
    /// twice.
    fn replay(&mut self) {
        let bit = self.chain.as_ref().unwrap().bit;
        let raw: Vec<Complex32> = self.raw.iter().copied().collect();
        self.box_m.clear();
        self.box_s.clear();
        self.sum_m = Complex64::new(0.0, 0.0);
        self.sum_s = Complex64::new(0.0, 0.0);
        self.env_m = 0.0;
        self.env_s = 0.0;
        self.floor_m = 0.0;
        self.floor_s = 0.0;
        self.hist.clear();
        self.held.clear();
        self.n = self.raw0;
        self.hist0 = self.raw0;
        let after = self.printed_upto.map(|p| (p + 6.6 * bit) as u64).unwrap_or(0);
        self.scan = (self.raw0 + 1).max(after);
        for z in raw {
            self.step_signal(z);
        }
        self.replays += 1;
    }

    /// One working sample through the mixers, matched filters and ATC.
    fn step_signal(&mut self, z: Complex32) {
        let (work, len) = {
            let c = self.chain.as_ref().unwrap();
            (c.work, c.len)
        };
        let h = self.tone_hz();
        let fm = self.afc_hz + h;
        let fs = self.afc_hz - h;
        let (sm, cm) = (2.0 * std::f64::consts::PI * self.phase_m).sin_cos();
        let (ss, cs) = (2.0 * std::f64::consts::PI * self.phase_s).sin_cos();
        self.phase_m = (self.phase_m + fm / work).rem_euclid(1.0);
        self.phase_s = (self.phase_s + fs / work).rem_euclid(1.0);
        let zm = z * Complex32::new(cm as f32, -sm as f32);
        let zs = z * Complex32::new(cs as f32, -ss as f32);
        self.sum_m += Complex64::new(zm.re as f64, zm.im as f64);
        self.sum_s += Complex64::new(zs.re as f64, zs.im as f64);
        self.box_m.push_back(zm);
        self.box_s.push_back(zs);
        if self.box_m.len() > len {
            let om = self.box_m.pop_front().unwrap();
            let os = self.box_s.pop_front().unwrap();
            self.sum_m -= Complex64::new(om.re as f64, om.im as f64);
            self.sum_s -= Complex64::new(os.re as f64, os.im as f64);
        }
        if self.n % 8192 == 0 {
            // Re-sum now and then so rounding cannot creep in.
            self.sum_m = self.box_m.iter().map(|z| Complex64::new(z.re as f64, z.im as f64)).sum();
            self.sum_s = self.box_s.iter().map(|z| Complex64::new(z.re as f64, z.im as f64)).sum();
        }
        let (m, s) = (self.sum_m.norm() as f32, self.sum_s.norm() as f32);
        let bit = work as f32 / self.cfg.baud as f32;
        let dec = self.atc(m, s, bit);
        self.hist.push_back(dec);
        self.n += 1;
    }

    /// Optimal ATC (Kok Chen's, as fldigi's "optimal" setting): each tone
    /// is measured against its own envelope and floor, so when selective
    /// fading takes one tone down the bit is read from the other. A plain
    /// mark-minus-space reads a faded mark as space. Clean mark gives
    /// +0.5 (Em^2 + Es^2), clean space the negative.
    fn atc(&mut self, m: f32, s: f32, bit: f32) -> Dec {
        let track = |env: &mut f32, floor: &mut f32, x: f32| {
            // Envelope: up in a quarter bit, down over 16 bits. Floor: down
            // in a quarter bit, up over 48 (fldigi's constants).
            let k = if x > *env { 4.0 / bit } else { 1.0 / (8.0 * bit) };
            *env += (x - *env) * k.min(1.0);
            let k = if x < *floor { 4.0 / bit } else { 1.0 / (48.0 * bit) };
            *floor += (x - *floor) * k.min(1.0);
        };
        track(&mut self.env_m, &mut self.floor_m, m);
        track(&mut self.env_s, &mut self.floor_s, s);
        let em = (self.env_m - self.floor_m).max(1e-20);
        let es = (self.env_s - self.floor_s).max(1e-20);
        let mc = m - self.floor_m;
        let sc = s - self.floor_s;
        let full = 0.5 * (em * em + es * es);
        let atc = (mc * em - sc * es - 0.5 * (em * em - es * es)) / full;
        // Half and half with the plain difference, each on its own full
        // scale: the ATC alone misreads the first bits of an over, before
        // the envelope of a tone not yet heard has been learnt, and costs a
        // little in plain noise; the plain difference alone misreads a
        // tone faded on its own. Measured on the sweep (tests/rtty.rs).
        let plain = (m - s) / self.env_m.max(self.env_s).max(1e-20);
        let v = 0.5 * atc + 0.5 * plain;
        Dec { m, s, v, q: v.abs().min(1.0) }
    }

    /// The decision at fractional working sample `t`, interpolated.
    fn at(&self, t: f64) -> Option<Dec> {
        if t < self.hist0 as f64 {
            return None;
        }
        let i = (t - self.hist0 as f64).floor() as usize;
        let f = (t - self.hist0 as f64 - i as f64) as f32;
        let a = *self.hist.get(i)?;
        let b = *self.hist.get(i + 1)?;
        let l = |x: f32, y: f32| x + (y - x) * f;
        Some(Dec { m: l(a.m, b.m), s: l(a.s, b.s), v: l(a.v, b.v), q: l(a.q, b.q) })
    }

    fn d(&self, i: u64) -> Option<f32> {
        let k = i.checked_sub(self.hist0)? as usize;
        self.hist.get(k).map(|d| d.v)
    }

    /// Search for frames from `scan` up to what has been filtered.
    fn frames(&mut self) {
        let (bit, len) = {
            let c = self.chain.as_ref().unwrap();
            (c.bit, c.len)
        };
        let end = self.hist0 + self.hist.len() as u64;
        loop {
            // The next mark-to-space crossing of the decisions.
            let mut i = self.scan.max(self.hist0 + 1);
            let mut found = None;
            while i < end {
                if let (Some(a), Some(b)) = (self.d(i - 1), self.d(i)) {
                    if a >= 0.0 && b < 0.0 {
                        found = Some(i);
                        break;
                    }
                }
                i += 1;
            }
            let Some(c) = found else {
                self.scan = end.max(1);
                break;
            };
            // The boxcar's sum crosses zero half a bit into the start bit;
            // bit k is then read at the end of the bit, edge + (k+1) bits.
            let edge = c as f64 - bit / 2.0;
            let last = edge + 7.0 * bit + bit / 3.0 + 2.0;
            if last >= end as f64 - 1.0 {
                self.scan = c; // wait for more; re-examine this crossing
                break;
            }
            match self.frame(edge, bit, len) {
                Some((code, q, energy, flat, e)) => {
                    self.frames += 1;
                    self.held.push_back(Held { code, q, energy, flat, at: e });
                    // Resume within the stop bit: the earliest the next start
                    // edge can come (one stop bit), less a little for speed error.
                    self.scan = (e + 6.6 * bit) as u64;
                }
                None => {
                    self.rejected += 1;
                    self.scan = c + 1;
                }
            }
        }
        // Keep the decisions history to what a frame can still reach.
        let keep_from = self.scan.saturating_sub((3.0 * bit) as u64 + len as u64);
        while self.hist0 < keep_from && !self.hist.is_empty() {
            self.hist.pop_front();
            self.hist0 += 1;
        }
    }

    /// The frame whose start edge is near `edge`: (code, decisiveness,
    /// energy, flatness, fitted edge), or None if it is no frame.
    fn frame(&self, edge: f64, bit: f64, _len: usize) -> Option<(u8, f32, f32, f32, f64)> {
        let mut best: Option<(f32, f64)> = None;
        let span = (bit / 3.0).max(1.0);
        let steps = (2.0 * span).ceil() as i32;
        for k in 0..=steps {
            let e = edge - span + k as f64 * (2.0 * span / steps as f64);
            let mut score = 0.0f32;
            for b in 0..7 {
                let d = self.at(e + (b as f64 + 1.0) * bit - 1.0)?.v;
                score += match b {
                    0 => -d,
                    6 => d,
                    _ => d.abs(),
                };
            }
            if best.is_none_or(|(s, _)| score > s) {
                best = Some((score, e));
            }
        }
        let (_, e) = best?;
        let mut code = 0u8;
        let mut q = 0.0f32;
        let mut energy = 0.0f32;
        let mut ds = [0.0f32; 7];
        let (mut lo, mut hi) = (f32::MAX, 0.0f32);
        for (b, d) in ds.iter_mut().enumerate() {
            let x = self.at(e + (b as f64 + 1.0) * bit - 1.0)?;
            *d = x.v;
            q += x.q;
            energy += x.m.max(x.s) * x.m.max(x.s);
            lo = lo.min(x.m.max(x.s));
            hi = hi.max(x.m.max(x.s));
        }
        if ds[0] >= 0.0 || ds[6] <= 0.0 {
            return None; // no start bit, or no stop bit: not a frame
        }
        let flat = lo / hi.max(1e-30);
        for d in &ds[1..6] {
            code = (code << 1) | u8::from(*d > 0.0);
        }
        Some((code, q / 7.0, energy / 7.0, flat, e))
    }

    /// The detector and AFC, every FFT_HOP working samples.
    fn spectrum_step(&mut self) {
        let c = self.chain.as_ref().unwrap();
        let (work, fft) = (c.work, c.fft.clone());
        if self.ring.len() < FFT_N {
            return;
        }
        let mut buf: Vec<Complex32> = self
            .ring
            .iter()
            .enumerate()
            .map(|(i, z)| {
                let w = 0.5 - 0.5 * (2.0 * std::f32::consts::PI * i as f32 / FFT_N as f32).cos();
                z * w
            })
            .collect();
        fft.process(&mut buf);
        let alpha = 1.0 - (-(FFT_HOP as f64 / work) / SPECTRUM_TAU_S).exp();
        if self.spectrum.len() != FFT_N {
            self.spectrum = vec![0.0; FFT_N];
            self.spectra = 0;
        }
        // Average quickly while it fills, then with the time constant.
        let a = if (self.spectra as f64) < 1.0 / alpha { 1.0 / (self.spectra as f64 + 1.0) } else { alpha } as f32;
        for (p, z) in self.spectrum.iter_mut().zip(&buf) {
            *p += a * (z.norm_sqr() - *p);
        }
        self.spectra += 1;

        let bin_hz = work / FFT_N as f64;
        // Noise: the median bin within NOISE_SPAN of the centre, where the
        // decimating filter is flat; the tones (a few percent of those bins)
        // hardly move a median. The median of an averaged noise spectrum
        // sits a little under its mean; near enough for a squelch.
        let span = (NOISE_SPAN * work / bin_hz) as i64;
        let mut sorted: Vec<f32> = (-span..=span).map(|k| self.spectrum[k.rem_euclid(FFT_N as i64) as usize]).collect();
        sorted.sort_by(|a, b| a.total_cmp(b));
        // The median of an average of n spectra sits below its mean, by ln 2
        // for one (chi-squared, two degrees of freedom) and less as more are
        // averaged; overlapping by three quarters, about half are independent.
        let n_eff = (self.spectra as f64).min(2.0 / alpha - 1.0) / 2.0;
        let bias = 1.0 + (1.0 / std::f64::consts::LN_2 - 1.0) / n_eff.max(1.0);
        let noise = (sorted[sorted.len() / 2] as f64 * bias).max(1e-30);
        // The frames' energy floor takes the larger of that and the noise in
        // this spectrum alone. The average lags by its second's time
        // constant, and a receiver's AGC moves the noise faster than that:
        // a strong over holds it down, and when the carrier drops it comes
        // back up within half a second. Against the lagging average the
        // noise frames there looked five times "noise", and with the
        // squelch still open on the over's tones they printed: one to three
        // characters after the over in three of five of openhamshack-nb's
        // own-echo runs ("PSE KSWI"; tests/rtty.rs
        // `nothing_after_the_carrier_drops_under_agc`). One spectrum's median
        // over its few hundred bins is already a good estimate (its bins are
        // chi-squared with two degrees of freedom: the median is ln 2 of the
        // mean); only the tones' measure needs the average. The larger, not
        // the latest alone: the floor rises at once but falls only with the
        // average, so a moment of quieter noise never lowers it.
        let mut now: Vec<f32> = (-span..=span).map(|k| buf[k.rem_euclid(FFT_N as i64) as usize].norm_sqr()).collect();
        now.sort_by(|a, b| a.total_cmp(b));
        let now = now[now.len() / 2] as f64 / std::f64::consts::LN_2;
        // Per working sample: a Hann-windowed FFT of white noise of variance
        // v has E|X|^2 = v * sum(w^2) = v * 0.375 N.
        self.noise_var = noise.max(now) / (0.375 * FFT_N as f64);
        // Power within +-w of a frequency, w about half the baud (where a
        // keyed tone's energy is), in bins.
        let w = ((self.cfg.baud / 2.0) / bin_hz).round().max(1.0) as i64;
        let band = |f: f64| -> f64 {
            let centre = (f / bin_hz).round() as i64;
            (centre - w..=centre + w).map(|k| self.spectrum[k.rem_euclid(FFT_N as i64) as usize] as f64).sum()
        };
        let nb = (2 * w + 1) as f64 * noise;
        let h = self.tone_hz();
        let range = if self.cfg.afc { self.cfg.afc_range } else { 0.0 };
        let mut best = (f64::NEG_INFINITY, 0.0, 0.0, 0.0);
        let steps = (range / (bin_hz / 2.0)).ceil() as i64;
        for k in -steps..=steps {
            let f = if steps == 0 { 0.0 } else { k as f64 * range / steps as f64 };
            let (m, s) = (band(f + h), band(f - h));
            let g = (m * s).sqrt();
            if g > best.0 {
                best = (g, f, m, s);
            }
        }
        let (_, f, m, s) = best;
        let ratio = (m.min(s) / nb).max(1e-9);
        self.tone_db = 10.0 * ratio.log10();
        // SNR in 2.5 kHz: the tones' power over the noise in 2.5 kHz,
        // summed over each tone's main lobe (+-1 baud), where a keyed tone
        // has nine tenths of its power; the detector's narrower window
        // above has the better signal-to-noise for deciding.
        let wide = ((self.cfg.baud / bin_hz).round().max(1.0)) as i64;
        let band_wide = |f: f64| -> f64 {
            let centre = (f / bin_hz).round() as i64;
            (centre - wide..=centre + wide).map(|k| self.spectrum[k.rem_euclid(FFT_N as i64) as usize] as f64).sum()
        };
        let sig = (band_wide(f + h) + band_wide(f - h) - 2.0 * (2 * wide + 1) as f64 * noise).max(0.0);
        self.snr_db = if sig > 0.0 { Some(10.0 * (sig / (noise * 2500.0 / bin_hz)).log10()) } else { None };
        let was_open = self.open;
        let thr = self.cfg.squelch_db;
        // Not before the average has settled: one spectrum of noise has peaks
        // a squelch would open on. The look-back covers the wait.
        let settled = self.spectra >= MIN_SPECTRA;
        if settled && self.tone_db >= thr - SQUELCH_HYST_DB {
            self.heard_at = self.n;
        }
        // Open on the threshold; once open, stay open through a fade until
        // the tones have been gone SQUELCH_HANG_S. Noise frames in the hang
        // are kept out by their energy (`release`).
        let hang = (self.n - self.heard_at) as f64 <= SQUELCH_HANG_S * work;
        self.open = thr <= 0.0 || (settled && (self.tone_db >= thr || (was_open && hang)));
        if self.cfg.afc && settled && self.tone_db >= thr.max(SQUELCH_DB) {
            // Pull towards the pair; snap when far (acquiring) and decode the
            // look-back again at the new offset; creep when close.
            let err = f - self.afc_hz;
            if err.abs() > self.cfg.baud / 4.0 {
                self.afc_hz = f;
                self.replay_due = true;
            } else {
                self.afc_hz += 0.3 * err;
            }
        }
    }

    /// Print what the squelch lets through.
    fn release(&mut self, out: &mut String, first_at: &mut Option<f64>) {
        let (work, len) = {
            let c = self.chain.as_ref().unwrap();
            (c.work, c.len)
        };
        let floor = (ENERGY_K as f64 * len as f64 * self.noise_var) as f32;
        let now = self.scan as f64;
        let horizon = now - LOOKBACK_S * work;
        let open = self.open;
        let squelched = self.cfg.squelch_db > 0.0;
        while let Some(h) = self.held.front().copied() {
            let bar = if !squelched {
                NOSQUELCH_Q
            } else if open {
                if self.shut_since > h.at { LOOKBACK_Q } else { OPEN_Q }
            } else {
                // Shut: hold it for the look-back, drop it when too old.
                if h.at < horizon {
                    self.held.pop_front();
                    continue;
                }
                break;
            };
            if squelched && self.noise_var <= 0.0 {
                break; // no noise estimate yet: keep it for the look-back
            }
            self.held.pop_front();
            // A held frame printed in the look-back is where an over starts,
            // and the frame that straddles noise and the first mark is the
            // one to refuse: noise at one end, signal at the other (rtty.py's
            // constant-envelope test). On the raw levels, not the ATC's,
            // and only here: once the squelch is open a tone faded on its own
            // (the ATC's case) must not cost the frame.
            let lookback = squelched && bar == LOOKBACK_Q;
            if h.q < bar || (squelched && h.energy < floor) || (lookback && h.flat < ENVELOPE) {
                continue;
            }
            self.printed_upto = Some(h.at);
            if let Some(ch) = self.printer.code(h.code) {
                out.push(ch);
                self.chars += 1;
                first_at.get_or_insert(h.at);
            }
        }
        if !open {
            // Back to letters after a while shut, so noise or a lost LTRS
            // cannot leave the next over in figures.
            if now - self.shut_since > UNSHIFT_AFTER_S * work {
                self.printer.figs = false;
            }
        }
    }
}

impl Decoder for Rtty {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        let mut cfg = self.cfg;
        if let Some(x) = params.num("baud") {
            cfg.baud = x;
        }
        if let Some(x) = params.num("shift") {
            cfg.shift = x;
        }
        if let Some(x) = params.flag("reverse") {
            cfg.reverse = x;
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
        if let Some(x) = params.flag("usos") {
            cfg.usos = x;
        }
        let rebuild = cfg.baud != self.cfg.baud || cfg.shift != self.cfg.shift || cfg.afc_range != self.cfg.afc_range;
        self.cfg = cfg;
        self.printer.usos = cfg.usos;
        if !cfg.afc {
            self.afc_hz = 0.0;
        }
        if rebuild {
            if let Some(rate) = self.chain.as_ref().map(|c| c.rate) {
                self.build(rate)?;
            }
        }
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let Input::Iq { samples, rate, t0, index } = input else {
            return Err(DecodeError::BadInput("iq_only"));
        };
        if self.chain.as_ref().is_none_or(|c| c.rate != rate) {
            self.build(rate)?;
        }
        if let Some(next) = self.next_index {
            if next != index {
                self.gaps += 1;
                self.reset_signal();
            }
        }
        if self.next_index.is_none() {
            self.base_index = index;
            self.base_t = t0;
        }
        self.next_index = Some(index + samples.len() as u64);
        let mut work = Vec::with_capacity(samples.len() / 2 + 1);
        self.chain.as_mut().unwrap().decim.process(samples, &mut work);
        let mut text = String::new();
        let mut first_at = None;
        for z in work {
            self.step(z);
            if self.since_fft >= FFT_HOP {
                self.since_fft = 0;
                let was_open = self.open;
                self.spectrum_step();
                if was_open && !self.open {
                    self.shut_since = self.n as f64;
                }
                if !was_open && self.open {
                    // Everything held from before now is look-back.
                    self.shut_since = self.n as f64;
                }
                if self.replay_due {
                    self.replay_due = false;
                    self.replay();
                }
                self.frames();
                self.release(&mut text, &mut first_at);
            }
        }
        self.frames();
        self.release(&mut text, &mut first_at);
        if !text.is_empty() {
            let c = self.chain.as_ref().unwrap();
            // Working sample to input index: decimation keeps every D-th
            // input, delayed by half the filter; time is the samples', never
            // the clock's.
            let at = first_at.unwrap_or(0.0) * (c.rate / c.work);
            let t = self.base_t + at / c.rate;
            out.push(t, Output::Text { text, offset_hz: self.afc_hz as f32, snr_db: self.snr_db.map(|x| x as f32) });
        }
        Ok(())
    }

    fn reset(&mut self) {
        self.reset_signal();
        self.afc_hz = 0.0;
        self.spectrum.clear();
        self.spectra = 0;
        self.open = false;
        self.printer = ita2::Printer::new(self.cfg.usos);
    }

    fn status(&self) -> Status {
        Status {
            sync: self.open,
            snr_db: self.snr_db.map(|x| x as f32),
            offset_hz: Some(self.afc_hz as f32),
            baud: Some(self.cfg.baud as f32),
            counters: vec![
                ("frames", self.frames),
                ("chars", self.chars),
                ("rejected", self.rejected),
                ("gaps", self.gaps),
                ("replays", self.replays),
            ],
        }
    }
}
