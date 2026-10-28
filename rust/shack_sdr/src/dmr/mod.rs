//! DMR Tier II (ETSI TS 102 361), both timeslots at once (RADIO-SDR-PLAN
//! §8.5 M4): calls, IDs, talkgroups, talker alias, encryption flags and
//! short messages from our own code; voice only through the user's mbelib.
//!
//! **Input.** IQ centred on the channel at any rate from 16 kS/s (Radio
//! gives 48 kS/s, the rate the demodulator works at; others are resampled).
//!
//! **Parameters.** `slot`: "1", "2" or "both" (default): which slots'
//! voice reaches the audio, both mixed. Live.
//!
//! **Results** (Output::Frame), each with its `slot` (1 or 2):
//!
//! - `call_start`: `src`, `dst` (null until known on a late entry),
//!   `callType` ("group" or "private"), `cc`, `encrypted`, `emergency`,
//!   `lateEntry`;
//! - `call_end`: the same, with `alias`, `durationS`, `ber` (bits corrected
//!   over bits checked) and `rssiDb`;
//! - `talker_alias`: `src`, `alias`, as soon as it is complete;
//! - `sms`: `src`, `dst`, `group`, `text`, `sap`, `srcPort`, `dstPort`,
//!   `octets`; a message with no text found in it is `data` instead.
//!
//! **Meta** whenever a slot changes, and each second during a call:
//! `{"mode": "bs" | "ms" | "direct" | null, "cc", "slots": [{"slot",
//! "state" ("idle", "voice", "data"), "src", "dst", "callType", "cc",
//! "alias", "encrypted", "emergency", "callStart", "lastHeard", "rssiDb",
//! "ber"}, ...]}`. An idle slot keeps its last call's IDs, alias and flags,
//! so a client can show "last heard".
//!
//! **Audio**: 8 kHz mono (`Output::Audio`), the chosen slots' voice mixed,
//! silence between calls, 0.3 s behind the air. Only with mbelib: without
//! it everything else decodes, and the session says `needs_codec:mbelib`
//! while a call's voice goes unheard. Encrypted voice is never decoded.
//!
//! - `fec`, `bptc`: the channel codes; `burst`: the burst's fields;
//! - `lc`: link control, late entry, talker alias; `data`: packet data;
//! - `ambe`: AMBE+2 frames and their FEC (not the vocoder);
//! - `demod`: 4FSK and the slot clock; `slot`: one timeslot's calls;
//! - `vocoder`: mbelib, at run time; `tx`: the test transmitter.

pub mod ambe;
pub mod bptc;
pub mod burst;
pub mod data;
pub mod demod;
pub mod fec;
pub mod lc;
pub mod slot;
pub mod tx;
pub mod vocoder;

#[cfg(feature = "python")]
pub(crate) mod py;

use crate::decoder::{
    check_params, CodecId, DecodeError, Decoder, Family, Fields, Info, Input, InputKind, Output, OutputKind, Panel,
    ParamDefault, ParamKind, ParamSpec, Params, Sink, Status, Value,
};
use burst::{Burst, DataType, SyncKind, BURST_BITS, CACH_BITS, CACH_SYMBOLS};
use demod::{Demod, Framer, RawBurst, MAX_MISSES};
use slot::{Event, Slot};
use std::collections::VecDeque;
use vocoder::{Mbelib, VocoderFactory};

const PARAMS: &[ParamSpec] = &[ParamSpec { key: "slot", kind: ParamKind::Text, default: ParamDefault::Text("both") }];

pub static INFO: Info = Info {
    id: "dmr",
    family: Family::DigitalVoice,
    inputs: &[InputKind::Iq],
    rate_hz: demod::RATE,
    bandwidth_hz: 12_500.0,
    outputs: &[OutputKind::Frame, OutputKind::Meta, OutputKind::Audio],
    panel: Panel::Slots,
    params: PARAMS,
    needs: &[CodecId::Mbelib],
    needs_for: &[(CodecId::Mbelib, &["voice"])],
    min_rate_hz: 16_000.0,
};

/// How far behind the air the sound is, seconds: a burst's voice is known
/// 30 ms after it starts, plus however long the app's blocks are.
const AUDIO_DELAY_S: f64 = 0.3;
const AUDIO_RATE: f64 = vocoder::RATE as f64;

/// One slot's sound on the 8 kHz timeline (sample n is UTC n / 8000).
#[derive(Default)]
struct Lane {
    buf: VecDeque<f32>,
    /// Timeline index of buf[0].
    pos: i64,
    /// Where the next voice burst's sound goes.
    cursor: Option<i64>,
}

impl Lane {
    fn write(&mut self, at: i64, pcm: &[f32]) {
        if self.buf.is_empty() {
            self.pos = at;
        }
        let end = self.pos + self.buf.len() as i64;
        if at > end {
            self.buf.extend(std::iter::repeat_n(0.0, (at - end) as usize));
        }
        for (i, &x) in pcm.iter().enumerate() {
            let k = at + i as i64 - self.pos;
            if k < 0 {
                continue;
            }
            let k = k as usize;
            if k < self.buf.len() {
                self.buf[k] = x;
            } else {
                self.buf.push_back(x);
            }
        }
    }

    /// Add [from, from + out.len()) into `out` (when `on`), and forget it.
    fn take(&mut self, from: i64, out: &mut [f32], on: bool) {
        for (i, o) in out.iter_mut().enumerate() {
            let k = from + i as i64 - self.pos;
            if on && k >= 0 && (k as usize) < self.buf.len() {
                *o += self.buf[k as usize];
            }
        }
        let upto = from + out.len() as i64;
        while self.pos < upto && !self.buf.is_empty() {
            self.buf.pop_front();
            self.pos += 1;
        }
        if self.buf.is_empty() {
            self.pos = self.pos.max(upto);
        }
    }
}

#[derive(Default)]
struct Counts {
    bursts: u64,
    syncs: u64,
    calls: u64,
    misses: u64,
}

pub struct Dmr {
    rate: f64,
    demod: Option<Demod>,
    framer: Framer,
    slots: [Slot; 2],
    mode: Option<SyncKind>,
    cc: Option<u8>,
    last_slot: u8,
    /// UTC of the demodulator's 48 kS/s sample 0.
    t_base: f64,
    next_index: Option<u64>,
    /// Which slots reach the audio.
    listen: [bool; 2],
    factory: Option<VocoderFactory>,
    lanes: [Lane; 2],
    mix_pos: Option<i64>,
    meta_dirty: bool,
    meta_t: f64,
    needs_codec: bool,
    snr_db: Option<f32>,
    counts: Counts,
}

impl Default for Dmr {
    fn default() -> Self {
        Self::new()
    }
}

impl Dmr {
    pub fn new() -> Self {
        Dmr {
            rate: 0.0,
            demod: None,
            framer: Framer::new(),
            slots: [Slot::new(1), Slot::new(2)],
            mode: None,
            cc: None,
            last_slot: 2,
            t_base: 0.0,
            next_index: None,
            listen: [true, true],
            factory: None,
            lanes: Default::default(),
            mix_pos: None,
            meta_dirty: true,
            meta_t: f64::NEG_INFINITY,
            needs_codec: false,
            snr_db: None,
            counts: Counts::default(),
        }
    }

    /// A decoder whose voice goes through `factory` instead of mbelib
    /// (tests, or another vocoder library one day).
    pub fn with_vocoder(factory: VocoderFactory) -> Self {
        Dmr { factory: Some(factory), ..Self::new() }
    }

    fn make_vocoder(&self) -> Option<Box<dyn vocoder::Vocoder>> {
        match &self.factory {
            Some(f) => f(),
            None => Mbelib::factory()(),
        }
    }

    fn meta(&self) -> Fields {
        vec![
            ("mode".into(), self.mode.map(|m| Value::Text(m.code().into())).unwrap_or(Value::Null)),
            ("cc".into(), self.cc.map(|c| Value::Num(c as f64)).unwrap_or(Value::Null)),
            ("slots".into(), Value::List(self.slots.iter().map(|s| s.summary()).collect())),
        ]
    }

    fn call_fields(&self, slot: usize, end: Option<f64>) -> Fields {
        let s = &self.slots[slot];
        let c = s.call.as_ref().or(s.last_call.as_ref()).cloned().unwrap_or_default();
        let num = |x: Option<u32>| x.map(|v| Value::Num(v as f64)).unwrap_or(Value::Null);
        let mut f: Fields = vec![
            ("slot".into(), Value::Num(s.n as f64)),
            ("src".into(), num(c.src)),
            ("dst".into(), num(c.dst)),
            ("callType".into(), c.group.map(|g| Value::Text(if g { "group" } else { "private" }.into())).unwrap_or(Value::Null)),
            ("cc".into(), c.cc.map(|v| Value::Num(v as f64)).unwrap_or(Value::Null)),
            ("encrypted".into(), Value::Bool(c.encrypted)),
            ("emergency".into(), Value::Bool(c.emergency)),
        ];
        match end {
            None => f.push(("lateEntry".into(), Value::Bool(c.late_entry))),
            Some(d) => {
                f.push(("alias".into(), c.alias.clone().map(Value::Text).unwrap_or(Value::Null)));
                f.push(("durationS".into(), Value::Num((d * 100.0).round() / 100.0)));
                f.push(("ber".into(), c.ber().map(|b| Value::Num((b * 1e4).round() / 1e4)).unwrap_or(Value::Null)));
                f.push(("rssiDb".into(), c.rssi_db.map(|r| Value::Num((r as f64 * 10.0).round() / 10.0)).unwrap_or(Value::Null)));
            }
        }
        f
    }

    fn emit(&mut self, slot: usize, t: f64, events: Vec<Event>, out: &mut Sink) {
        for e in events {
            match e {
                Event::CallStart => {
                    self.counts.calls += 1;
                    out.push(t, Output::Frame { kind: "call_start", fields: self.call_fields(slot, None), raw: None });
                }
                Event::CallEnd { duration } => {
                    self.lanes[slot].cursor = None;
                    out.push(t, Output::Frame { kind: "call_end", fields: self.call_fields(slot, Some(duration)), raw: None });
                }
                Event::Alias(a) => {
                    let src = self.slots[slot].call.as_ref().and_then(|c| c.src);
                    out.push(
                        t,
                        Output::Frame {
                            kind: "talker_alias",
                            fields: vec![
                                ("slot".into(), Value::Num(slot as f64 + 1.0)),
                                ("src".into(), src.map(|v| Value::Num(v as f64)).unwrap_or(Value::Null)),
                                ("alias".into(), Value::Text(a)),
                            ],
                            raw: None,
                        },
                    );
                }
                Event::Message(r, h) => {
                    let port = |p: Option<u16>| p.map(|v| Value::Num(v as f64)).unwrap_or(Value::Null);
                    let mut f: Fields = vec![
                        ("slot".into(), Value::Num(slot as f64 + 1.0)),
                        ("src".into(), Value::Num(h.src as f64)),
                        ("dst".into(), Value::Num(h.dst as f64)),
                        ("group".into(), Value::Bool(h.group)),
                        ("sap".into(), Value::Num(h.sap as f64)),
                        ("srcPort".into(), port(r.ports.map(|p| p.0))),
                        ("dstPort".into(), port(r.ports.map(|p| p.1))),
                        ("octets".into(), Value::Num(r.octets as f64)),
                    ];
                    let kind = match r.text {
                        Some(text) => {
                            f.push(("text".into(), Value::Text(text)));
                            "sms"
                        }
                        None => "data",
                    };
                    out.push(t, Output::Frame { kind, fields: f, raw: None });
                }
                Event::Changed => self.meta_dirty = true,
            }
        }
    }

    fn handle(&mut self, b: RawBurst, out: &mut Sink) {
        self.counts.bursts += 1;
        let t = self.t_base + b.p0 / demod::RATE;
        let mut cach = [0u8; CACH_BITS];
        let mut bits: Burst = [0u8; BURST_BITS];
        for (i, &y) in b.symbols.iter().enumerate() {
            let d = burst::symbol_to_dibit(y);
            let (hi, lo) = (d >> 1, d & 1);
            if i < CACH_SYMBOLS {
                cach[2 * i] = hi;
                cach[2 * i + 1] = lo;
            } else {
                let k = 2 * (i - CACH_SYMBOLS);
                bits[k] = hi;
                bits[k + 1] = lo;
            }
        }
        if let Some((kind, _, _)) = b.sync {
            self.counts.syncs += 1;
            if self.mode != Some(kind) {
                self.mode = Some(kind);
                self.meta_dirty = true;
            }
        }
        let tact = (self.mode == Some(SyncKind::Bs)).then(|| burst::tact(&cach)).flatten();
        let slot = match b.sync {
            Some((SyncKind::Direct(s), _, _)) => s,
            _ => tact.map(|t| t.slot).unwrap_or(3 - self.last_slot),
        };
        self.last_slot = slot;
        let si = slot as usize - 1;
        let mut ev = Vec::new();
        let mut valid = false;
        let mut voice = None;
        match b.sync {
            Some((_, true, _)) => {
                self.ensure_vocoder(si);
                voice = self.slots[si].voice_burst(&bits, None, self.cc, t, &mut ev);
                valid = self.slots[si].call.is_some();
                if valid {
                    self.voice_heard(si);
                }
            }
            Some((_, false, _)) => {
                if let Some((cc, dt, errs)) = burst::slot_type(&bits) {
                    valid = true;
                    // Golay(20,8) takes any word to within three bits of a
                    // codeword a third of the time: only a clean slot type
                    // names the channel's colour code.
                    if errs == 0 && self.cc != Some(cc) {
                        self.cc = Some(cc);
                        self.meta_dirty = true;
                    }
                    if let Some(dt) = DataType::from(dt) {
                        self.slots[si].data_burst(&bits, cc, dt, t, &mut ev);
                    }
                }
            }
            None => {
                if self.slots[si].expects_voice() {
                    if let Some((e, _)) = burst::emb(&bits).filter(|(e, _)| self.cc.is_none_or(|c| c == e.cc)) {
                        valid = true;
                        self.ensure_vocoder(si);
                        voice = self.slots[si].voice_burst(&bits, Some(e), Some(e.cc), t, &mut ev);
                        self.voice_heard(si);
                    }
                }
                // A repeater keyed up is still there whatever its burst.
                valid |= tact.is_some();
            }
        }
        if valid {
            self.framer.misses = 0;
            self.slots[si].rssi_db = Some(b.rssi_db);
            if let Some(c) = self.slots[si].call.as_mut() {
                c.rssi_db = Some(b.rssi_db);
            }
            let snr = 10.0 * (5.0 / b.mse.max(1e-4)).log10();
            self.snr_db = Some(match self.snr_db {
                Some(s) => s + 0.2 * (snr - s),
                None => snr,
            });
        } else {
            self.framer.misses += 1;
            self.counts.misses += 1;
            if self.framer.misses >= MAX_MISSES {
                self.framer.reset(b.p0.max(0.0) as u64 + 1);
                self.last_slot = 2;
            }
        }
        if let Some(v) = voice {
            let lane = &mut self.lanes[si];
            let at = (v.t * AUDIO_RATE).round() as i64 + (0.1 * AUDIO_RATE) as i64;
            let floor = self.mix_pos.unwrap_or(i64::MIN);
            let cur = match lane.cursor {
                Some(c) if at <= c + 800 && c >= floor => c,
                _ => at.max(floor),
            };
            lane.write(cur, &v.pcm);
            lane.cursor = Some(cur + v.pcm.len() as i64);
        }
        self.emit(si, t, ev, out);
    }

    /// Open the slot's vocoder if it has none: at every voice burst, so an
    /// mbelib installed (and found by "look again") mid-session is used
    /// from the next burst.
    fn ensure_vocoder(&mut self, si: usize) {
        if !self.slots[si].has_vocoder() {
            let v = self.make_vocoder();
            self.slots[si].set_vocoder(v);
        }
    }

    /// A voice burst went by: does it go unheard for want of a vocoder?
    fn voice_heard(&mut self, si: usize) {
        let s = &self.slots[si];
        if !s.has_vocoder() && !s.call.as_ref().is_some_and(|c| c.encrypted) {
            self.needs_codec = true;
        }
    }

    fn mix(&mut self, now: f64, out: &mut Sink) {
        if !self.slots.iter().any(|s| s.has_vocoder()) {
            return;
        }
        let target = ((now - AUDIO_DELAY_S) * AUDIO_RATE).floor() as i64;
        let from = match self.mix_pos {
            // More than two seconds behind (a gap in the input): skip on.
            Some(p) if target - p <= 2 * AUDIO_RATE as i64 => p,
            _ => target - (0.1 * AUDIO_RATE) as i64,
        };
        if target <= from {
            self.mix_pos = Some(from);
            return;
        }
        let mut pcm = vec![0f32; (target - from) as usize];
        for (i, lane) in self.lanes.iter_mut().enumerate() {
            lane.take(from, &mut pcm, self.listen[i]);
        }
        pcm.iter_mut().for_each(|x| *x = x.clamp(-1.0, 1.0));
        out.push(from as f64 / AUDIO_RATE, Output::Audio { pcm, rate: vocoder::RATE, channels: 1 });
        self.mix_pos = Some(target);
    }
}

impl Decoder for Dmr {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        if let Some(s) = params.text("slot") {
            self.listen = match s {
                "1" => [true, false],
                "2" => [false, true],
                "both" => [true, true],
                _ => return Err(DecodeError::BadInput("bad_param")),
            };
        }
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let Input::Iq { samples, rate, t0, index } = input else {
            return Err(DecodeError::BadInput("input"));
        };
        if rate < INFO.min_rate_hz {
            return Err(DecodeError::BadInput("rate"));
        }
        if self.demod.is_none() || (self.rate - rate).abs() > 1e-6 {
            self.demod = Some(Demod::new(rate));
            self.rate = rate;
            self.next_index = None;
        }
        let d = self.demod.as_mut().expect("made above");
        if self.next_index != Some(index) {
            // A gap: the slot clock cannot be trusted across it.
            d.reset();
            self.framer.reset(d.produced);
            self.t_base = t0 - d.produced as f64 / demod::RATE;
        }
        self.next_index = Some(index + samples.len() as u64);
        d.process(samples);
        let bursts = self.framer.run(d);
        let now = self.t_base + d.produced as f64 / demod::RATE;
        for b in bursts {
            self.handle(b, out);
        }
        for si in 0..2 {
            let mut ev = Vec::new();
            self.slots[si].tick(now, &mut ev);
            self.emit(si, now, ev, out);
        }
        let active = self.slots.iter().any(|s| s.call.is_some());
        if self.meta_dirty || (active && now - self.meta_t >= 1.0) {
            self.meta_dirty = false;
            self.meta_t = now;
            out.push(now, Output::Meta { fields: self.meta() });
        }
        self.mix(now, out);
        // Said for as long as a call's voice goes unheard, not only in the
        // blocks that held a voice burst, so the session's `why` is steady.
        // A call that has only had its header so far opens its vocoder now.
        for si in 0..2 {
            if self.slots[si].call.is_some() {
                self.ensure_vocoder(si);
            }
        }
        let unheard = self.slots.iter().any(|s| s.call.as_ref().is_some_and(|c| !c.encrypted) && !s.has_vocoder());
        if std::mem::take(&mut self.needs_codec) || unheard {
            return Err(DecodeError::NeedsCodec(CodecId::Mbelib));
        }
        Ok(())
    }

    fn reset(&mut self) {
        if let Some(d) = self.demod.as_mut() {
            d.reset();
            self.framer.reset(d.produced);
        }
        self.next_index = None;
        for (i, s) in self.slots.iter_mut().enumerate() {
            let mut fresh = Slot::new(i as u8 + 1);
            fresh.set_vocoder(None);
            *s = fresh;
        }
        self.lanes = Default::default();
        self.mode = None;
        self.cc = None;
        self.last_slot = 2;
        self.meta_dirty = true;
    }

    fn status(&self) -> Status {
        let s = &self.slots;
        Status {
            sync: self.framer.locked(),
            snr_db: self.snr_db,
            offset_hz: self.framer.locked().then(|| (self.framer.offset_units() as f64 * demod::HZ_PER_UNIT) as f32),
            baud: None,
            counters: vec![
                ("bursts", self.counts.bursts),
                ("syncs", self.counts.syncs),
                ("misses", self.counts.misses),
                ("calls", self.counts.calls),
                ("voiceFrames", s[0].voice_frames + s[1].voice_frames),
                ("lcOk", s[0].lc_ok + s[1].lc_ok),
                ("lcBad", s[0].lc_bad + s[1].lc_bad),
                ("dataMessages", s[0].data_messages + s[1].data_messages),
            ],
        }
    }
}
