//! SSTV receive (RADIO-SDR-PLAN §8.5 M2): a VIS header or a train of line
//! syncs starts a picture in any of the fourteen modes in modes.rs; each
//! line is placed by its sync pulse and a fitted line clock (so a sender
//! whose sound card is 200 ppm out still comes out straight), read as RGB,
//! sent as it arrives (`Image` lines), and when the picture ends it is
//! straightened once more from all its syncs, saved as a PNG for
//! `api/decoders/files`, and described in `meta` (mode, start time, the
//! sender's clock error, SNR, tuning offset).
//!
//! It replaces openhamshack-nb `ohscore/modes/sstv.py`'s Decoder (Robot
//! 36, Martin M1, Scottie S1, PD120 there), and is held to "never worse"
//! than it on golden numbers captured from it on synthetic pictures
//! (testdata/sstv, tools/sstv_golden.py): PSNR against the picture sent,
//! and the share of rows within a pixel of where they belong. It is the
//! one SSTV decoder (GUIDE §1 "one of each"): OpenHamShack Icom's
//! `app/sstv` is a second, pure-Python one, and Flex vendors sstv.py.
//!
//! What it keeps from sstv.py, and what differs and why:
//!
//! - **Frequency from complex baseband**, as sstv.py: the audio is mixed
//!   down (by 1750 Hz, the middle of sync and white, so the band is
//!   symmetric) and low-pass filtered, once, with its state carried across
//!   blocks. IQ, centred on the 1900 Hz leader as an SSB channel's
//!   session frequency would be, takes the same path less the mix's image.
//!   Any rate: 48 kHz audio is decimated to about 12 kHz in the same
//!   filter, never resampled (a resampled sound card's 2 % rate error is
//!   a slanted picture: Icom's discriminator.py).
//! - **Pixels through a filter as narrow as the noise asks** (`PixFilter`):
//!   centred on the picture band, 550 Hz either side at 8 dB, 2 kHz on a
//!   clean signal, from the picture's SNR. Below an FM discriminator's
//!   threshold every hertz let in costs clicks; above it width is detail.
//!   sstv.py had one 15-tap filter about 1400 Hz wide: at 10 dB in 2.5 kHz
//!   it read Martin M1 at 18.7 dB PSNR, this at 23.1.
//! - **A pixel is the mean frequency over its exact span**: the angle of
//!   the sum of z[n] conj z[n-1] across it, at fractional-sample edges,
//!   less the known pull of the filtered noise (`View::freq`), which drew
//!   every pixel towards mid-grey by up to 50 Hz at 10 dB.
//! - **VIS at any tuning error to +-150 Hz** (`afcRangeHz`). sstv.py's 10
//!   ms tone detectors null a tone 50 Hz away, so it found no header
//!   beyond about 30 Hz off. Here the same template (leaders, break, start
//!   bit) is scored against a bank of detectors 25 Hz apart, the best
//!   offset taken, the bits read there, and the offset then measured to a
//!   hertz from the leaders and carried into the picture: every pixel and
//!   every sync detector is corrected for it, and it is tracked through
//!   the picture from the sync pulses.
//! - **Syncs by a matched filter for the tones around them** (`matched`):
//!   the pulse with its porch (and Martin's separator before it), so the
//!   peak is symmetric; sstv.py's pulse-only detector placed PD120's syncs
//!   2-3 samples late in noise, a pixel.
//! - **The line clock is fitted, not looped.** sstv.py ran a second-order
//!   loop on each sync. Here every sync found is kept, and each line's
//!   start is a weighted straight-line fit through the syncs before it (a
//!   sound card's error is a constant rate, which a line fits exactly and a
//!   loop only chases); a sync far from the fit is ignored unless three
//!   strong ones in a row agree (the sender's timing really jumped). When
//!   the picture ends, every line is read again from the fit through all
//!   of its syncs, before and after, which also places the first line of
//!   the modes whose first sync runs on from the VIS stop bit; rows that
//!   change are sent again. That is OpenHamShack Icom's decoder's "fit"
//!   (app/sstv/decoder.py), made local, its span chosen per picture by
//!   cross-validation (`choose_span`): long for a steady clock in noise,
//!   short when multipath moves the arrival time.
//! - **The header names the mode, not the picture.** A VIS header heard
//!   during a picture counts only if that picture's syncs stopped under
//!   it (PD's white rows pass for one), and its parity mends a bit a fade
//!   left in doubt.
//! - **Robot 36's colour is rebuilt from both lines of a pair.** sstv.py
//!   gave each row the chroma of the row before for the half it lacked;
//!   here a row is sent once the pair is complete, so its two colour
//!   differences come from its own pair.
//! - **Kept from sstv.py:** the template VIS detector and parity check; the
//!   start from line syncs alone (a picture joined part way, marked
//!   partial, ending when its syncs stop), now from the first line whose
//!   sync is really there; Robot 36's separator naming its chroma; the
//!   Dayton paper's timings and colour conversion.
//!
//! Memory: a picture's baseband is kept until it ends, for the final
//! straightening: 8 bytes a sample at about 12 kHz, 11 MB for Martin M1
//! and 28 MB for PD290, freed when it ends.

pub mod modes;
pub mod png;
pub mod tx;

#[cfg(feature = "python")]
pub(crate) mod py;

use crate::decoder::{
    check_params, DecodeError, Decoder, Family, Fields, ImageEvent, Info, Input, InputKind, Output, OutputKind, Panel,
    ParamDefault, ParamKind, ParamSpec, Params, Sink, Status, Value,
};
use crate::dsp::{lowpass, Decimator};
use modes::{Chan, Colour, Layout, Mode};
use rustfft::num_complex::{Complex32, Complex64};
use std::collections::VecDeque;
use std::f64::consts::PI;

const PARAMS: &[ParamSpec] = &[
    // "" decodes every mode; a mode's id decodes only that one.
    ParamSpec { key: "mode", kind: ParamKind::Text, default: ParamDefault::Text("") },
    // Start a picture from its line syncs when its VIS header was missed.
    ParamSpec { key: "syncStart", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
    // How far off tune a VIS header is looked for, Hz either way.
    ParamSpec { key: "afcRangeHz", kind: ParamKind::Range { min: 0.0, max: 150.0 }, default: ParamDefault::Num(150.0) },
];

pub static INFO: Info = Info {
    id: "sstv",
    family: Family::Amateur,
    inputs: &[InputKind::Audio, InputKind::Iq],
    rate_hz: 12_000.0,
    bandwidth_hz: 3_000.0,
    outputs: &[OutputKind::Image, OutputKind::Meta],
    panel: Panel::Image,
    params: PARAMS,
    needs: &[],
    needs_for: &[],
    // IQ is centred on 1900 Hz: 1100-2300 Hz and the offset search need
    // about +-1.1 kHz of it.
    min_rate_hz: 4_000.0,
};

/// Audio below this cannot carry 2300 Hz and the filter's skirt.
const MIN_AUDIO_RATE: f64 = 6_000.0;
/// The mix's reference: the middle of sync (1200) and white (2300), so the
/// signal sits symmetrically about 0 Hz in the baseband.
const REF_HZ: f64 = 1750.0;
/// The baseband filter: cutoff, and half its length in working samples.
/// It feeds the syncs, the VIS header and the pixel filter (`PixFilter`,
/// which narrows it for the pixels as the SNR asks), so it passes every
/// tone (1100-2300 Hz, +-550 about the reference, and 150 Hz of tuning
/// error either way) with room for the fastest mode's FM sidebands, and
/// rejects the mix's image of audio (3.5 kHz away).
const LP_HZ: f64 = 1500.0;
const LP_HALF: usize = 24;
/// The working rate is the input's divided by a whole number, at least
/// this: 48 kHz to 12, 44.1 to 11.025, 12 and 8 untouched.
const WORK_MIN: f64 = 7_900.0;
/// The VIS and line-train detectors run at a quarter of that, about 3 kHz.
const SLOW_DIV_TARGET: f64 = 3_000.0;
const SLOW_LP_HZ: f64 = 1_000.0;
const SLOW_HALF: usize = 8;

/// VIS: the tone detectors' window (10 ms nulls the other VIS tones, 100
/// Hz apart); the template's mean tone fraction to believe a header, and
/// the least each leader and the start bit may score alone (a picture of
/// flat mid-grey in Martin has 1900 Hz with a short sync in it, which
/// passes the mean on the leaders alone but never the 30 ms start bit);
/// how far past the best candidate to look before believing it; the grid
/// of offsets tried; the most a bit may be in doubt (the difference of its
/// two tones' scores) for parity to correct it.
const VIS_WIN_MS: f64 = 10.0;
const VIS_MIN: f64 = 0.5;
const VIS_SEG_MIN: f64 = 0.35;
const VIS_SETTLE_MS: f64 = 20.0;
const VIS_STEP_HZ: f64 = 25.0;
const VIS_FLIP_MAX: f64 = 0.15;

/// Line syncs: the pulse's tone share to believe one (sstv.py's: noise
/// alone reaches about 0.3 at the best place in a search, a sync at 3 dB
/// about 0.6); the search either side of where it is expected, before the
/// clock is fitted (wide: also absorbs the 9 ms by which senders disagree
/// about Scottie's starting sync) and after.
const SYNC_MIN: f64 = 0.4;
const WIDE_MS: f64 = 15.0;
const WIDE_SHARE: f64 = 0.03;
const LOCK_MS: f64 = 3.0;
const LOCK_SHARE: f64 = 0.006;
/// A sync further than this from the fit is not believed...
const GATE_MS: f64 = 1.5;
/// ...unless this many strong ones in a row say the timing really moved.
const RELOCK: u32 = 3;
const STRONG: f64 = 0.6;
/// The fit's weights fall off with distance in lines as a Gaussian, its
/// width chosen per picture from these (`choose_span`).
const SPANS: [f64; 7] = [1.5, 3.0, 6.0, 12.0, 24.0, 48.0, 96.0];
/// Before there are enough syncs to choose, and never narrower than this
/// for the fit that predicts the next line from the ones before it.
const SPAN_DEFAULT: f64 = 48.0;
const SPAN_CAUSAL_MIN: f64 = 4.0;
/// Syncs before the fit is used, spanning at least FIT_SPAN lines.
const FIT_MIN: usize = 4;
const FIT_SPAN: u32 = 3;
/// The fitted line may differ from the mode's by at most this (1 %, fifty
/// times any real sound card): beyond it the fit has locked onto something
/// that is not the sync train (Icom's decoder.py).
const MAX_CLOCK: f64 = 0.01;
/// AFC through the picture: the share of each strong sync's measured
/// offset taken.
const AFC_GAIN: f64 = 0.1;

/// A picture with no VIS header ends when its syncs stop for this many
/// lines (sstv.py's), or for PARTIAL_LOST_S if that is fewer lines (at
/// least two): four of Scottie DX's lines are four seconds, and a picture
/// joined part way can only end by its syncs stopping. One with a header
/// ends after LOST_LINES and LOST_S, so a fade does not end it but a
/// station that has stopped does.
const PARTIAL_LOST: u32 = 4;
const PARTIAL_LOST_S: f64 = 2.0;
/// A partial picture shorter than this many lines leaves no file.
const PARTIAL_MIN_ROWS: u32 = 8;
const LOST_LINES: u32 = 16;
const LOST_S: f64 = 15.0;

/// Starting from line syncs (sstv.py's figures): at least TRAIN_LINES
/// lines and TRAIN_S of pulses at one mode's period, mean tone fraction
/// TRAIN_MIN, at most TRAIN_MISS missing, nothing at 1200 Hz half a line
/// from any of them; looked for every TRAIN_EVERY_S; detectors no longer
/// than 9 ms (a PD sync's full 20 ms window would null it 50 Hz out), at
/// three offsets 60 Hz apart.
const TRAIN_LINES: usize = 5;
const TRAIN_S: f64 = 1.5;
const TRAIN_MIN: f64 = 0.6;
const TRAIN_MISS: usize = 1;
const TRAIN_WIN_MS: f64 = 9.0;
const TRAIN_EVERY_S: f64 = 0.25;
const TRAIN_OFFSETS: [f64; 3] = [-60.0, 0.0, 60.0];

/// Rows re-sent at the end go in events of at most this many.
const RESEND_ROWS: usize = 32;
/// A row is re-sent when the final reading moves a pixel by more than this.
const RESEND_DIFF: i32 = 6;
/// Meta while receiving, every this many lines.
const META_EVERY: u32 = 16;

#[derive(Clone, Debug)]
struct Config {
    only: Option<&'static Mode>,
    sync_start: bool,
    afc_range: f64,
}

impl Default for Config {
    fn default() -> Self {
        Config { only: None, sync_start: true, afc_range: 150.0 }
    }
}

// ---------------------------------------------------------------- detectors

/// A sliding tone detector: for each window of `n` samples, the share of
/// its power that is a tone at `hz` (relative to the stream's reference):
/// a quadrature mix and a boxcar, the filter matched to an n-sample burst
/// (sstv.py's `_tone_ratio`), normalised by the window's own power so one
/// threshold serves a strong signal and a weak one.
#[derive(Clone, Debug)]
struct Tone {
    n: usize,
    rot: Complex64,
    ph: Complex64,
    ring: VecDeque<Complex64>,
    sum: Complex64,
    count: u64,
}

impl Tone {
    fn new(hz: f64, n: usize, rate: f64) -> Tone {
        let w = -2.0 * PI * hz / rate;
        Tone {
            n,
            rot: Complex64::new(w.cos(), w.sin()),
            ph: Complex64::new(1.0, 0.0),
            ring: VecDeque::with_capacity(n + 1),
            sum: Complex64::new(0.0, 0.0),
            count: 0,
        }
    }

    /// Push a sample; the window's |sum|^2 once it is full.
    fn push(&mut self, z: Complex64) -> Option<f64> {
        let m = z * self.ph;
        self.ph *= self.rot;
        self.count += 1;
        if self.count % 1024 == 0 {
            self.ph /= self.ph.norm();
        }
        self.ring.push_back(m);
        self.sum += m;
        if self.ring.len() > self.n {
            self.sum -= self.ring.pop_front().unwrap();
        }
        if self.count % 65_536 == 0 {
            // Keep the running sum exact over hours.
            self.sum = self.ring.iter().sum();
        }
        (self.ring.len() == self.n).then(|| self.sum.norm_sqr())
    }
}

/// Window power, alongside the tones.
#[derive(Clone, Debug)]
struct Power {
    n: usize,
    ring: VecDeque<f64>,
    sum: f64,
    count: u64,
}

impl Power {
    fn new(n: usize) -> Power {
        Power { n, ring: VecDeque::with_capacity(n + 1), sum: 0.0, count: 0 }
    }
    fn push(&mut self, z: Complex64) -> Option<f64> {
        let p = z.norm_sqr();
        self.ring.push_back(p);
        self.sum += p;
        self.count += 1;
        if self.ring.len() > self.n {
            self.sum -= self.ring.pop_front().unwrap();
        }
        if self.count % 65_536 == 0 {
            self.sum = self.ring.iter().sum();
        }
        (self.ring.len() == self.n).then_some(self.sum.max(1e-30))
    }
}

/// A VIS header found: where it starts (slow samples, fractional), the
/// tuning offset tried, the code, and whether its parity held.
#[derive(Clone, Copy, Debug)]
struct VisFound {
    start: f64,
    delta: f64,
    code: u8,
    parity_ok: bool,
}

#[derive(Clone, Copy, Debug)]
struct Cand {
    s: u64,
    delta_i: usize,
    score: f64,
}

/// The VIS detector on the slow stream: a bank of tone detectors at the
/// header's four tones, each at every offset in the grid, their ratios
/// kept as running sums so the template's segment means are two lookups.
struct Vis {
    sr: f64,
    n: usize,
    deltas: Vec<f64>,
    /// Detector index for (tone, offset): tones 1100, 1200, 1300, 1900.
    idx: Vec<[usize; 4]>,
    tones: Vec<Tone>,
    power: Power,
    /// cum[t][i] = sum of tone t's ratio over windows starting at
    /// base .. base + i.
    cum: Vec<Vec<f64>>,
    base: u64,
    next_cand: u64,
    best: Option<Cand>,
    // Template geometry, slow samples.
    lead: usize,
    brk: usize,
    start_bit: usize,
    bit: f64,
    header: usize,
    settle: u64,
}

impl Vis {
    fn new(sr: f64, afc_range: f64) -> Vis {
        let n = (VIS_WIN_MS / 1000.0 * sr).round() as usize;
        let k = (afc_range / VIS_STEP_HZ).floor() as i32;
        let deltas: Vec<f64> = (-k..=k).map(|i| i as f64 * VIS_STEP_HZ).collect();
        let mut freqs: Vec<f64> = Vec::new();
        let mut idx = Vec::new();
        for &d in &deltas {
            let mut row = [0usize; 4];
            for (t, base) in [modes::VIS_ONE_HZ, modes::SYNC_HZ, modes::VIS_ZERO_HZ, modes::LEADER_HZ].iter().enumerate() {
                let f = base + d;
                row[t] = match freqs.iter().position(|x| (x - f).abs() < 1e-9) {
                    Some(i) => i,
                    None => {
                        freqs.push(f);
                        freqs.len() - 1
                    }
                };
            }
            idx.push(row);
        }
        let tones: Vec<Tone> = freqs.iter().map(|&f| Tone::new(f - REF_HZ, n, sr)).collect();
        let ms = |x: f64| (x / 1000.0 * sr).round() as usize;
        Vis {
            sr,
            n,
            cum: vec![vec![0.0]; tones.len()],
            tones,
            idx,
            deltas,
            power: Power::new(n),
            base: 0,
            next_cand: 0,
            best: None,
            lead: ms(modes::LEADER_MS),
            brk: ms(modes::BREAK_MS),
            start_bit: ms(modes::BIT_MS),
            bit: modes::BIT_MS / 1000.0 * sr,
            header: ms(modes::VIS_MS),
            settle: ms(VIS_SETTLE_MS) as u64,
        }
    }

    /// Windows whose ratios are known: starts base .. base + have.
    fn have(&self) -> u64 {
        self.cum[0].len() as u64 - 1
    }

    /// Mean ratio of detector `t` over windows lying wholly inside
    /// [s + a, s + b) (sstv.py's `seg`).
    fn mean(&self, t: usize, s: u64, a: usize, b: usize) -> f64 {
        let lo = s + a as u64 - self.base;
        let hi = (s + b as u64 + 1).saturating_sub(self.n as u64).max(s + a as u64 + 1) - self.base;
        let c = &self.cum[t];
        (c[hi as usize] - c[lo as usize]) / (hi - lo) as f64
    }

    fn template(&self, s: u64, di: usize) -> (f64, bool) {
        let [_, sync, _, lead] = self.idx[di];
        let (l, b, sb) = (self.lead, self.brk, self.start_bit);
        let l1 = self.mean(lead, s, 0, l);
        let br = self.mean(sync, s, l, l + b);
        let l2 = self.mean(lead, s, l + b, 2 * l + b);
        let st = self.mean(sync, s, 2 * l + b, 2 * l + b + sb);
        ((l1 + br + l2 + st) / 4.0, l1 >= VIS_SEG_MIN && l2 >= VIS_SEG_MIN && st >= VIS_SEG_MIN)
    }

    /// Push slow samples; any header found, in order.
    fn push(&mut self, zs: &[Complex32], found: &mut Vec<VisFound>) {
        for &z in zs {
            let z = Complex64::new(z.re as f64, z.im as f64);
            let p = self.power.push(z);
            for (t, tone) in self.tones.iter_mut().enumerate() {
                if let (Some(a), Some(p)) = (tone.push(z), p) {
                    let r = a / (self.n as f64 * p);
                    let c = &mut self.cum[t];
                    let last = *c.last().unwrap();
                    c.push(last + r);
                }
            }
            // Every candidate whose whole header (and the settle beyond the
            // best so far) has its windows known.
            while self.next_cand + self.header as u64 <= self.base + self.have() {
                let s = self.next_cand;
                self.next_cand += 1;
                if s < self.base {
                    continue;
                }
                let mut best_here: Option<Cand> = None;
                for di in 0..self.deltas.len() {
                    let (score, segs_ok) = self.template(s, di);
                    if segs_ok && score >= VIS_MIN && best_here.is_none_or(|b| score > b.score) {
                        best_here = Some(Cand { s, delta_i: di, score });
                    }
                }
                if let Some(c) = best_here {
                    if self.best.is_none_or(|b| c.score > b.score) {
                        self.best = Some(c);
                    }
                }
                if let Some(b) = self.best {
                    if s >= b.s + self.settle {
                        self.best = None;
                        found.push(self.decide(b));
                        // Nothing inside this header can start another.
                        self.next_cand = self.next_cand.max(b.s + self.header as u64);
                    }
                }
            }
        }
        self.trim();
    }

    fn decide(&self, c: Cand) -> VisFound {
        let [one, _, zero, _] = self.idx[c.delta_i];
        // The peak to a fraction of a slow sample, from its neighbours.
        let at = |s: u64| if s >= self.base { self.template(s, c.delta_i).0 } else { 0.0 };
        let (ym, y0, yp) = (at(c.s.saturating_sub(1)), c.score, at(c.s + 1));
        let den = ym - 2.0 * y0 + yp;
        let frac = if den < 0.0 { (0.5 * (ym - yp) / den).clamp(-0.5, 0.5) } else { 0.0 };
        let b0 = (2 * self.lead + self.brk + self.start_bit) as f64;
        let guard = 2.5 / 1000.0 * self.sr;
        // Each bit's two tones' scores; a bit is 1 where 1100 Hz wins.
        let margins: Vec<f64> = (0..8)
            .map(|k| {
                let a = (b0 + k as f64 * self.bit + guard).round() as usize;
                let b = (b0 + (k + 1) as f64 * self.bit - guard).round() as usize;
                self.mean(one, c.s, a, b) - self.mean(zero, c.s, a, b)
            })
            .collect();
        let bits = |flip: Option<usize>| -> (u8, bool) {
            let mut code = 0u8;
            let mut ones = 0;
            for (k, m) in margins.iter().enumerate() {
                if (*m > 0.0) != (flip == Some(k)) {
                    ones += 1;
                    if k < 7 {
                        code |= 1 << k;
                    }
                }
            }
            (code, ones % 2 == 0)
        };
        let (mut code, mut parity_ok) = bits(None);
        if !parity_ok {
            // One bit wrong, most likely the one most in doubt (a fade
            // across it): parity says one is, its score says which. Taken
            // only when that bit was close and the code is a mode.
            let k = (0..8).min_by(|a, b| margins[*a].abs().total_cmp(&margins[*b].abs())).unwrap();
            let (c2, ok2) = bits(Some(k));
            if ok2 && margins[k].abs() < VIS_FLIP_MAX && modes::by_vis(c2).is_some() {
                (code, parity_ok) = (c2, true);
            }
        }
        VisFound { start: c.s as f64 + frac, delta: self.deltas[c.delta_i], code, parity_ok }
    }

    fn trim(&mut self) {
        // Keep a header and a little of windows behind the next candidate.
        let keep_from = self.next_cand.saturating_sub(self.settle + 2);
        if keep_from > self.base + 4096 {
            let k = (keep_from - self.base) as usize;
            for c in &mut self.cum {
                let off = c[k];
                c.drain(..k);
                c.iter_mut().for_each(|x| *x -= off);
            }
            self.base += k as u64;
        }
    }
}

/// The line-train detector on the slow stream: 1200 Hz detectors at two
/// window lengths (Martin's 4.862 ms sync, and 9 ms for the rest) and
/// three offsets, their ratios kept for the longest train any mode needs.
struct Train {
    sr: f64,
    /// (window samples, offset, detector, ratios), ratios[i] for the window
    /// starting at base + i.
    streams: Vec<(usize, f64, Tone, Power, Vec<f32>)>,
    base: u64,
    keep: usize,
    pushed: u64,
    looked: u64,
    /// No train may start before this slow sample (a picture just read).
    from: u64,
}

impl Train {
    fn new(sr: f64) -> Train {
        let mut streams = Vec::new();
        for win_ms in [4.862, TRAIN_WIN_MS] {
            let n = (win_ms / 1000.0 * sr).round() as usize;
            for d in TRAIN_OFFSETS {
                streams.push((n, d, Tone::new(modes::SYNC_HZ + d - REF_HZ, n, sr), Power::new(n), Vec::new()));
            }
        }
        // The longest train any mode needs, and a line more.
        let keep = modes::MODES
            .iter()
            .map(|m| {
                let p = m.layout().line_ms / 1000.0 * sr;
                ((train_lines(p, sr) + 2) as f64 * p).ceil() as usize
            })
            .max()
            .unwrap();
        Train { sr, streams, base: 0, keep, pushed: 0, looked: 0, from: 0 }
    }

    fn push(&mut self, zs: &[Complex32]) {
        for &z in zs {
            let z = Complex64::new(z.re as f64, z.im as f64);
            for (n, _, tone, pw, r) in &mut self.streams {
                if let (Some(a), Some(p)) = (tone.push(z), pw.push(z)) {
                    r.push((a / (*n as f64 * p)) as f32);
                }
            }
            self.pushed += 1;
        }
        let len = self.streams.iter().map(|s| s.4.len()).min().unwrap_or(0);
        if len > self.keep + 4096 {
            let k = len - self.keep;
            for s in &mut self.streams {
                s.4.drain(..k);
            }
            self.base += k as u64;
        }
    }

    /// Look for a train ending now, among `modes`: (mode, the slow sample
    /// where a line's sync pulse starts, the offset of the detector).
    fn look(&mut self, modes: &[&'static Mode]) -> Option<(&'static Mode, f64, f64)> {
        if (self.pushed - self.looked) as f64 <= TRAIN_EVERY_S * self.sr {
            return None;
        }
        self.looked = self.pushed;
        let mut best: Option<(f64, &'static Mode, f64, f64)> = None;
        for m in modes {
            let lay = m.layout();
            let p = lay.line_ms / 1000.0 * self.sr;
            let k = train_lines(p, self.sr);
            let sync_n = lay.sync_ms / 1000.0 * self.sr;
            let short = lay.sync_ms < TRAIN_WIN_MS - 1.0;
            for (n, d, _, _, r) in &self.streams {
                if (*n as f64 > TRAIN_WIN_MS / 1000.0 * self.sr - 1.0) == short {
                    continue;
                }
                let len = r.len() as f64;
                let pc = p.ceil() as usize;
                let base0 = len - (k as f64 - 1.0) * p - pc as f64;
                if base0 < 0.0 || self.base as f64 + base0 < self.from as f64 {
                    continue;
                }
                let at = |i: f64| r[(i.round().max(0.0) as usize).min(r.len() - 1)] as f64;
                let scores: Vec<f64> = (0..pc)
                    .map(|phi| {
                        let mut sum = 0.0;
                        let mut miss = 0;
                        let mut gap = 0.0;
                        for j in 0..k {
                            let i = base0 + phi as f64 + p * j as f64;
                            let v = at(i);
                            sum += v;
                            if v < SYNC_MIN {
                                miss += 1;
                            }
                            gap += at(i + p / 2.0);
                        }
                        // Half a line from every pulse is picture, never
                        // 1200 Hz: a steady carrier passes for every mode.
                        if miss <= TRAIN_MISS && gap / (k as f64) < SYNC_MIN / 2.0 {
                            sum / k as f64
                        } else {
                            0.0
                        }
                    })
                    .collect();
                let kk = (0..pc).max_by(|a, b| scores[*a].total_cmp(&scores[*b])).unwrap();
                let s = scores[kk];
                if s < TRAIN_MIN || best.is_some_and(|b| s <= b.0) {
                    continue;
                }
                // A window shorter than the pulse scores alike all along
                // it: the middle of that plateau, less half the difference.
                let hi = s * 0.9;
                let (mut lo_k, mut hi_k) = (kk, kk);
                while lo_k > 0 && scores[lo_k - 1] >= hi {
                    lo_k -= 1;
                }
                while hi_k + 1 < pc && scores[hi_k + 1] >= hi {
                    hi_k += 1;
                }
                let pulse = base0 + (lo_k + hi_k) as f64 / 2.0 - (sync_n - *n as f64) / 2.0;
                best = Some((s, m, self.base as f64 + pulse, *d));
            }
        }
        best.map(|(_, m, p, d)| (m, p, d))
    }
}

fn train_lines(p: f64, sr: f64) -> usize {
    TRAIN_LINES.max((TRAIN_S * sr / p).ceil() as usize)
}

// ---------------------------------------------------------------- picture

/// A sync measured: line `k` starts at `pos` (working samples), believed
/// with weight `q`.
#[derive(Clone, Copy, Debug)]
struct Obs {
    k: u32,
    pos: f64,
    q: f64,
}

/// The known stretch of tones around a line's sync, (ms, Hz) in order, and
/// how long the part before the pulse is: the tones up to the sync from
/// the last scan before it (Martin's last separator, from the line before)
/// and from it to the next scan (the porch).
#[derive(Clone, Debug)]
struct Template {
    segs: Vec<(f64, f64)>,
    pre_ms: f64,
}

fn template(mode: &Mode) -> Template {
    let segs = mode.segs;
    let s = segs.iter().position(|x| matches!(x, modes::Seg::Sync(_))).expect("every mode has a sync");
    let mut pre = Vec::new();
    let mut i = s;
    loop {
        i = if i == 0 { segs.len() - 1 } else { i - 1 };
        match segs[i] {
            modes::Seg::Tone(ms, hz) if i != s => pre.push((ms, hz)),
            _ => break,
        }
    }
    pre.reverse();
    let pre_ms = pre.iter().map(|x| x.0).sum();
    let mut v = pre;
    if let modes::Seg::Sync(ms) = segs[s] {
        v.push((ms, modes::SYNC_HZ));
    }
    for x in &segs[s + 1..] {
        match *x {
            modes::Seg::Tone(ms, hz) => v.push((ms, hz)),
            _ => break,
        }
    }
    Template { segs: v, pre_ms }
}

/// A sync found: where its pulse starts, the match (0-1), the tone's and
/// the noise's power, and the tuning error it shows.
#[derive(Clone, Copy, Debug)]
struct Hit {
    pos: f64,
    q: f64,
    sig: f64,
    noise: f64,
    err_hz: f64,
}

/// The weighted straight-line fit through `obs` near line `k` (only those
/// at or before it when `causal`; without the one at `skip`), weights
/// falling off over `span` lines: (start of line k, line period). None
/// with too few, or a period further than MAX_CLOCK from `p_nom`.
fn fit_with(obs: &[Obs], k: u32, causal: bool, p_nom: f64, span: f64, skip: Option<usize>) -> Option<(f64, f64)> {
    let (mut sw, mut sx, mut sy, mut sxx, mut sxy) = (0.0, 0.0, 0.0, 0.0, 0.0);
    let (mut n, mut kmin, mut kmax) = (0usize, u32::MAX, 0u32);
    for (i, o) in obs.iter().enumerate() {
        if (causal && o.k > k) || skip == Some(i) {
            continue;
        }
        let x = o.k as f64 - k as f64;
        if x.abs() > 5.0 * span {
            continue;
        }
        let w = o.q * (-0.5 * (x / span).powi(2)).exp();
        if w < 1e-9 {
            continue;
        }
        n += 1;
        kmin = kmin.min(o.k);
        kmax = kmax.max(o.k);
        sw += w;
        sx += w * x;
        sy += w * o.pos;
        sxx += w * x * x;
        sxy += w * x * o.pos;
    }
    if n < FIT_MIN || kmax - kmin < FIT_SPAN {
        return None;
    }
    let den = sw * sxx - sx * sx;
    if den <= 1e-12 * sw * sw {
        return None;
    }
    let b = (sw * sxy - sx * sy) / den;
    if (b / p_nom - 1.0).abs() > MAX_CLOCK {
        return None;
    }
    let a = (sy - b * sx) / sw;
    Some((a, b))
}

fn fit(obs: &[Obs], k: u32, causal: bool, p_nom: f64, span: f64) -> Option<(f64, f64)> {
    fit_with(obs, k, causal, p_nom, if causal { span.max(SPAN_CAUSAL_MIN) } else { span }, None)
}

/// How far the fit should look, chosen by leave-one-out cross-validation:
/// the span whose fit, made without each sync, best predicts it. A
/// sender's steady clock error and noise alone choose the longest, which
/// averages the noise away; a path whose delay moves (HF multipath, where
/// one path and then the other is the stronger, 0.5-1 ms apart) chooses a
/// short one, which follows it as sstv.py's line-by-line loop did.
fn choose_span(obs: &[Obs], p_nom: f64) -> f64 {
    if obs.len() < 12 {
        return SPAN_DEFAULT;
    }
    let mut best = (f64::MAX, SPAN_DEFAULT);
    for &span in &SPANS {
        let (mut e, mut w) = (0.0, 0.0);
        for (i, o) in obs.iter().enumerate() {
            if let Some((a, _)) = fit_with(obs, o.k, false, p_nom, span, Some(i)) {
                e += o.q * (o.pos - a).powi(2);
                w += o.q;
            }
        }
        // Ties (to 2 %) go to the longer span.
        if w > 0.0 && e / w <= best.0 * 1.02 {
            best = (e / w, span);
        }
    }
    best.1
}

/// The picture band's middle (black 1500 to white 2300).
const PIX_HZ: f64 = 1900.0;

/// The filter pixels are read through, centred on the picture band and
/// narrower than the baseband's when the signal is weak. Below an FM
/// discriminator's threshold every hertz of noise let in costs clicks, and
/// the filter's width is what sets that threshold; above it, width is
/// resolution. Measured on every mode (8, 15 and 40 dB, 500-1400 Hz), the
/// best width hardly depends on the mode and moves steadily with the SNR:
/// about 500 Hz at 8 dB, 700 at 15, 1400 and more at 40 (Martin M1: 22.0,
/// 25.9 and 35.7 dB PSNR there, against 16.7, 23.8 and 35.7 through one
/// fixed 1400 Hz). So each line is read through `pix_width` of the
/// picture's SNR so far (`Picture::snr_level`).
#[derive(Clone, Debug)]
struct PixFilter {
    taps: Vec<f64>,
    /// The noise's correlation from one sample to the next after it, and
    /// its power over the baseband's (for the pull `View::freq` takes off).
    rho: f64,
    noise_gain: f64,
}

/// The pixel filter's width (Hz either side) for an SNR in 2.5 kHz and a
/// mode whose luminance (or green) pixel lasts `px_ms`.
///
/// Up to 25 dB the same for every mode. Above it each mode widens at its
/// own pace: a long pixel averages more samples, so its noise stops
/// mattering sooner. Measured (PSNR over four pictures, best width): at 30
/// dB PD120 (0.19 ms pixels) 1100 Hz, Robot 36 (0.275 ms) 1500, Martin M1
/// (0.458 ms) 1800 or more; at 35 dB PD120 1600. The slope is that fit.
fn pix_width(snr_db: f64, px_ms: f64) -> f64 {
    let w = if snr_db <= 25.0 {
        500.0 + 28.0 * (snr_db - 8.0)
    } else {
        let slope = (25.0 + 540.0 * (px_ms - 0.19)).clamp(25.0, 400.0);
        976.0 + slope * (snr_db - 25.0)
    };
    w.clamp(550.0, 2000.0)
}

impl PixFilter {
    fn new(fc: f64, wr: f64, noise_bw: f64) -> PixFilter {
        let fc = fc.min(0.45 * wr);
        let half = ((1.6 * wr / fc).ceil() as usize).clamp(8, 64);
        let taps: Vec<f64> = lowpass(2 * half + 1, fc, wr).into_iter().map(|t| t as f64).collect();
        let e: f64 = taps.iter().map(|t| t * t).sum();
        let rho = taps.windows(2).map(|w| w[0] * w[1]).sum::<f64>() / e;
        PixFilter { taps, rho, noise_gain: wr * e / noise_bw }
    }
}

/// A stretch of the pixel filter's output, `base` the working sample of
/// its first.
struct View {
    base: i64,
    z: Vec<Complex64>,
    wr: f64,
}

impl View {
    fn at(&self, j: i64) -> Complex64 {
        let i = j - self.base;
        if i < 0 || i as usize >= self.z.len() {
            return Complex64::new(0.0, 0.0);
        }
        self.z[i as usize]
    }

    /// The integral of z[n] conj z[n-1] over working time [a, b] (the
    /// increment from n-1 to n spans [n-1, n]).
    fn sum_d(&self, a: f64, b: f64) -> Complex64 {
        let mut acc = Complex64::new(0.0, 0.0);
        let j0 = a.floor() as i64 + 1;
        let j1 = b.ceil() as i64;
        let mut prev = self.at(j0 - 1);
        for j in j0..=j1 {
            let cur = self.at(j);
            let lo = a.max((j - 1) as f64);
            let hi = b.min(j as f64);
            if hi > lo {
                acc += cur * prev.conj() * (hi - lo);
            }
            prev = cur;
        }
        acc
    }

    /// Mean frequency over [a, b], Hz, as sent (the offset taken off).
    ///
    /// The filters make the noise in neighbouring samples alike, so noise
    /// alone adds `bias` (its power times that likeness: real, positive)
    /// to every z[n] conj z[n-1], pulling each pixel towards the middle
    /// grey: 50 Hz, sixteen grey levels, at 10 dB. It is known, so it is
    /// taken off.
    fn freq(&self, a: f64, b: f64, delta: f64, bias: f64) -> f64 {
        (self.sum_d(a, b) - (b - a) * bias).arg() * self.wr / (2.0 * PI) + PIX_HZ - delta
    }

    /// `n` pixels over [a, a + dur), each the mean over its exact span.
    fn pixels(&self, a: f64, dur: f64, n: usize, delta: f64, bias: f64) -> Vec<f32> {
        (0..n)
            .map(|i| {
                let e0 = a + dur * i as f64 / n as f64;
                let e1 = a + dur * (i + 1) as f64 / n as f64;
                let f = self.freq(e0, e1, delta, bias);
                ((f - modes::BLACK_HZ) / (modes::WHITE_HZ - modes::BLACK_HZ) * 255.0).clamp(0.0, 255.0) as f32
            })
            .collect()
    }
}

/// One line read: each scan's pixels (0-255, unrounded), and for Robot 36
/// whether its separator said R-Y.
#[derive(Clone, Debug)]
struct LineData {
    scans: Vec<(Chan, Vec<f32>)>,
    sep_ry: Option<bool>,
}

impl Picture {
    /// The picture's SNR so far: its syncs' mean power over the noise
    /// floor. Not each line's: in a fade a line's SNR drops, and narrowing
    /// its filter then cost more rows their place than it saved in noise
    /// (multipath moves the picture's edges against its syncs, and a
    /// narrower filter stretches each edge over more of that): Martin M1
    /// in slow fading kept 94 % of its rows in place so, 97 % this way.
    fn snr_level(&self, noise_bw: f64) -> f64 {
        if self.found < 4 {
            return self.snr0;
        }
        snr_of(self.sig / self.found as f64, self.noise_pow(), noise_bw).unwrap_or(self.snr0)
    }

    /// The receiver's noise floor: the quietest quarter of the lines'
    /// measurements. Each sync's is what the template does not explain,
    /// which under multipath includes the echo (in proportion to the
    /// signal), so their mean overstated the noise and the pixels were
    /// pulled back too far: Martin M1 in slow fading lost 2 dB to it. The
    /// floor does not change within a picture; the echo's share does.
    fn noise_pow(&self) -> f64 {
        if self.noises.len() < 4 {
            return self.noise0;
        }
        let mut v = self.noises.clone();
        let k = v.len() / 4;
        *v.select_nth_unstable_by(k, |a, b| a.total_cmp(b)).1
    }
}

impl LineData {
    fn get(&self, c: Chan) -> &[f32] {
        &self.scans.iter().find(|s| s.0 == c).unwrap().1
    }
}

fn to_u8(v: f64) -> u8 {
    v.round().clamp(0.0, 255.0) as u8
}

fn yc_row(y: &[f32], ry: &[f32], by: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(y.len() * 3);
    for i in 0..y.len() {
        let [r, g, b] = modes::yc_to_rgb(y[i] as f64, ry[i] as f64, by[i] as f64);
        out.extend_from_slice(&[to_u8(r), to_u8(g), to_u8(b)]);
    }
    out
}

/// Lines to rows, in order: RGB and PD at once, Robot 36 once both lines
/// of a pair (R-Y then B-Y) are in.
struct Assembler {
    mode: &'static Mode,
    /// Robot 36: the R-Y line waiting for its B-Y one, and the last chroma
    /// of each kind for a line whose partner was lost.
    pending: Option<(u32, Vec<f32>, Vec<f32>)>,
    last_ry: Option<Vec<f32>>,
    last_by: Option<Vec<f32>>,
}

impl Assembler {
    fn new(mode: &'static Mode) -> Self {
        Assembler { mode, pending: None, last_ry: None, last_by: None }
    }

    fn neutral(&self) -> Vec<f32> {
        vec![128.0; self.mode.width as usize]
    }

    /// Line `k` read as `d`; Robot 36's chroma kind `is_ry`. Rows out.
    fn line(&mut self, k: u32, d: &LineData, is_ry: bool) -> Vec<(u32, Vec<u8>)> {
        let m = self.mode;
        match (m.colour, m.rows) {
            (Colour::Rgb, _) => {
                let (r, g, b) = (d.get(Chan::R), d.get(Chan::G), d.get(Chan::B));
                let row = (0..r.len()).flat_map(|i| [to_u8(r[i] as f64), to_u8(g[i] as f64), to_u8(b[i] as f64)]).collect();
                vec![(k, row)]
            }
            (Colour::Yc, 2) => {
                let (ry, by) = (d.get(Chan::Ry), d.get(Chan::By));
                vec![(2 * k, yc_row(d.get(Chan::Y0), ry, by)), (2 * k + 1, yc_row(d.get(Chan::Y1), ry, by))]
            }
            _ if m.id == "robot36" => {
                let (y, c) = (d.get(Chan::Y0).to_vec(), d.get(Chan::Chroma).to_vec());
                let mut out = Vec::new();
                if is_ry {
                    out.extend(self.flush());
                    self.pending = Some((k, y, c.clone()));
                    self.last_ry = Some(c);
                } else {
                    match self.pending.take() {
                        Some((pk, py, pry)) if pk + 1 == k => {
                            out.push((pk, yc_row(&py, &pry, &c)));
                            out.push((k, yc_row(&y, &pry, &c)));
                        }
                        other => {
                            if let Some(p) = other {
                                self.pending = Some(p);
                                out.extend(self.flush());
                            }
                            let ry = self.last_ry.clone().unwrap_or_else(|| self.neutral());
                            out.push((k, yc_row(&y, &ry, &c)));
                        }
                    }
                    self.last_by = Some(c);
                }
                out
            }
            _ => {
                // Robot 72: everything in the line.
                vec![(k, yc_row(d.get(Chan::Y0), d.get(Chan::Ry), d.get(Chan::By)))]
            }
        }
    }

    /// A pending Robot 36 R-Y line whose B-Y partner never came.
    fn flush(&mut self) -> Vec<(u32, Vec<u8>)> {
        match self.pending.take() {
            Some((k, y, ry)) => {
                let by = self.last_by.clone().unwrap_or_else(|| self.neutral());
                vec![(k, yc_row(&y, &ry, &by))]
            }
            None => Vec::new(),
        }
    }
}

struct Picture {
    mode: &'static Mode,
    lay: Layout,
    tpl: Template,
    /// The SNR from the header (dB in 2.5 kHz), until the syncs say, and
    /// the pixel filter's width each line was read with.
    snr0: f64,
    widths: Vec<f64>,
    /// The first scan's pixel, ms (`pix_width`).
    px_ms: f64,
    /// Each sync's noise measurement (`noise_pow`).
    noises: Vec<f64>,
    /// The fit's span (`choose_span`), chosen again every META_EVERY lines.
    span: f64,
    /// Noise power in the baseband (per sample), for taking the noise's
    /// pull off each pixel: from the header (or train), then the syncs.
    noise0: f64,
    vis: Option<u8>,
    /// Working samples a line, nominally.
    p_nom: f64,
    /// Where line 0 starts, as first believed.
    origin: f64,
    /// The next line to read.
    k: u32,
    obs: Vec<Obs>,
    /// Each line's start as read (for cutting the picture at a new header).
    used: Vec<f64>,
    asm: Assembler,
    /// Rows as sent, and how many are filled.
    rgb: Vec<u8>,
    rows: u32,
    /// Lines in a row with no sync, and strong syncs in a row the fit
    /// would not take.
    miss_run: u32,
    reject_run: u32,
    found: u64,
    missed: u64,
    /// Robot 36 without a header: separator votes for "even lines are R-Y".
    parity_vote: i64,
    delta: f64,
    sig: f64,
    noise: f64,
    t_start: f64,
    /// Rows to send at the end of this feed (from, rgb).
    out_rows: Vec<(u32, Vec<u8>)>,
}

// ---------------------------------------------------------------- decoder

pub struct Sstv {
    cfg: Config,
    // Front end.
    kind: Option<InputKind>,
    rate: f64,
    d: usize,
    wr: f64,
    rot: Complex64,
    ph: Complex64,
    mixed: u64,
    gain: f32,
    lp: Option<Decimator>,
    lp_drop: usize,
    /// The baseband filter's noise bandwidth, Hz, for the SNR.
    noise_bw: f64,
    next_index: Option<u64>,
    /// The input index of working sample 0, and the latest block's (t0, index).
    index0: u64,
    t_block: (f64, u64),
    // Working stream.
    z: Vec<Complex32>,
    z_base: u64,
    // Slow stream.
    d2: usize,
    slow_lp: Option<Decimator>,
    slow_drop: usize,
    vis: Option<Vis>,
    train: Option<Train>,
    // Picture.
    pic: Option<Picture>,
    /// The last META_EVERY multiple of lines a meta went out at.
    meta_due: u32,
    /// Headers heard during a picture, not yet judged (`settle_pending`),
    /// and the sync pulses of the picture last ended, to judge them by.
    pending: Vec<VisFound>,
    prev_syncs: Vec<f64>,
    // Counters.
    images: u64,
    partials: u64,
    lines: u64,
    syncs_found: u64,
    syncs_missed: u64,
    vis_heard: u64,
    vis_bad: u64,
    last_snr: Option<f32>,
    last_offset: Option<f32>,
}

impl Default for Sstv {
    fn default() -> Self {
        Self::new()
    }
}

impl Sstv {
    pub fn new() -> Self {
        Sstv {
            cfg: Config::default(),
            kind: None,
            rate: 0.0,
            d: 1,
            wr: 0.0,
            rot: Complex64::new(1.0, 0.0),
            ph: Complex64::new(1.0, 0.0),
            mixed: 0,
            gain: 1.0,
            lp: None,
            lp_drop: 0,
            noise_bw: 1.0,
            next_index: None,
            index0: 0,
            t_block: (0.0, 0),
            z: Vec::new(),
            z_base: 0,
            d2: 1,
            slow_lp: None,
            slow_drop: 0,
            vis: None,
            train: None,
            pic: None,
            meta_due: 0,
            pending: Vec::new(),
            prev_syncs: Vec::new(),
            images: 0,
            partials: 0,
            lines: 0,
            syncs_found: 0,
            syncs_missed: 0,
            vis_heard: 0,
            vis_bad: 0,
            last_snr: None,
            last_offset: None,
        }
    }

    fn build(&mut self, kind: InputKind, rate: f64, index: u64) -> Result<(), DecodeError> {
        let min = if kind == InputKind::Audio { MIN_AUDIO_RATE } else { INFO.min_rate_hz };
        if !(rate.is_finite() && rate >= min) {
            return Err(DecodeError::BadInput("rate"));
        }
        self.kind = Some(kind);
        self.rate = rate;
        self.d = ((rate / WORK_MIN).floor() as usize).max(1);
        self.wr = rate / self.d as f64;
        // IQ is centred on the leader tone, audio on 0 Hz: mix each to REF.
        let centre = if kind == InputKind::Iq { tx::IQ_CENTRE_HZ } else { 0.0 };
        let w = -2.0 * PI * (REF_HZ - centre) / rate;
        self.rot = Complex64::new(w.cos(), w.sin());
        self.ph = Complex64::new(1.0, 0.0);
        self.mixed = 0;
        // Audio is mixed times two so a tone of amplitude A comes out at A,
        // as an IQ tone would (dsp::AudioToIq).
        self.gain = if kind == InputKind::Audio { 2.0 } else { 1.0 };
        // 2 D LP_HALF + 1 taps: an odd length whose delay is a whole number
        // of working samples, so working sample j is input sample j D.
        let taps = lowpass(2 * self.d * LP_HALF + 1, LP_HZ, rate);
        let e: f64 = taps.iter().map(|t| (*t as f64).powi(2)).sum();
        self.noise_bw = rate * e;
        self.lp = Some(Decimator::new(taps, self.d));
        self.lp_drop = LP_HALF;
        self.d2 = ((self.wr / SLOW_DIV_TARGET).floor() as usize).max(1);
        let sr = self.wr / self.d2 as f64;
        self.slow_lp = Some(Decimator::new(lowpass(2 * self.d2 * SLOW_HALF + 1, SLOW_LP_HZ, self.wr), self.d2));
        self.slow_drop = SLOW_HALF;
        self.vis = Some(Vis::new(sr, self.cfg.afc_range));
        self.train = Some(Train::new(sr));
        self.z.clear();
        self.z_base = 0;
        self.index0 = index;
        self.next_index = Some(index);
        self.pic = None;
        self.pending.clear();
        Ok(())
    }

    fn sr(&self) -> f64 {
        self.wr / self.d2 as f64
    }

    fn ms(&self, ms: f64) -> f64 {
        ms / 1000.0 * self.wr
    }

    /// UTC of working sample `j`.
    fn t_of(&self, j: f64) -> f64 {
        let (t0, idx) = self.t_block;
        t0 + (self.index0 as f64 + j * self.d as f64 - idx as f64) / self.rate
    }

    fn end(&self) -> u64 {
        self.z_base + self.z.len() as u64
    }

    fn zat(&self, j: i64) -> Complex64 {
        let i = j - self.z_base as i64;
        if i < 0 || i as usize >= self.z.len() {
            return Complex64::new(0.0, 0.0);
        }
        let z = self.z[i as usize];
        Complex64::new(z.re as f64, z.im as f64)
    }

    /// The baseband around [a, b] through the picture's pixel filter
    /// (`PixFilter`), mixed so the picture band's middle is at 0 Hz.
    fn view(&self, a: f64, b: f64, pf: &PixFilter) -> View {
        let h = pf.taps.len() / 2;
        let (j0, j1) = (a.floor() as i64 - 1, b.ceil() as i64 + 1);
        let om = -2.0 * PI * (PIX_HZ - REF_HZ) / self.wr;
        let mixed: Vec<Complex64> = (j0 - h as i64..=j1 + h as i64)
            .map(|m| {
                let ph = om * (m as f64);
                self.zat(m) * Complex64::new(ph.cos(), ph.sin())
            })
            .collect();
        let z = (0..=(j1 - j0) as usize)
            .map(|i| mixed[i..i + pf.taps.len()].iter().zip(&pf.taps).map(|(x, t)| x * *t).sum())
            .collect();
        View { base: j0, z, wr: self.wr }
    }

    /// The best match to the line's known stretch of tones around its sync
    /// (`tpl`: the separator before it, the pulse, the porch after) whose
    /// pulse starts within `w` of `expect`.
    ///
    /// A matched filter for each tone of the stretch, their energies
    /// summed, rather than a 1200 Hz detector for the pulse alone: the
    /// porch after a pulse is 1500 Hz, only 300 Hz away, so a pulse
    /// detector's score falls off more slowly on that side, and noise then
    /// picks its peak from a lopsided plateau (every PD120 sync 2-3 samples
    /// late at 20 dB, a pixel). With the porch matched too, each side has
    /// one slow and one quick edge, and the peak is symmetric. Energies,
    /// not one coherent sum across the tones: that was sharper still in
    /// noise, but multipath gives the channel a different phase at 1200 Hz
    /// than at 1500, which a coherent template can only explain by moving
    /// (300 Hz of phase is a time), and near a notch it moved Robot 36's
    /// rows three pixels where sstv.py's tone detector moved them none.
    fn matched(&self, expect: f64, w: f64, tpl: &Template, scale: f64, delta: f64) -> Option<Hit> {
        let wr = self.wr;
        let ms = |x: f64| x / 1000.0 * wr * scale;
        let start = expect - ms(tpl.pre_ms);
        let total: f64 = tpl.segs.iter().map(|s| ms(s.0)).sum();
        let j0 = start.ceil();
        let off0 = j0 - start;
        let n = ((start + total).floor() - j0) as usize + 1;
        // Each sample's segment, by the middle of the interval after it,
        // and each tone's phasor from its own segment's start.
        let mut seg_of = Vec::with_capacity(n);
        let mut t = Vec::with_capacity(n);
        let (mut acc, mut e) = (ms(tpl.segs[0].0), 0usize);
        let mut ph = Complex64::new(1.0, 0.0);
        for m in 0..n {
            let mid = off0 + m as f64 + 0.5;
            while e + 1 < tpl.segs.len() && mid >= acc {
                e += 1;
                acc += ms(tpl.segs[e].0);
            }
            seg_of.push(e);
            t.push(ph.conj());
            let om = 2.0 * PI * (tpl.segs[e].1 + delta - REF_HZ) / wr;
            ph *= Complex64::new(om.cos(), om.sin());
        }
        let nseg = tpl.segs.len();
        let counts: Vec<f64> = (0..nseg).map(|k| seg_of.iter().filter(|x| **x == k).count() as f64).collect();
        let sq: f64 = counts.iter().map(|n| n * n).sum();
        let wi = w.ceil() as i64;
        let base = j0 as i64;
        if base - wi < self.z_base as i64 + 1 || base + wi + n as i64 > self.end() as i64 {
            return None;
        }
        let zs: Vec<Complex64> = (base - wi..base + wi + n as i64).map(|j| self.zat(j)).collect();
        let score = |i: usize| -> (f64, f64, f64) {
            let mut c = vec![Complex64::new(0.0, 0.0); nseg];
            let mut p = 0.0;
            for m in 0..n {
                let z = zs[i + m];
                c[seg_of[m]] += z * t[m];
                p += z.norm_sqr();
            }
            // Placed by each tone's own share, which is symmetric (and 1
            // for a perfect match); judged below by the pulse's alone.
            let energy: f64 = c.iter().zip(&counts).map(|(c, n)| if *n > 0.0 { c.norm_sqr() / n } else { 0.0 }).sum();
            let sig = c.iter().map(|c| c.norm_sqr()).sum::<f64>() / sq;
            (if p > 1e-30 { energy / p } else { 0.0 }, sig, p)
        };
        let rs: Vec<f64> = (0..=(2 * wi) as usize).map(|i| score(i).0).collect();
        let k = (0..rs.len()).max_by(|a, b| rs[*a].total_cmp(&rs[*b]))?;
        let mut frac = 0.0;
        if k > 0 && k + 1 < rs.len() {
            let den = rs[k - 1] - 2.0 * rs[k] + rs[k + 1];
            if den < 0.0 {
                frac = (0.5 * (rs[k - 1] - rs[k + 1]) / den).clamp(-0.5, 0.5);
            }
        }
        let (_, sig, p) = score(k);
        // The tuning error: the phase the halves of the longest tone gain
        // on its template between their middles (unbiased by the noise,
        // unlike the mean of z[n] conj z[n-1], which the filter's
        // correlated noise pulls towards the reference).
        let long = (0..nseg).max_by(|a, b| counts[*a].total_cmp(&counts[*b])).unwrap();
        let idx: Vec<usize> = (0..n).filter(|m| seg_of[*m] == long).collect();
        let h = idx.len() / 2;
        let (mut c1, mut c2) = (Complex64::new(0.0, 0.0), Complex64::new(0.0, 0.0));
        for (j, m) in idx.iter().enumerate() {
            if j < h {
                c1 += zs[k + m] * t[*m];
            } else {
                c2 += zs[k + m] * t[*m];
            }
        }
        let err_hz = (c2 * c1.conj()).arg() * wr / (2.0 * PI * (idx.len() as f64 / 2.0).max(1.0));
        // Whether it is a sync at all: the longest tone's share of its own
        // samples' power. Not the placing score: a 7-sample porch of
        // filtered noise, alike over its few samples, scores there as if
        // it were tone, and syncs were "found" in noise.
        let pl: f64 = idx.iter().map(|m| zs[k + m].norm_sqr()).sum();
        let q = if pl > 1e-30 { (c1 + c2).norm_sqr() / (idx.len() as f64 * pl) } else { 0.0 };
        Some(Hit {
            pos: expect + (k as i64 - wi) as f64 + frac,
            q,
            sig,
            noise: (p / n as f64 - sig).max(0.0),
            err_hz,
        })
    }

    /// A steady tone at `hz` over [a, b): (its frequency error, Hz; its
    /// power; the noise power beside it), by the same halves as `matched`.
    fn tone_fit(&self, a: f64, b: f64, hz: f64) -> (f64, f64, f64) {
        let (j0, j1) = (a.ceil() as i64, b.floor() as i64);
        let n = (j1 - j0).max(2) as usize;
        let om = -2.0 * PI * (hz - REF_HZ) / self.wr;
        let rot = Complex64::new(om.cos(), om.sin());
        let mut ph = Complex64::new(1.0, 0.0);
        let (mut c1, mut c2, mut p) = (Complex64::new(0.0, 0.0), Complex64::new(0.0, 0.0), 0.0);
        for m in 0..n {
            let z = self.zat(j0 + m as i64);
            if m < n / 2 {
                c1 += z * ph;
            } else {
                c2 += z * ph;
            }
            p += z.norm_sqr();
            ph *= rot;
        }
        let err = (c2 * c1.conj()).arg() * self.wr / (2.0 * PI * (n as f64 / 2.0));
        let sig = (c1 + c2).norm_sqr() / (n * n) as f64;
        (err, sig, (p / n as f64 - sig).max(0.0))
    }

    // -------------------------------------------------------- feeding

    fn push_input(&mut self, input: &Input<'_>) -> Vec<Complex32> {
        let mut mixed: Vec<Complex32> = Vec::with_capacity(input.len());
        let mut step = |x: Complex64| {
            let m = x * self.ph * self.gain as f64;
            self.ph *= self.rot;
            self.mixed += 1;
            if self.mixed % 1024 == 0 {
                self.ph /= self.ph.norm();
            }
            mixed.push(Complex32::new(m.re as f32, m.im as f32));
        };
        match input {
            Input::Audio { samples, .. } => samples.iter().for_each(|&a| step(Complex64::new(a as f64, 0.0))),
            Input::Iq { samples, .. } => samples.iter().for_each(|z| step(Complex64::new(z.re as f64, z.im as f64))),
        }
        mixed
    }

    /// Mixed input to working and slow samples, and the detectors.
    fn process(&mut self, mixed: &[Complex32], found: &mut Vec<VisFound>) {
        let mut w = Vec::with_capacity(mixed.len() / self.d + 1);
        self.lp.as_mut().unwrap().process(mixed, &mut w);
        let drop = self.lp_drop.min(w.len());
        self.lp_drop -= drop;
        let w = &w[drop..];
        self.z.extend_from_slice(w);
        let mut s = Vec::with_capacity(w.len() / self.d2 + 1);
        self.slow_lp.as_mut().unwrap().process(w, &mut s);
        let drop = self.slow_drop.min(s.len());
        self.slow_drop -= drop;
        let s = &s[drop..];
        self.vis.as_mut().unwrap().push(s, found);
        self.train.as_mut().unwrap().push(s);
    }

    fn allowed(&self) -> Vec<&'static Mode> {
        match self.cfg.only {
            Some(m) => vec![m],
            None => modes::MODES.iter().collect(),
        }
    }

    /// Headers heard during a picture, judged once the picture has been
    /// read past them. A picture's own content can pass for a header -
    /// PD's white rows send R-Y and B-Y at 128, 1900 Hz, for two scans
    /// running, and its 20 ms sync passes for the break - so a header
    /// counts only if the picture's syncs stopped under it: none found
    /// from its start to a line past its end. (sstv.py never looked for a
    /// header during a picture, so a new picture sent straight after an
    /// unfinished one was lost.)
    fn settle_pending(&mut self, out: &mut Sink) {
        while let Some(&v) = self.pending.first() {
            let s = v.start * self.d2 as f64;
            let e = s + self.ms(modes::VIS_MS);
            let verdict = match self.pic.as_ref() {
                // The picture it was heard in has ended since.
                None => Some(!self.prev_syncs.iter().any(|x| *x >= s && *x <= e)),
                Some(p) => {
                    let off = self.ms(p.lay.sync_off_ms);
                    let synced = p.obs.iter().any(|o| o.pos + off >= s && o.pos + off <= e + p.p_nom);
                    let read_past = p.used.last().is_some_and(|u| *u > e + p.p_nom);
                    if synced {
                        Some(false)
                    } else if read_past || (self.end() as f64) > e + 4.0 * p.p_nom {
                        Some(true)
                    } else {
                        None
                    }
                }
            };
            match verdict {
                None => break,
                Some(real) => {
                    self.pending.remove(0);
                    if real {
                        self.on_vis(v, out);
                        self.lines(out);
                    }
                }
            }
        }
    }

    /// A VIS header heard: start its picture (ending one in progress).
    fn on_vis(&mut self, v: VisFound, out: &mut Sink) {
        self.vis_heard += 1;
        let start_w = v.start * self.d2 as f64;
        let mode = if v.parity_ok { modes::by_vis(v.code) } else { None };
        let mode = mode.filter(|m| self.cfg.only.is_none_or(|o| o.id == m.id));
        let Some(mode) = mode else {
            self.vis_bad += 1;
            let mut f: Fields = vec![("state".into(), Value::Text(if v.parity_ok { "unsupported" } else { "bad_vis" }.into()))];
            f.push(("vis".into(), Value::Num(v.code as f64)));
            f.push((
                "mode".into(),
                match modes::UNSUPPORTED.iter().find(|u| u.0 == v.code) {
                    Some(u) if v.parity_ok => Value::Text(u.1.into()),
                    _ => match modes::by_vis(v.code) {
                        Some(m) if v.parity_ok => Value::Text(m.id.into()),
                        _ => Value::Null,
                    },
                },
            ));
            out.push(self.t_of(start_w), Output::Meta { fields: f });
            return;
        };
        if self.pic.is_some() {
            self.finish(Some(start_w), out);
        }
        // The offset to a fraction of a hertz from the leaders (less 20 ms
        // at each end), starting from the grid's; and the noise beside them.
        let l = self.ms(modes::LEADER_MS);
        let (e, b) = (self.ms(20.0), self.ms(modes::BREAK_MS));
        let hz = modes::LEADER_HZ + v.delta;
        let (e1, s1, n1) = self.tone_fit(start_w + e, start_w + l - e, hz);
        let (e2, s2, n2) = self.tone_fit(start_w + l + b + e, start_w + 2.0 * l + b - e, hz);
        let err = (e1 + e2) / 2.0;
        let delta = v.delta + if err.abs() <= VIS_STEP_HZ { err } else { 0.0 };
        // The detector placed the header to a slow sample (four working
        // ones). Its bits now known, the header from the second leader to
        // the stop bit is a known waveform: matched as a line's sync is,
        // it is placed to a fraction of a working sample, and with it the
        // first line of the modes whose first sync cannot be measured.
        let mut segs = vec![(modes::LEADER_MS, modes::LEADER_HZ)];
        segs.extend(modes::vis_segments(v.code).into_iter().skip(3));
        let tpl = Template { segs, pre_ms: 0.0 };
        let lead2 = start_w + l + b;
        let start_w = match self.matched(lead2, 2.0 * self.d2 as f64 + 1.0, &tpl, 1.0, delta) {
            Some(h) if h.q >= SYNC_MIN => h.pos - l - b,
            _ => start_w,
        };
        let line0 = start_w + self.ms(modes::VIS_MS) + self.ms(mode.head_ms);
        self.begin(mode, Some(v.code), line0, delta, (s1 + s2) / 2.0, (n1 + n2) / 2.0, self.t_of(start_w), out);
    }

    #[allow(clippy::too_many_arguments)]
    fn begin(
        &mut self,
        mode: &'static Mode,
        vis: Option<u8>,
        line0: f64,
        delta: f64,
        sig0: f64,
        noise0: f64,
        t: f64,
        out: &mut Sink,
    ) {
        let snr0 = self.snr_db(sig0, noise0).unwrap_or(20.0);
        let lay = mode.layout();
        let p_nom = self.ms(lay.line_ms);
        let px_ms = lay.scans[0].2 / mode.width as f64;
        self.images += 1;
        self.meta_due = 0;
        if vis.is_none() {
            self.partials += 1;
        }
        out.push(t, Output::Image { event: ImageEvent::Start { width: mode.width, height: mode.height, format: "rgb" } });
        self.pic = Some(Picture {
            mode,
            lay,
            tpl: template(mode),
            snr0,
            widths: Vec::new(),
            px_ms,
            noises: Vec::new(),
            span: SPAN_DEFAULT,
            noise0,
            vis,
            p_nom,
            origin: line0,
            k: 0,
            obs: Vec::new(),
            used: Vec::new(),
            asm: Assembler::new(mode),
            rgb: vec![0; (mode.width * mode.height * 3) as usize],
            rows: 0,
            miss_run: 0,
            reject_run: 0,
            found: 0,
            missed: 0,
            parity_vote: 0,
            delta,
            sig: 0.0,
            noise: 0.0,
            t_start: t,
            out_rows: Vec::new(),
        });
        let meta = self.meta("receiving", None);
        out.push(t, Output::Meta { fields: meta });
    }

    fn try_train(&mut self, out: &mut Sink) {
        let modes = self.allowed();
        let Some((mode, pulse_slow, d)) = self.train.as_mut().unwrap().look(&modes) else { return };
        let lay = mode.layout();
        let p = self.ms(lay.line_ms);
        let mut start = pulse_slow * self.d2 as f64 - self.ms(lay.sync_off_ms);
        // The offset from the pulses themselves (the one found is the
        // train's first), and the noise beside them.
        let k = train_lines(lay.line_ms / 1000.0 * self.sr(), self.sr());
        let (len, e) = (self.ms(lay.sync_ms), self.ms(1.0).min(self.ms(lay.sync_ms) / 4.0));
        let (mut err, mut sig, mut noise) = (0.0, 0.0, 0.0);
        for j in 0..k {
            let at = start + self.ms(lay.sync_off_ms) + j as f64 * p;
            let (de, sg, n) = self.tone_fit(at + e, at + len - e, modes::SYNC_HZ + d);
            err += de / k as f64;
            sig += sg / k as f64;
            noise += n / k as f64;
        }
        let delta = d + if err.abs() < 60.0 { err } else { 0.0 };
        // The first whole line in the buffer, with room for its sync search.
        let w = self.ms(WIDE_MS).max(WIDE_SHARE * p);
        while start + self.ms(lay.sync_off_ms) - w - 2.0 < self.z_base as f64 {
            start += p;
        }
        // A train may miss one pulse, and when that is its first the
        // picture would start a line early, on whatever came before: every
        // row one out (sstv.py does this; Scottie with no header scored
        // 16.7 dB for it). So start at the first line whose sync is there.
        let tpl = template(mode);
        for _ in 0..TRAIN_MISS + 1 {
            match self.matched(start + self.ms(lay.sync_off_ms), self.ms(LOCK_MS), &tpl, 1.0, delta) {
                Some(h) if h.q >= SYNC_MIN => break,
                _ => start += p,
            }
        }
        let t = self.t_of(start);
        self.begin(mode, None, start, delta, sig, noise, t, out);
    }

    /// Read every line whose audio is here.
    fn lines(&mut self, out: &mut Sink) {
        while let Some(done) = self.line() {
            if done {
                self.finish(None, out);
                break;
            }
        }
        self.flush_rows(out);
    }

    fn flush_rows(&mut self, out: &mut Sink) {
        let Some(pic) = self.pic.as_mut() else { return };
        if pic.out_rows.is_empty() {
            return;
        }
        let rows = std::mem::take(&mut pic.out_rows);
        let w3 = pic.mode.width as usize * 3;
        let t0 = pic.t_start;
        // Contiguous runs, one event each.
        let mut i = 0;
        let mut events = Vec::new();
        while i < rows.len() {
            let from = rows[i].0;
            let mut rgb = Vec::with_capacity(w3 * 4);
            let mut j = i;
            while j < rows.len() && rows[j].0 == from + (j - i) as u32 && j - i < RESEND_ROWS {
                rgb.extend_from_slice(&rows[j].1);
                j += 1;
            }
            events.push((from, rgb));
            i = j;
        }
        for (from, rgb) in events {
            out.push(t0, Output::Image { event: ImageEvent::Lines { from, rgb } });
        }
    }

    /// One line, if its audio is here: Some(true) when the picture is
    /// complete or lost, Some(false) to go on, None to wait for more.
    fn line(&mut self) -> Option<bool> {
        let end = self.end() as f64;
        let wr = self.wr;
        let pic = self.pic.as_ref()?;
        let k = pic.k;
        let p_nom = pic.p_nom;
        let causal = fit(&pic.obs, k, true, p_nom, pic.span);
        let pred = match (causal, pic.obs.last()) {
            (Some((a, _)), _) => a,
            (None, Some(o)) => o.pos + (k - o.k) as f64 * p_nom,
            (None, None) => pic.origin + k as f64 * p_nom,
        };
        let scale = causal.map(|(_, b)| b / p_nom).unwrap_or(1.0);
        let lay = pic.lay.clone();
        let ms = |x: f64| x / 1000.0 * wr;
        let w = if causal.is_some() { ms(LOCK_MS).max(LOCK_SHARE * p_nom) } else { ms(WIDE_MS).max(WIDE_SHARE * p_nom) };
        let sync_len = ms(lay.sync_ms) * scale;
        let sync_at = pred + ms(lay.sync_off_ms) * scale;
        // The audio the sync search and the line's last pixel need.
        let need = (sync_at + w + sync_len + 2.0).max(pred + ms(lay.last_pixel_ms) * scale + 2.0);
        if need > end {
            return None;
        }
        let (mode, delta, partial) = (pic.mode, pic.delta, pic.vis.is_none());
        // Line 0's sync runs on from the VIS stop bit in every mode whose
        // sync opens its line: one plateau of 1200 Hz, where the detector
        // finds the stop bit; the header's own fit places that line.
        let abuts = k == 0 && !partial && lay.sync_off_ms == 0.0;
        let found = if abuts { None } else { self.matched(sync_at, w, &pic.tpl, scale, delta) };
        let pic = self.pic.as_mut().unwrap();
        let mut start = pred;
        let mut good = false;
        if let Some(Hit { pos, q, sig, noise, err_hz }) = found {
            let at = pos - ms(lay.sync_off_ms) * scale;
            if q >= SYNC_MIN {
                let near = causal.is_none() || (at - pred).abs() <= ms(GATE_MS);
                if near {
                    pic.reject_run = 0;
                    good = true;
                } else if q >= STRONG {
                    pic.reject_run += 1;
                    if pic.reject_run >= RELOCK {
                        // The sender's timing jumped: start the fit again.
                        pic.obs.clear();
                        pic.reject_run = 0;
                        good = true;
                    }
                } else {
                    pic.reject_run = 0;
                }
                if good {
                    pic.obs.push(Obs { k, pos: at, q });
                    pic.sig += sig;
                    pic.noise += noise;
                    pic.noises.push(noise);
                    start = at;
                    if q >= STRONG && err_hz.abs() < 100.0 {
                        pic.delta += AFC_GAIN * err_hz;
                    }
                }
            }
        }
        let pic = self.pic.as_mut().unwrap();
        if k % META_EVERY == 0 && k > 0 {
            pic.span = choose_span(&pic.obs, p_nom);
        }
        if let Some((a, _)) = fit(&pic.obs, k, true, p_nom, pic.span) {
            start = a;
        }
        if good {
            pic.found += 1;
            pic.miss_run = 0;
        } else if !abuts {
            pic.missed += 1;
            pic.miss_run += 1;
        }
        let scale = fit(&pic.obs, k, true, p_nom, pic.span).map(|(_, b)| b / p_nom).unwrap_or(1.0);
        pic.used.push(start);
        pic.k += 1;
        let width = pix_width(pic.snr_level(self.noise_bw), pic.px_ms);
        pic.widths.push(width);
        let (delta, noise) = (pic.delta, pic.noise_pow());
        let pf = PixFilter::new(width, wr, self.noise_bw);
        let data = self.read_line(mode, &lay, &pf, start, scale, delta, noise);
        let pic = self.pic.as_mut().unwrap();
        if let Some(sr) = data.sep_ry {
            pic.parity_vote += if sr == (k % 2 == 0) { 1 } else { -1 };
        }
        let is_ry = robot_is_ry(pic.vis.is_some(), pic.parity_vote, k);
        let rows = pic.asm.line(k, &data, is_ry);
        let w3 = mode.width as usize * 3;
        for (r, row) in rows {
            if r < mode.height {
                pic.rgb[r as usize * w3..(r as usize + 1) * w3].copy_from_slice(&row);
                pic.rows = pic.rows.max(r + 1);
                pic.out_rows.push((r, row));
            }
        }
        self.lines += 1;
        let pic = self.pic.as_ref().unwrap();
        let lost = if partial {
            let by_time = ((PARTIAL_LOST_S * wr / p_nom).ceil() as u32).max(2);
            pic.miss_run >= PARTIAL_LOST.min(by_time)
        } else {
            pic.miss_run >= LOST_LINES && pic.miss_run as f64 * p_nom >= LOST_S * wr
        };
        Some(pic.k >= mode.lines() || lost)
    }

    /// Line read from `start` at `scale` (the fitted line over the mode's),
    /// with the offset `delta` and the baseband's noise power `noise`.
    #[allow(clippy::too_many_arguments)]
    fn read_line(
        &self,
        mode: &'static Mode,
        lay: &Layout,
        pf: &PixFilter,
        start: f64,
        scale: f64,
        delta: f64,
        noise: f64,
    ) -> LineData {
        let ms = |x: f64| x / 1000.0 * self.wr * scale;
        let v = self.view(start, start + ms(lay.line_ms), pf);
        let bias = noise * pf.noise_gain * pf.rho;
        let w = mode.width as usize;
        let scans = lay.scans.iter().map(|&(c, off, dur)| (c, v.pixels(start + ms(off), ms(dur), w, delta, bias))).collect();
        let sep_ry = lay
            .sep
            .map(|(off, dur)| v.freq(start + ms(off + dur / 4.0), start + ms(off + 3.0 * dur / 4.0), delta, bias) < 1900.0);
        LineData { scans, sep_ry }
    }

    /// End the picture: straighten it from all its syncs, re-send what
    /// moved, save it. `cut`: a new header starts there, so lines from
    /// it on are not this picture's.
    fn finish(&mut self, cut: Option<f64>, out: &mut Sink) {
        self.flush_rows(out);
        let Some(mut pic) = self.pic.take() else { return };
        let mode = pic.mode;
        let partial = pic.vis.is_none();
        let mut lines = pic.k;
        if let Some(c) = cut {
            lines = pic.used.iter().take_while(|s| **s + pic.p_nom <= c).count() as u32;
        }
        if partial {
            // Where a picture joined part way ends is not known: it ends
            // without the lines read since its syncs stopped.
            lines = lines.saturating_sub(pic.miss_run);
        }
        pic.obs.retain(|o| o.k < lines);
        // Every line again, from the fit through the syncs on both sides.
        let mut asm = Assembler::new(mode);
        let mut rgb = vec![0u8; pic.rgb.len()];
        let mut rows = 0u32;
        let w3 = mode.width as usize * 3;
        let mut votes = 0i64;
        let mut datas = Vec::with_capacity(lines as usize);
        let span = choose_span(&pic.obs, pic.p_nom);
        for k in 0..lines {
            let (start, scale) = match fit(&pic.obs, k, false, pic.p_nom, span) {
                Some((a, b)) => (a, b / pic.p_nom),
                None => (pic.used[k as usize], 1.0),
            };
            let pf = PixFilter::new(pic.widths[k as usize], self.wr, self.noise_bw);
            let d = self.read_line(mode, &pic.lay, &pf, start, scale, pic.delta, pic.noise_pow());
            if let Some(sr) = d.sep_ry {
                votes += if sr == (k % 2 == 0) { 1 } else { -1 };
            }
            datas.push(d);
        }
        for (k, d) in datas.iter().enumerate() {
            let is_ry = robot_is_ry(pic.vis.is_some(), votes, k as u32);
            let mut produced = asm.line(k as u32, d, is_ry);
            if k as u32 + 1 == lines {
                produced.extend(asm.flush());
            }
            for (r, row) in produced {
                if r < mode.height {
                    rgb[r as usize * w3..(r as usize + 1) * w3].copy_from_slice(&row);
                    rows = rows.max(r + 1);
                }
            }
        }
        // What moved, re-sent.
        let mut changed = Vec::new();
        for r in 0..rows {
            let (a, b) = (&rgb[r as usize * w3..(r as usize + 1) * w3], &pic.rgb[r as usize * w3..(r as usize + 1) * w3]);
            if r >= pic.rows || a.iter().zip(b).any(|(x, y)| (*x as i32 - *y as i32).abs() > RESEND_DIFF) {
                changed.push((r, a.to_vec()));
            }
        }
        let corrected = !changed.is_empty();
        pic.rgb = rgb;
        // Rows a header picture never got are black; a partial one is as
        // tall as what it got, and one lost within a few lines (a train
        // that was not one, or a fade) leaves no file.
        let height = if !partial {
            mode.height
        } else if rows >= PARTIAL_MIN_ROWS * mode.rows {
            rows
        } else {
            0
        };
        pic.rows = rows;
        let png_rgb = &pic.rgb[..height as usize * w3];
        let t_end = self.t_of(pic.used.get(lines.saturating_sub(1) as usize).copied().unwrap_or(pic.origin) + pic.p_nom);
        let name = format!("sstv_{}_{}.png", stamp(pic.t_start), mode.id);
        let png = if height > 0 { png::encode(png_rgb, mode.width, height) } else { Vec::new() };
        self.syncs_found += pic.found;
        self.syncs_missed += pic.missed;
        let off = self.ms(pic.lay.sync_off_ms);
        self.prev_syncs = pic.obs.iter().map(|o| o.pos + off).collect();
        let state = if lines >= mode.lines() { "done" } else { "lost" };
        let fields = {
            pic.out_rows = changed;
            self.pic = Some(pic);
            self.flush_rows(out);
            let f = self.meta_with(state, if height > 0 { Some(name.clone()) } else { None }, corrected);
            self.pic = None;
            f
        };
        if height > 0 {
            out.push(t_end, Output::Image { event: ImageEvent::Done { file: name, png, path: None } });
        }
        out.push(t_end, Output::Meta { fields });
        // Nothing read can start a train or a header again.
        let s = self.end() / self.d2 as u64;
        if let Some(t) = self.train.as_mut() {
            t.from = t.from.max(s);
        }
    }

    fn meta(&self, state: &str, file: Option<String>) -> Fields {
        self.meta_with(state, file, false)
    }

    fn meta_with(&self, state: &str, file: Option<String>, corrected: bool) -> Fields {
        let Some(pic) = self.pic.as_ref() else { return Vec::new() };
        // The sender's clock: the line through every sync.
        let fit_all = fit(&pic.obs, pic.k / 2, false, pic.p_nom, 1e9);
        let num_or_null = |x: Option<f64>| x.map(Value::Num).unwrap_or(Value::Null);
        vec![
            ("state".into(), Value::Text(state.into())),
            ("mode".into(), Value::Text(pic.mode.id.into())),
            ("vis".into(), num_or_null(pic.vis.map(|v| v as f64))),
            ("partial".into(), Value::Bool(pic.vis.is_none())),
            ("width".into(), Value::Num(pic.mode.width as f64)),
            ("height".into(), Value::Num(pic.mode.height as f64)),
            ("rows".into(), Value::Num(pic.rows as f64)),
            ("started".into(), Value::Num(pic.t_start)),
            // The sender's sound card, ppm fast (its lines that much short).
            ("slantPpm".into(), num_or_null(fit_all.map(|(_, b)| ((pic.p_nom / b - 1.0) * 1e6 * 10.0).round() / 10.0))),
            ("snrDb".into(), num_or_null(self.pic_snr(pic).map(|s| (s * 10.0).round() / 10.0))),
            ("offsetHz".into(), Value::Num((pic.delta * 10.0).round() / 10.0)),
            ("syncs".into(), Value::Num(pic.found as f64)),
            ("missed".into(), Value::Num(pic.missed as f64)),
            ("corrected".into(), Value::Bool(corrected)),
            ("file".into(), file.map(Value::Text).unwrap_or(Value::Null)),
        ]
    }

    /// SNR in 2.5 kHz from the sync pulses: tone power over the noise's
    /// density in the baseband filter's noise bandwidth.
    fn pic_snr(&self, pic: &Picture) -> Option<f64> {
        if pic.found == 0 {
            return None;
        }
        snr_of(pic.sig, pic.noise, self.noise_bw)
    }

    fn snr_db(&self, sig: f64, noise: f64) -> Option<f64> {
        snr_of(sig, noise, self.noise_bw)
    }

    fn trim(&mut self) {
        let keep_from = match &self.pic {
            // The whole picture, for its final reading.
            Some(p) => (p.origin - p.p_nom).max(0.0) as u64,
            // A train's worth, or a header's, whichever is longer.
            None => {
                let t = self.train.as_ref().unwrap();
                let keep = (t.keep * self.d2) as u64 + self.ms(modes::VIS_MS + 500.0) as u64;
                self.end().saturating_sub(keep)
            }
        };
        if keep_from > self.z_base + 8192 {
            let k = (keep_from - self.z_base) as usize;
            self.z.drain(..k.min(self.z.len()));
            self.z_base += k as u64;
        }
    }

    fn reset_signal(&mut self) {
        self.pic = None;
        self.pending.clear();
        self.kind = None;
        self.lp = None;
        self.vis = None;
        self.train = None;
        self.z.clear();
        self.z_base = 0;
        self.next_index = None;
    }
}

/// SNR in 2.5 kHz from a tone's power and the noise's in the baseband
/// filter's noise bandwidth `bw`.
fn snr_of(sig: f64, noise: f64, bw: f64) -> Option<f64> {
    (sig > 0.0 && noise > 0.0).then(|| 10.0 * (sig / (noise / bw * crate::testsig::SNR_BANDWIDTH)).log10())
}

/// Robot 36: is line `k` R-Y? With a header, even lines are; without one,
/// the separators' vote says which parity is.
fn robot_is_ry(header: bool, vote: i64, k: u32) -> bool {
    (k % 2 == 0) == (header || vote >= 0)
}

/// "20261023-101500Z" from UTC seconds (civil from days, H. Hinnant).
fn stamp(t: f64) -> String {
    let secs = t.floor() as i64;
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}Z", sod / 3600, sod / 60 % 60, sod % 60)
}

impl Decoder for Sstv {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        let mut cfg = self.cfg.clone();
        if let Some(m) = params.text("mode") {
            cfg.only = match m {
                "" => None,
                id => Some(modes::by_id(id).ok_or(DecodeError::BadInput("bad_param"))?),
            };
        }
        if let Some(b) = params.flag("syncStart") {
            cfg.sync_start = b;
        }
        if let Some(r) = params.num("afcRangeHz") {
            cfg.afc_range = r;
        }
        let rebuild = cfg.afc_range != self.cfg.afc_range;
        self.cfg = cfg;
        if rebuild {
            if let Some(v) = self.vis.as_ref() {
                let sr = v.sr;
                self.vis = Some(Vis::new(sr, self.cfg.afc_range));
                self.slow_drop = 0;
            }
        }
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let kind = match input {
            Input::Iq { .. } => InputKind::Iq,
            Input::Audio { .. } => InputKind::Audio,
        };
        if self.kind != Some(kind) || self.rate != input.rate() {
            self.finish(None, out);
            self.build(kind, input.rate(), input.index())?;
        }
        // A gap: up to two seconds is bridged with silence so the line
        // clock keeps its place; longer ends the picture and starts afresh.
        let expect = self.next_index.unwrap_or(input.index());
        if input.index() != expect {
            let gap = input.index() as i64 - expect as i64;
            if gap > 0 && (gap as f64) < 2.0 * self.rate {
                let silence = vec![Complex32::new(0.0, 0.0); gap as usize];
                let mut found = Vec::new();
                self.process(&silence, &mut found);
            } else {
                self.finish(None, out);
                self.build(kind, input.rate(), input.index())?;
            }
        }
        self.next_index = Some(input.index() + input.len() as u64);
        self.t_block = (input.t0(), input.index());
        let mixed = self.push_input(&input);
        let mut found = Vec::new();
        self.process(&mixed, &mut found);
        // Lines up to the new audio first: a header found in it may cut
        // the picture they belong to.
        self.lines(out);
        for v in found {
            if self.pic.is_none() {
                self.on_vis(v, out);
                self.lines(out);
            } else {
                self.pending.push(v);
            }
        }
        self.settle_pending(out);
        if self.pic.is_none() && self.cfg.sync_start {
            self.try_train(out);
            self.lines(out);
        }
        self.periodic_meta(out);
        if let Some(p) = &self.pic {
            self.last_offset = Some(p.delta as f32);
            self.last_snr = self.pic_snr(p).map(|s| s as f32);
        }
        self.trim();
        Ok(())
    }

    fn reset(&mut self) {
        self.reset_signal();
    }

    fn status(&self) -> Status {
        Status {
            sync: self.pic.is_some(),
            snr_db: self.last_snr,
            offset_hz: self.last_offset,
            baud: None,
            counters: vec![
                ("images", self.images),
                ("partials", self.partials),
                ("lines", self.lines),
                ("syncsFound", self.syncs_found),
                ("syncsMissed", self.syncs_missed),
                ("visHeard", self.vis_heard),
                ("visBad", self.vis_bad),
            ],
        }
    }
}

impl Sstv {
    /// `meta` while receiving, every META_EVERY lines.
    fn periodic_meta(&mut self, out: &mut Sink) {
        let Some(p) = self.pic.as_ref() else { return };
        let due = p.k / META_EVERY;
        if due > 0 && p.used.len() as u32 == p.k && self.meta_due != due {
            self.meta_due = due;
            let t = self.t_of(*p.used.last().unwrap());
            let f = self.meta("receiving", None);
            out.push(t, Output::Meta { fields: f });
        }
    }
}
