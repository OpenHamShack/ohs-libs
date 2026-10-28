//! AIS, the ships' Automatic Identification System (ITU-R M.1371-5): both
//! channels, 87B (161.975 MHz, "A") and 88B (162.025 MHz, "B"), from one
//! IQ input centred between them, each 9600 baud GMSK carrying HDLC
//! frames. Every good frame becomes a `frame` result with the message's
//! fields (msg.rs) and its NMEA `!AIVDM` sentences.
//!
//! **Input.** Complex IQ at 96 kS/s or more with 162.000 MHz at
//! `offsetHz` from its centre (0: centred, as Radio's AIS session tunes).
//! A fast input - a receiver's own 2.4 MS/s - is brought down by integer
//! decimators to between 192 and 384 kS/s before anything else, so the
//! session costs little more at 2.4 MS/s than at 240 kS/s.
//!
//! **Each channel**: mixed to 0 Hz, resampled to 48 kS/s (five samples a
//! bit), filtered to ±7.2 kHz, and read by one-bit differential detection
//! (GMSK is FM with a smoothed bit, ±2400 Hz: the phase turns a quarter
//! turn over a bit of a long run). Then:
//!
//! - **Five slicers, one per sample phase, instead of a clock loop.** At
//!   five samples a bit one of them is within a tenth of a bit of the
//!   bit's centre, and a burst is so short (26-130 ms) that neither a
//!   transmitter's clock (±50 ppm in the standard) nor a receiver's drifts
//!   a tenth of a bit across it. Each slicer runs its own NRZI, HDLC
//!   deframer and CRC; a frame two of them both read is passed on once.
//!   No loop means nothing to acquire in the 24 training bits, and two
//!   bursts on a channel in quick succession each get every slicer afresh.
//! - **The frequency error is measured on the training sequence.** A
//!   transmitter or receiver off frequency moves the discriminator's
//!   output; the slicer's threshold follows a 16-bit running mean until a
//!   flag opens a frame, and is then fixed at the mean of the 24 training
//!   bits before it (0101... is balanced after NRZI, so its mean is the
//!   error exactly) for the rest of the frame, where the data's own
//!   imbalance would otherwise drag it.
//! - **Noise prints nothing**: a frame needs a training sequence before
//!   its flag, whole octets, a good CRC and a type and length that AIS
//!   sends (hdlc.rs, msg.rs `length_ok`).
//!
//! **Output.** `Frame { kind: "ais" }` with msg::fields' fields and
//! `channel` ("A"/"B"), `nmea` (the sentences), `snrDb` (the burst's
//! signal to noise in 9.6 kHz, Eb/N0, from the spread of its envelope),
//! `levelDb` (its power, dB full scale) and `offsetHz` (its
//! frequency error), `plausible` (msg::plausible: the MMSI's form, the
//! position, speed, course and heading in range) and, when false,
//! `implausible` (which failed); `raw` is the payload's octets, MSB first. Status
//! counts frames per channel and damaged frames (a failed CRC, a frame
//! not whole octets, or an abort: one a slicer, so up to five a burst).
//!
//! - `hdlc`: CRC, stuffing, NRZI and the deframer.
//! - `msg`: the message layouts, their fields and NMEA.
//! - `tx`: the test transmitter.

pub mod hdlc;
pub mod msg;
pub mod tx;

#[cfg(feature = "python")]
pub(crate) mod py;

use crate::decoder::{
    check_params, DecodeError, Decoder, Family, Info, Input, InputKind, Output, OutputKind, Panel, ParamDefault,
    ParamKind, ParamSpec, Params, Sink, Status, Value,
};
use crate::dsp::{lowpass, Decimator, Resampler};
use hdlc::{Deframer, Framed};
use rustfft::num_complex::Complex32;
use std::collections::VecDeque;
use tx::Channel;

const PARAMS: &[ParamSpec] = &[
    // Where 162.000 MHz is, Hz from the IQ's centre.
    ParamSpec { key: "offsetHz", kind: ParamKind::Range { min: -5e6, max: 5e6 }, default: ParamDefault::Num(0.0) },
];

pub static INFO: Info = Info {
    id: "ais",
    // AIS is maritime, not broadcast; but its station is everyone's to hear
    // and it is listened to as broadcast is, and a family of its own would
    // change every client's catalogue for one decoder.
    family: Family::Broadcast,
    inputs: &[InputKind::Iq],
    rate_hz: 96_000.0,
    // 87B and 88B, 50 kHz apart, each ±12.5 kHz.
    bandwidth_hz: 75_000.0,
    outputs: &[OutputKind::Frame],
    panel: Panel::Frames { columns: &["mmsi", "type", "shipName", "lat", "lon", "speed", "course", "channel"] },
    params: PARAMS,
    needs: &[],
    needs_for: &[],
    min_rate_hz: 96_000.0,
};

/// The rate each channel is demodulated at: five samples a bit.
pub const CHANNEL_RATE: f64 = 48_000.0;
const SPS: usize = 5;
/// The channel filter's cut-off: GMSK at 9600 Bd and BT 0.4 has 99 % of
/// its power within about ±6.5 kHz.
const CHANNEL_CUTOFF_HZ: f64 = 7_200.0;
/// The first decimators bring the input down to at least this.
const FRONT_MIN_RATE: f64 = 192_000.0;
/// What the front decimators keep flat: both channels and a frequency error.
const FRONT_PASS_HZ: f64 = 40_000.0;
/// Frames read by two slicers within this many samples are one frame.
const SAME_FRAME_SAMPLES: u64 = 3 * SPS as u64;
/// The running threshold's time constant, bits.
const DC_BITS: f32 = 16.0;

/// One sample phase's slicer on one channel.
#[derive(Clone, Debug)]
struct Slicer {
    deframer: Deframer,
    dc_run: f32,
    dc_frozen: Option<f32>,
    /// The last 32 discriminator values at this phase (one a bit).
    hist: VecDeque<f32>,
}

impl Slicer {
    fn new() -> Self {
        Slicer { deframer: Deframer::new(), dc_run: 0.0, dc_frozen: None, hist: VecDeque::with_capacity(33) }
    }

    /// One bit's discriminator value: what frame (if any) it closed, and
    /// the threshold it was sliced at (the frame's frequency error).
    fn push(&mut self, v: f32) -> (Option<Framed>, f32) {
        let v = v.clamp(-2.0, 2.0);
        let thr = self.dc_frozen.unwrap_or(self.dc_run);
        let (framed, flag) = self.deframer.push((v > thr) as u8);
        self.hist.push_back(v);
        if self.hist.len() > 32 {
            self.hist.pop_front();
        }
        self.dc_run += (v - self.dc_run) / DC_BITS;
        if flag {
            self.dc_frozen = if self.deframer.in_frame() && self.hist.len() == 32 {
                // The 24 bits before the flag's 8: the training.
                Some(self.hist.iter().take(24).sum::<f32>() / 24.0)
            } else {
                None
            };
        } else if !self.deframer.in_frame() {
            self.dc_frozen = None;
        }
        (framed, thr)
    }
}

/// A mixer: multiplies by e^(-j 2 pi f n / rate), renormalised now and
/// then so rounding never grows it.
#[derive(Clone, Debug)]
struct Nco {
    z: Complex32,
    step: Complex32,
    n: u32,
}

impl Nco {
    fn new(f: f64, rate: f64) -> Self {
        let w = -2.0 * std::f64::consts::PI * f / rate;
        Nco { z: Complex32::new(1.0, 0.0), step: Complex32::new(w.cos() as f32, w.sin() as f32), n: 0 }
    }

    fn mix(&mut self, x: &[Complex32], out: &mut Vec<Complex32>) {
        out.clear();
        for &v in x {
            out.push(v * self.z);
            self.z *= self.step;
            self.n += 1;
            if self.n >= 1024 {
                self.z /= self.z.norm();
                self.n = 0;
            }
        }
    }
}

/// One AIS channel's demodulator.
struct ChannelRx {
    ch: Channel,
    /// The mixer to 0 Hz.
    nco: Nco,
    resampler: Resampler<Complex32>,
    filter: Decimator,
    /// The last bit's samples, one a phase.
    prev: [Complex32; SPS],
    slicers: Vec<Slicer>,
    /// Samples at 48 kS/s since the start.
    n: u64,
    /// Power of each recent sample, for the burst's level.
    power: VecDeque<f32>,
    /// The noise's bandwidth over 9.6 kHz, as a ratio (the channel
    /// filter's noise bandwidth), to quote SNR as Eb/N0.
    noise_bw: f32,
    /// Frames passed on lately: (end sample, payload), for the dedup.
    recent: VecDeque<(u64, Vec<u8>)>,
    frames: u64,
    damaged: u64,
    scratch_mix: Vec<Complex32>,
    scratch_rs: Vec<Complex32>,
    scratch_f: Vec<Complex32>,
}

/// A good frame, before it becomes a result.
struct Heard {
    ch: Channel,
    /// End sample at 48 kS/s.
    end: u64,
    octets: Vec<u8>,
    snr_db: f32,
    level_db: f32,
    offset_hz: f32,
}

impl ChannelRx {
    fn new(ch: Channel, front_rate: f64) -> Self {
        let taps = lowpass(41, CHANNEL_CUTOFF_HZ, CHANNEL_RATE);
        let (s2, s1): (f64, f64) = taps.iter().fold((0.0, 0.0), |(a, b), &t| (a + (t * t) as f64, b + t as f64));
        let noise_bw = (s2 / (s1 * s1) * CHANNEL_RATE / tx::SNR_BANDWIDTH) as f32;
        ChannelRx {
            ch,
            nco: Nco::new(ch.hz(), front_rate),
            resampler: Resampler::new(front_rate, CHANNEL_RATE, 0.3, 16),
            filter: Decimator::new(taps, 1),
            prev: [Complex32::new(0.0, 0.0); SPS],
            slicers: (0..SPS).map(|_| Slicer::new()).collect(),
            n: 0,
            power: VecDeque::with_capacity(8192),
            noise_bw,
            recent: VecDeque::new(),
            frames: 0,
            damaged: 0,
            scratch_mix: Vec::new(),
            scratch_rs: Vec::new(),
            scratch_f: Vec::new(),
        }
    }

    fn reset(&mut self) {
        self.resampler.reset();
        self.filter.reset();
        self.slicers.iter_mut().for_each(|s| *s = Slicer::new());
        self.power.clear();
        self.recent.clear();
        self.prev = [Complex32::new(0.0, 0.0); SPS];
    }

    fn process(&mut self, x: &[Complex32], heard: &mut Vec<Heard>) {
        self.nco.mix(x, &mut self.scratch_mix);
        self.scratch_rs.clear();
        self.resampler.process(&self.scratch_mix, &mut self.scratch_rs);
        self.scratch_f.clear();
        self.filter.process(&self.scratch_rs, &mut self.scratch_f);
        let k = std::f32::consts::FRAC_2_PI;
        for i in 0..self.scratch_f.len() {
            let z = self.scratch_f[i];
            self.power.push_back(z.norm_sqr());
            if self.power.len() > 8192 {
                self.power.pop_front();
            }
            // One-bit differential detection: the phase turned over the
            // last bit, a quarter turn for a long run of either symbol.
            // It is the discriminator integrated over the bit, but taken
            // as one angle, so a noise click inside the bit counts once
            // rather than spinning the sum: 2-3 dB better than slicing
            // the discriminator's samples on the sweep (tests/ais.rs).
            let phase = (self.n % SPS as u64) as usize;
            let d = (z * self.prev[phase].conj()).arg() * k;
            self.prev[phase] = z;
            self.n += 1;
            match self.slicers[phase].push(d) {
                (Some(Framed::Good(octets)), thr) => self.good(octets, thr, heard),
                (Some(Framed::Damaged), _) => self.damaged += 1,
                (None, _) => {}
            }
        }
    }

    fn good(&mut self, octets: Vec<u8>, dc: f32, heard: &mut Vec<Heard>) {
        let end = self.n;
        self.recent.retain(|(t, _)| end - t < 200);
        if self.recent.iter().any(|(t, o)| end - t <= SAME_FRAME_SAMPLES && *o == octets) {
            return;
        }
        self.recent.push_back((end, octets.clone()));
        self.frames += 1;
        // The burst's power: the frame's own samples, back from its end
        // (its octets and FCS with a little stuffing, and the flags).
        let len = ((octets.len() * 8 + 16) * 33 / 32 + 16) * SPS;
        let len = len.min(self.power.len());
        // M2M4: GMSK's envelope is constant, so the spread of the
        // frame's power is the noise's alone. With m2 = E|x|^2 = S + N and
        // m4 = E|x|^4 = S^2 + 4SN + 2N^2, S = sqrt(2 m2^2 - m4). It needs no
        // noise floor between bursts, which a busy channel may not leave.
        let (m2, m4) = self.power.iter().rev().take(len.max(1)).fold((0f64, 0f64), |(a, b), &p| (a + p as f64, b + (p as f64).powi(2)));
        let (m2, m4) = (m2 / len.max(1) as f64, m4 / len.max(1) as f64);
        let sig = (2.0 * m2 * m2 - m4).max(0.0).sqrt();
        let noise = (m2 - sig).max(m2 * 1e-4);
        let snr = (10.0 * (sig.max(1e-20) / noise * self.noise_bw as f64).log10()) as f32;
        let p = m2 as f32;
        heard.push(Heard {
            ch: self.ch,
            end,
            octets,
            snr_db: snr,
            level_db: 10.0 * p.max(1e-20).log10(),
            offset_hz: dc * 2400.0,
        });
    }
}

/// The AIS decoder: one session hears both channels.
pub struct Ais {
    offset_hz: f64,
    /// 162.000 MHz to 0 Hz, before the decimators (None when centred).
    shift: Option<Nco>,
    rate: f64,
    front: Vec<Decimator>,
    front_rate: f64,
    channels: Vec<ChannelRx>,
    next_index: Option<u64>,
    /// UTC of the 48 kS/s channels' sample 0.
    t_base: f64,
    seq: u8,
    implausible: u64,
    scratch: [Vec<Complex32>; 2],
}

impl Default for Ais {
    fn default() -> Self {
        Self::new()
    }
}

/// The integer decimators that bring `rate` down to between
/// FRONT_MIN_RATE and twice it (none when it is already there), and the
/// rate they leave.
fn front_stages(rate: f64) -> (Vec<Decimator>, f64) {
    let mut stages = Vec::new();
    let mut r = rate;
    while r / FRONT_MIN_RATE >= 2.0 {
        let d = ((r / FRONT_MIN_RATE).floor() as usize).min(8);
        let out = r / d as f64;
        // Blackman: the transition is about 5.5 x rate / taps wide; it must
        // end before what folds onto the passband (out - FRONT_PASS_HZ).
        let width = out - 2.0 * FRONT_PASS_HZ;
        let taps = ((5.5 * r / width).ceil() as usize) | 1;
        stages.push(Decimator::new(lowpass(taps, (FRONT_PASS_HZ + width / 2.0).min(out / 2.0), r), d));
        r = out;
    }
    (stages, r)
}

impl Ais {
    pub fn new() -> Self {
        Ais {
            offset_hz: 0.0,
            shift: None,
            rate: 0.0,
            front: Vec::new(),
            front_rate: 0.0,
            channels: Vec::new(),
            next_index: None,
            t_base: 0.0,
            seq: 0,
            implausible: 0,
            scratch: [Vec::new(), Vec::new()],
        }
    }

    fn build(&mut self, rate: f64) {
        let (front, fr) = front_stages(rate);
        self.front = front;
        self.front_rate = fr;
        self.rate = rate;
        self.shift = (self.offset_hz != 0.0).then(|| Nco::new(self.offset_hz, rate));
        self.channels = [Channel::A, Channel::B].into_iter().map(|c| ChannelRx::new(c, fr)).collect();
    }

    fn result(&mut self, h: Heard) -> Option<(f64, Output)> {
        let bits = hdlc::unpack(&h.octets);
        let m = msg::parse(&bits)?;
        let mut fields = msg::fields(&m);
        let sentences = msg::nmea(&bits, h.ch.code(), self.seq);
        if sentences.len() > 1 {
            self.seq = (self.seq + 1) % 10;
        }
        fields.push(("channel".into(), Value::Text(h.ch.code().to_string())));
        fields.push(("nmea".into(), Value::List(sentences.into_iter().map(Value::Text).collect())));
        fields.push(("snrDb".into(), Value::Num((h.snr_db as f64 * 10.0).round() / 10.0)));
        fields.push(("levelDb".into(), Value::Num((h.level_db as f64 * 10.0).round() / 10.0)));
        fields.push(("offsetHz".into(), Value::Num(h.offset_hz.round() as f64)));
        // Passed on all the same: a log of the air should hold what was on
        // it. A map takes only plausible ones (msg::plausible).
        match msg::plausible(&m) {
            Ok(()) => fields.push(("plausible".into(), Value::Bool(true))),
            Err(why) => {
                self.implausible += 1;
                fields.push(("plausible".into(), Value::Bool(false)));
                fields.push(("implausible".into(), Value::Text(why.into())));
            }
        }
        let t = self.t_base + h.end as f64 / CHANNEL_RATE;
        let raw: Vec<u8> = bits.chunks(8).map(|c| c.iter().fold(0u8, |v, &b| (v << 1) | b)).collect();
        Some((t, Output::Frame { kind: "ais", fields, raw: Some(raw) }))
    }
}

impl Decoder for Ais {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        if let Some(o) = params.num("offsetHz") {
            if o != self.offset_hz {
                self.offset_hz = o;
                // The mixers move: made again at the next block.
                self.rate = 0.0;
            }
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
            // A gap or the start: the clock is set again from this block.
            // A frame in flight across a gap fails its CRC, so nothing
            // else need be undone.
            let n = self.channels.first().map_or(0, |c| c.n);
            self.t_base = t0 - n as f64 / CHANNEL_RATE;
        }
        self.next_index = Some(index + samples.len() as u64);
        let mut cur = std::mem::take(&mut self.scratch[0]);
        let mut nxt = std::mem::take(&mut self.scratch[1]);
        match self.shift.as_mut() {
            Some(nco) => nco.mix(samples, &mut cur),
            None => {
                cur.clear();
                cur.extend_from_slice(samples);
            }
        }
        for d in self.front.iter_mut() {
            nxt.clear();
            d.process(&cur, &mut nxt);
            std::mem::swap(&mut cur, &mut nxt);
        }
        let mut heard = Vec::new();
        for c in self.channels.iter_mut() {
            c.process(&cur, &mut heard);
        }
        self.scratch = [cur, nxt];
        heard.sort_by_key(|h| h.end);
        for h in heard {
            if let Some((t, o)) = self.result(h) {
                out.push(t, o);
            }
        }
        Ok(())
    }

    fn reset(&mut self) {
        self.front.iter_mut().for_each(|d| d.reset());
        self.channels.iter_mut().for_each(|c| c.reset());
        self.next_index = None;
    }

    fn status(&self) -> Status {
        let get = |i: usize| self.channels.get(i);
        let frames = |i: usize| get(i).map_or(0, |c| c.frames);
        let bad = self.channels.iter().map(|c| c.damaged).sum();
        Status {
            sync: false,
            snr_db: None,
            offset_hz: None,
            baud: None,
            counters: vec![("frames", frames(0) + frames(1)), ("channelA", frames(0)), ("channelB", frames(1)), ("damaged", bad), ("implausible", self.implausible)],
        }
    }
}
