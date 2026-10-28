//! ADS-B and Mode S on 1090 MHz: aircraft from their extended squitters
//! (DF17/18), with what their other replies (DF4/5/11/20/21) add, checked
//! so that only aircraft that are really there are reported (track.rs).
//!
//! **Input.** Complex IQ at 2 MS/s or more centred on 1090 MHz: Radio
//! gives its receiver's own 2.4 MS/s, as dump1090 reads an RTL-SDR. A
//! Mode S half-chip (0.5 us) is then 1.2 samples, so nothing here assumes
//! a whole number of samples to a chip.
//!
//! **The demodulator** works on the magnitude (the carrier's phase and a
//! transponder's frequency error, up to ±1 MHz, do not matter), after a
//! slow DC blocker that takes out a receiver's LO leakage without touching
//! a 120 us reply:
//!
//! - **Half-chip energies from a running sum.** The magnitude is summed as
//!   it comes, and the energy in any half-chip window, at any fractional
//!   start, is a difference of that sum interpolated at two points. So the
//!   same few operations serve 2, 2.4 or 8 MS/s and any sub-sample timing.
//! - **The preamble** (pulses at 0, 1, 3.5 and 4.5 us, quiet between and
//!   for 3 us after) is looked for at every sample: a cheap test of the
//!   first two pulses against the gaps between them, then all sixteen
//!   half-chips - each pulse above the quiet's mean and the pulses well
//!   above the noise floor (the 25th percentile of the block's power, so
//!   traffic does not raise it).
//! - **Phase correction:** the preamble's start is refined to an eighth of
//!   a sample by its best score, and the message is sliced there and at
//!   0.2 and 0.4 of a sample either side; the first slicing whose parity
//!   holds is taken. At 2.4 MS/s a fixed phase misreads bits wherever a
//!   half-chip boundary falls mid-sample; dump1090's 2.4 MS/s demodulator
//!   tries several phases for the same reason.
//! - **Bits** are which half of each microsecond holds more energy (the
//!   middle of each half: `slice`); how much more is the bit's confidence.
//!
//! **Rate and bandwidth decide more than anything here.** Mode S's bits
//! differ around ±1 MHz from the carrier, so the receiver's IF filter
//! matters: through a sharp 1.536 MHz filter (an SDRplay RSP below 5 MS/s)
//! some bit patterns read wrong at any SNR, and a real capture made so
//! gave nothing to any decoder; 2 MHz decodes, and 6-8 MS/s with a 5 MHz
//! filter more than triples the worst bit's margin. Radio runs an RSP at
//! 6 MS/s or more for ADS-B; an RTL-SDR, with no such filter, at 2.4.
//!
//! **Parity on every message** (crc.rs):
//!
//! - DF17/18 are taken only with a remainder of zero. With `correct` on, a
//!   DF17 one bit wrong is mended, but only a strong one (preamble SNR at
//!   least CORRECT_MIN_DB) and only when the bit the syndrome names is one
//!   of the message's two least certain bits: noise that happens to match
//!   a syndrome rarely also chooses the weakest bit. Never DF18, never the
//!   format field, never two bits.
//! - DF11 is taken with a remainder of zero (a squitter), or a small
//!   interrogator code for an aircraft already confirmed.
//! - DF4/5/20/21 carry their address in their parity: taken only for an
//!   address already confirmed on DF17 (track.rs `confirmed`).
//!
//! **Output.** `Frame { kind: "adsb" }`, one per aircraft at most every
//! `everyS` seconds while it changes, only for aircraft track.rs shows;
//! fields as track.rs `fields` (icao, callsign, position, altitudes, speed,
//! track, vertical rate, squawk, emergency, rssiDb, messages...). Status
//! counts every stage, so a deaf receiver and a picky decoder look
//! different.
//!
//! - `crc`: the parity and the one-bit syndromes.
//! - `msg`: the formats, read and written.
//! - `cpr`: positions.
//! - `track`: aircraft, and the plausibility checks.
//! - `tx`: the test transmitter.

pub mod cpr;
pub mod crc;
pub mod msg;
pub mod track;
pub mod tx;

#[cfg(feature = "python")]
pub(crate) mod py;

use crate::decoder::{
    check_params, DecodeError, Decoder, Family, Info, Input, InputKind, Output, OutputKind, Panel, ParamDefault,
    ParamKind, ParamSpec, Params, Sink, Status,
};
use rustfft::num_complex::Complex32;
use track::{Config, Tracker};

const PARAMS: &[ParamSpec] = &[
    // The station, for the range check and surface positions. Unset (the
    // default), no range check is made and surface positions need an
    // airborne one first.
    ParamSpec { key: "refLat", kind: ParamKind::Range { min: -90.0, max: 90.0 }, default: ParamDefault::Num(0.0) },
    ParamSpec { key: "refLon", kind: ParamKind::Range { min: -180.0, max: 180.0 }, default: ParamDefault::Num(0.0) },
    ParamSpec { key: "maxRangeKm", kind: ParamKind::Range { min: 10.0, max: 1000.0 }, default: ParamDefault::Num(500.0) },
    ParamSpec { key: "correct", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
    ParamSpec { key: "everyS", kind: ParamKind::Range { min: 0.0, max: 60.0 }, default: ParamDefault::Num(1.0) },
];

pub static INFO: Info = Info {
    id: "adsb",
    // Like AIS: not amateur, everyone's to hear, listened to as broadcast is.
    family: Family::Broadcast,
    inputs: &[InputKind::Iq],
    rate_hz: 2_400_000.0,
    bandwidth_hz: 2_000_000.0,
    outputs: &[OutputKind::Frame],
    panel: Panel::Frames { columns: &["icao", "callsign", "altBaro", "gs", "track", "lat", "lon", "squawk"] },
    params: PARAMS,
    needs: &[],
    needs_for: &[],
    min_rate_hz: 2_000_000.0,
};

/// A DF17 is mended only from a preamble at least this far over the floor.
pub const CORRECT_MIN_DB: f32 = 12.0;
/// The preamble's pulses must average this far over the noise floor (the
/// mean noise magnitude). Nothing weaker decodes - a reply at 10 dB SNR
/// has pulses about 11 dB over it - and at 3 dB pure noise passed 7,000
/// times a second at 2.4 MS/s, each tried at five phases: the decoder cost
/// 105 ms of CPU a second against 30 now, and heard nothing more.
const PREAMBLE_MIN_DB: f32 = 6.0;
/// The DC blocker's time constant.
const DC_S: f64 = 0.01;
/// Blocks of this many samples give the noise floor's percentile.
const FLOOR_CHUNK: usize = 256;

/// The DC blocker and the magnitude buffer: samples carried over between
/// blocks so a reply across a block boundary is read whole.
pub struct Adsb {
    rate: f64,
    /// Samples in a half-chip.
    w: f64,
    dc: Complex32,
    dc_k: f32,
    mag: Vec<f32>,
    prefix: Vec<f64>,
    /// The session sample number of mag[0].
    base: u64,
    /// Samples still to skip at the start of the next block (a message
    /// read ran past this one's end).
    skip: usize,
    /// The noise floor, magnitude.
    floor: Option<f32>,
    next_index: Option<u64>,
    /// UTC of session sample 0.
    t_base: f64,
    correct: bool,
    syndromes: [u32; 112],
    tracker: Tracker,
    c: Counts,
    floors: Vec<f32>,
}

#[derive(Clone, Copy, Debug, Default)]
struct Counts {
    preambles: u64,
    messages: u64,
    df17: u64,
    df11: u64,
    surveillance: u64,
    corrected: u64,
    crc_bad: u64,
}

impl Default for Adsb {
    fn default() -> Self {
        Self::new()
    }
}

/// What one slicing gave.
struct Sliced {
    bytes: Vec<u8>,
    /// Each bit's confidence, 0-1.
    conf: Vec<f32>,
    /// The mean energy of the halves that held the pulses, per sample.
    level: f64,
}

impl Adsb {
    pub fn new() -> Self {
        Adsb {
            rate: 0.0,
            w: 0.0,
            dc: Complex32::new(0.0, 0.0),
            dc_k: 0.0,
            mag: Vec::new(),
            prefix: Vec::new(),
            base: 0,
            skip: 0,
            floor: None,
            next_index: None,
            t_base: 0.0,
            correct: true,
            syndromes: crc::syndromes(),
            tracker: Tracker::new(Config::default()),
            c: Counts::default(),
            floors: Vec::new(),
        }
    }

    /// The running sum at fractional sample `x` (0 <= x <= len).
    #[inline]
    fn s(&self, x: f64) -> f64 {
        let k = x as usize;
        let f = x - k as f64;
        let p = &self.prefix;
        if k + 1 >= p.len() {
            return p[p.len() - 1];
        }
        p[k] + f * (p[k + 1] - p[k])
    }

    /// Energy in half-chip `j` of a reply starting at `x`.
    #[inline]
    fn h(&self, x: f64, j: usize) -> f64 {
        let a = x + j as f64 * self.w;
        self.s(a + self.w) - self.s(a)
    }

    /// The preamble's score at `x`: (pulses' mean, quiet's mean), per
    /// half-chip.
    fn preamble(&self, x: f64) -> (f64, f64, f64) {
        let mut hi = 0.0;
        let mut hi_min = f64::MAX;
        for j in [0, 2, 7, 9] {
            let v = self.h(x, j);
            hi += v;
            hi_min = hi_min.min(v);
        }
        let lo: f64 = [1, 3, 4, 5, 6, 8, 11, 12, 13, 14, 15].iter().map(|&j| self.h(x, j)).sum();
        (hi / 4.0, lo / 11.0, hi_min)
    }

    fn slice(&self, x: f64) -> Option<Sliced> {
        let d0 = x + 16.0 * self.w;
        // The middle half of each half-chip, not all of it: a receiver's
        // filter spreads every pulse into its neighbours' edges, and the
        // middles carry the least of that. Measured through sharp IF
        // filters (tests/adsb.rs): the worst bit's margin rises from 0.23
        // to 0.27 at 2.4 MS/s and from 0.66 to 0.89 at 6 MS/s.
        let q = 0.25 * self.w;
        let bit = |k: usize| {
            let c1 = d0 + (2 * k) as f64 * self.w + 0.5 * self.w;
            let c2 = c1 + self.w;
            (self.s(c1 + q) - self.s(c1 - q), self.s(c2 + q) - self.s(c2 - q))
        };
        let mut df = 0u32;
        for k in 0..5 {
            let (a, b) = bit(k);
            df = (df << 1) | (a > b) as u32;
        }
        let n = msg::length(df)?;
        let mut bytes = vec![0u8; n / 8];
        let mut conf = Vec::with_capacity(n);
        let mut level = 0.0;
        for k in 0..n {
            let (a, b) = bit(k);
            if a > b {
                bytes[k / 8] |= 0x80 >> (k % 8);
            }
            conf.push(((a - b).abs() / (a + b).max(1e-30)) as f32);
            level += a.max(b);
        }
        Some(Sliced { bytes, conf, level: level / n as f64 / (2.0 * q) })
    }

    /// Is a slicing a message we believe? (msg, corrected, clean).
    fn judge(&self, s: &mut Sliced, snr_db: f32, t: f64) -> Option<(msg::Msg, bool)> {
        let df = msg::bits(&s.bytes, 0, 5);
        let rem = crc::remainder(&s.bytes);
        match df {
            17 | 18 => {
                if rem == 0 {
                    return msg::parse(&s.bytes).map(|m| (m, false));
                }
                if df != 17 || !self.correct || snr_db < CORRECT_MIN_DB {
                    return None;
                }
                let i = (5..112).find(|&i| self.syndromes[i] == rem)?;
                let mut weakest: Vec<(f32, usize)> = s.conf.iter().copied().zip(0..).collect();
                weakest.sort_by(|a, b| a.0.total_cmp(&b.0));
                if !weakest.iter().take(2).any(|&(_, k)| k == i) {
                    return None;
                }
                s.bytes[i / 8] ^= 0x80 >> (i % 8);
                msg::parse(&s.bytes).map(|m| (m, true))
            }
            11 => {
                let m = msg::parse(&s.bytes)?;
                if rem == 0 || (rem < 0x80 && self.tracker.confirmed(m.icao, t)) {
                    Some((m, false))
                } else {
                    None
                }
            }
            _ => {
                let m = msg::parse(&s.bytes)?;
                self.tracker.confirmed(m.icao, t).then_some((m, false))
            }
        }
    }

    fn build(&mut self, rate: f64) {
        self.rate = rate;
        self.w = 0.5e-6 * rate;
        self.dc_k = (1.0 / (DC_S * rate)) as f32;
        self.mag.clear();
        self.skip = 0;
    }

    /// The noise floor from this block's magnitudes: the 25th percentile
    /// of short chunks' means, so replies, however many, do not lift it.
    fn update_floor(&mut self, from: usize) {
        self.floors.clear();
        for c in self.mag[from..].chunks(FLOOR_CHUNK) {
            if c.len() == FLOOR_CHUNK {
                self.floors.push(c.iter().sum::<f32>() / FLOOR_CHUNK as f32);
            }
        }
        if self.floors.is_empty() {
            return;
        }
        let k = self.floors.len() / 4;
        let p = *self.floors.select_nth_unstable_by(k, |a, b| a.total_cmp(b)).1;
        self.floor = Some(match self.floor {
            None => p,
            Some(f) => f + (p - f) * 0.3,
        });
    }

    fn scan(&mut self, out: &mut Sink) {
        let n = self.mag.len();
        self.prefix.clear();
        self.prefix.reserve(n + 1);
        let mut acc = 0.0f64;
        self.prefix.push(0.0);
        for &m in &self.mag {
            acc += m as f64;
            self.prefix.push(acc);
        }
        let w = self.w;
        // Room for the longest reply, the phase search and the last bit.
        let span = ((16.0 + 224.0) * w).ceil() as usize + 3;
        let floor = self.floor.unwrap_or(0.0) as f64 * w;
        let pre_min = floor * 10f64.powf(PREAMBLE_MIN_DB as f64 / 20.0);
        let mut i = self.skip;
        self.skip = 0;
        while i + span <= n {
            let x = i as f64;
            // The cheap test, first pulse first: most samples are noise
            // and stop at the first comparison.
            let h0 = self.h(x, 0);
            if h0 <= pre_min {
                i += 1;
                continue;
            }
            let (h1, h2, h3) = (self.h(x, 1), self.h(x, 2), self.h(x, 3));
            if !(h0 > h1 && h2 > h1 && h2 > h3) {
                i += 1;
                continue;
            }
            let (hi, lo, hi_min) = self.preamble(x);
            if !(hi > pre_min && hi_min > lo && hi > lo * 1.5) {
                i += 1;
                continue;
            }
            // One preamble passes at several neighbouring samples (at 8
            // MS/s a half-chip is four): take the best of the next
            // half-chip's positions and try it once, rather than slicing
            // each - which cost three times the CPU on Mode A/C fruit.
            let reach = (w.ceil() as usize).max(1);
            let mut peak = (hi - lo, i);
            for j in i + 1..=(i + reach).min(n - span) {
                let (h, l, _) = self.preamble(j as f64);
                if h - l > peak.0 {
                    peak = (h - l, j);
                }
            }
            let next = (i + reach + 1).max(peak.1 + 1);
            let x = peak.1 as f64;
            // Refine to an eighth of a sample.
            let mut best = (f64::MIN, x);
            for k in -4..=4 {
                let xx = x + k as f64 / 8.0;
                if xx < 0.0 {
                    continue;
                }
                let (hi, lo, _) = self.preamble(xx);
                if hi - lo > best.0 {
                    best = (hi - lo, xx);
                }
            }
            self.c.preambles += 1;
            let snr_db = (20.0 * (hi / floor.max(1e-30)).log10()) as f32;
            let t_of = |xx: f64| self.t_base + (self.base as f64 + xx) / self.rate;
            let mut got = None;
            let mut bad = false;
            // Five phases, a fifth of a sample apart: three (±0.25) lost about
            // 3 % more at 2.4 MS/s and 15 dB.
            for d in [0.0, -0.2, 0.2, -0.4, 0.4] {
                let xx = best.1 + d;
                if xx < 0.0 {
                    continue;
                }
                let Some(mut s) = self.slice(xx) else { continue };
                bad = true;
                if let Some((m, corrected)) = self.judge(&mut s, snr_db, t_of(xx)) {
                    got = Some((m, corrected, s, xx));
                    break;
                }
            }
            match got {
                Some((m, corrected, s, xx)) => {
                    let t = t_of(xx);
                    self.count(&m, corrected);
                    let clean = matches!(m.df, 17 | 18) && !corrected;
                    self.tracker.heard(&m, t, s.level * s.level, clean);
                    // On past the whole reply: nothing inside it is another.
                    i = (xx + (16 + 2 * s.bytes.len() * 8) as f64 * w) as usize;
                }
                None => {
                    if bad {
                        self.c.crc_bad += 1;
                    }
                    i = next;
                }
            }
        }
        let keep = i.min(n);
        self.skip = i - keep;
        self.mag.drain(..keep);
        self.base += keep as u64;
        let now = self.t_base + (self.base as f64 + self.mag.len() as f64) / self.rate;
        for (t, fields) in self.tracker.due(now) {
            out.push(t, Output::Frame { kind: "adsb", fields, raw: None });
        }
    }

    fn count(&mut self, m: &msg::Msg, corrected: bool) {
        self.c.messages += 1;
        if corrected {
            self.c.corrected += 1;
        }
        match m.df {
            17 | 18 => self.c.df17 += 1,
            11 => self.c.df11 += 1,
            _ => self.c.surveillance += 1,
        }
    }
}

impl Decoder for Adsb {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        if let (Some(la), Some(lo)) = (params.num("refLat"), params.num("refLon")) {
            self.tracker.cfg.station = Some((la, lo));
        }
        if let Some(r) = params.num("maxRangeKm") {
            self.tracker.cfg.max_range_km = r;
        }
        if let Some(c) = params.flag("correct") {
            self.correct = c;
        }
        if let Some(e) = params.num("everyS") {
            self.tracker.cfg.every_s = e;
        }
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let Input::Iq { samples, rate, t0, index } = input else {
            return Err(DecodeError::BadInput("rate"));
        };
        if rate < INFO.min_rate_hz {
            return Err(DecodeError::BadInput("rate"));
        }
        if (rate - self.rate).abs() > 1e-6 {
            self.build(rate);
            self.next_index = None;
        }
        if self.next_index != Some(index) {
            // A gap or the start: nothing carried over belongs to this.
            self.mag.clear();
            self.skip = 0;
            self.base = index;
            self.t_base = t0 - index as f64 / rate;
        }
        self.next_index = Some(index + samples.len() as u64);
        let from = self.mag.len();
        let k = self.dc_k;
        for &z in samples {
            self.dc += (z - self.dc) * k;
            self.mag.push((z - self.dc).norm());
        }
        self.update_floor(from);
        self.scan(out);
        Ok(())
    }

    fn reset(&mut self) {
        self.mag.clear();
        self.skip = 0;
        self.next_index = None;
        self.dc = Complex32::new(0.0, 0.0);
        self.tracker.clear();
    }

    fn status(&self) -> Status {
        let c = self.c;
        let r = self.tracker.rejected;
        let shown = self.tracker.shown();
        Status {
            sync: shown > 0,
            snr_db: None,
            offset_hz: None,
            baud: None,
            counters: vec![
                ("aircraft", shown as u64),
                ("messages", c.messages),
                ("df17", c.df17),
                ("df11", c.df11),
                ("surveillance", c.surveillance),
                ("corrected", c.corrected),
                ("preambles", c.preambles),
                ("crcBad", c.crc_bad),
                ("rejectedRange", r.range),
                ("rejectedJump", r.jump),
                ("rejectedSpeed", r.speed),
                ("rejectedAltitude", r.altitude),
                ("rejectedFirstFix", r.first_fix),
            ],
        }
    }
}
