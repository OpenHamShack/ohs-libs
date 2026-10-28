//! One timeslot's calls: what its bursts mean, put together. A slot is a
//! separate conversation (a repeater carries two at once), so each has its
//! own call, embedded LC, talker alias, data message and vocoder.
//!
//! - **A voice call** starts at its voice LC header, or at its first voice
//!   superframe when the header was missed (late entry: the IDs arrive
//!   with the embedded LC, within a superframe or two). It ends at its
//!   terminator, or when the slot has carried no voice for 360 ms.
//! - **Encryption** is reported, never undone: the LC's privacy option,
//!   a PI header, or the EMB's PI bit marks the call, and its voice is
//!   not handed to the vocoder at all.
//! - **Data**: a data header, then its blocks; a finished message becomes
//!   an `sms` result when text is found in it, otherwise `data`.

use super::ambe;
use super::burst::{self, Burst, DataType, Emb};
use super::data::{self, Header, Message};
use super::lc::{self, EmbeddedLc, Lc, TalkerAlias};
use super::vocoder::{Vocoder, FRAME_SAMPLES};
use crate::decoder::Value;

/// A voice call ends after this long with no voice in its slot.
pub const CALL_TIMEOUT_S: f64 = 0.36;
/// A data message is dropped after this long without its next block.
const DATA_TIMEOUT_S: f64 = 1.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Idle,
    Voice,
    Data,
}

impl State {
    pub fn code(self) -> &'static str {
        match self {
            State::Idle => "idle",
            State::Voice => "voice",
            State::Data => "data",
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Call {
    pub src: Option<u32>,
    pub dst: Option<u32>,
    pub group: Option<bool>,
    pub cc: Option<u8>,
    pub encrypted: bool,
    pub emergency: bool,
    pub alias: Option<String>,
    pub start: f64,
    pub last: f64,
    pub late_entry: bool,
    /// Bits corrected and bits checked over the call (AMBE C0 and C1,
    /// BPTC), for its BER estimate.
    pub errs: u64,
    pub checked: u64,
    pub rssi_db: Option<f32>,
}

impl Call {
    pub fn ber(&self) -> Option<f64> {
        (self.checked > 0).then(|| self.errs as f64 / self.checked as f64)
    }
}

/// What a slot tells the decoder after a burst.
#[derive(Debug, PartialEq)]
pub enum Event {
    CallStart,
    CallEnd { duration: f64 },
    Alias(String),
    Message(data::Received, Header),
    /// The slot's summary changed (state, IDs, alias, encrypted).
    Changed,
}

/// Each voice burst's sound: three 20 ms frames, at the burst's time.
pub struct Voice {
    pub t: f64,
    pub pcm: Vec<f32>,
}

pub struct Slot {
    pub n: u8,
    pub state: State,
    pub call: Option<Call>,
    /// The last call, kept for the summary once it has ended.
    pub last_call: Option<Call>,
    pub last_heard: Option<f64>,
    /// Superframe position of the last voice burst (0 is A, with the sync).
    voice_idx: Option<u8>,
    emb_lc: EmbeddedLc,
    alias: TalkerAlias,
    /// A PI header ahead of the call it belongs to.
    pi_pending: bool,
    msg: Option<(Message, f64)>,
    vocoder: Option<Box<dyn Vocoder>>,
    pub voice_frames: u64,
    pub lc_ok: u64,
    pub lc_bad: u64,
    pub data_messages: u64,
    pub rssi_db: Option<f32>,
    /// Errors and bits over the last bursts, for a running BER.
    recent: std::collections::VecDeque<(u32, u32)>,
}

impl Slot {
    pub fn new(n: u8) -> Self {
        Slot {
            n,
            state: State::Idle,
            call: None,
            last_call: None,
            last_heard: None,
            voice_idx: None,
            emb_lc: EmbeddedLc::default(),
            alias: TalkerAlias::default(),
            pi_pending: false,
            msg: None,
            vocoder: None,
            voice_frames: 0,
            lc_ok: 0,
            lc_bad: 0,
            data_messages: 0,
            rssi_db: None,
            recent: Default::default(),
        }
    }

    pub fn set_vocoder(&mut self, v: Option<Box<dyn Vocoder>>) {
        self.vocoder = v;
    }

    pub fn has_vocoder(&self) -> bool {
        self.vocoder.is_some()
    }

    /// The running BER over the slot's last bursts.
    pub fn ber(&self) -> Option<f64> {
        let (e, n) = self.recent.iter().fold((0u64, 0u64), |a, &(e, n)| (a.0 + e as u64, a.1 + n as u64));
        (n > 0).then(|| e as f64 / n as f64)
    }

    fn count(&mut self, errs: u32, bits: u32) {
        self.recent.push_back((errs, bits));
        if self.recent.len() > 33 {
            self.recent.pop_front();
        }
        if let Some(c) = self.call.as_mut() {
            c.errs += errs as u64;
            c.checked += bits as u64;
        }
    }

    fn start_call(&mut self, t: f64, late: bool, ev: &mut Vec<Event>) {
        if self.call.is_some() {
            return;
        }
        self.emb_lc.reset();
        self.alias.reset();
        self.call = Some(Call { start: t, last: t, late_entry: late, encrypted: self.pi_pending, ..Default::default() });
        self.pi_pending = false;
        self.state = State::Voice;
        ev.push(Event::CallStart);
        ev.push(Event::Changed);
    }

    pub fn end_call(&mut self, t: f64, ev: &mut Vec<Event>) {
        if let Some(c) = self.call.take() {
            let duration = (t.max(c.last) - c.start).max(0.0);
            self.last_call = Some(c);
            self.voice_idx = None;
            self.state = if self.msg.is_some() { State::Data } else { State::Idle };
            ev.push(Event::CallEnd { duration });
            ev.push(Event::Changed);
        }
    }

    /// Apply a voice LC to the call (from a header or the embedded LC).
    fn apply_lc(&mut self, lc: &Lc, cc: u8, ev: &mut Vec<Event>) {
        let Some(c) = self.call.as_mut() else { return };
        let o = lc.options();
        let new = (Some(lc.src()), Some(lc.dst()), Some(lc.flco() == lc::FLCO_GROUP), o.privacy || lc.protected(), o.emergency);
        let old = (c.src, c.dst, c.group, c.encrypted, c.emergency);
        c.src = new.0;
        c.dst = new.1;
        c.group = new.2;
        c.encrypted |= new.3;
        c.emergency |= new.4;
        c.cc = Some(cc);
        if (c.src, c.dst, c.group, c.encrypted, c.emergency) != old {
            ev.push(Event::Changed);
        }
    }

    /// Timeouts, called as time passes even with no burst for this slot.
    pub fn tick(&mut self, now: f64, ev: &mut Vec<Event>) {
        if self.call.as_ref().is_some_and(|c| now - c.last > CALL_TIMEOUT_S) {
            let last = self.call.as_ref().map(|c| c.last).unwrap_or(now);
            self.end_call(last, ev);
        }
        if self.msg.as_ref().is_some_and(|(_, t)| now - t > DATA_TIMEOUT_S) {
            self.msg = None;
            if self.state == State::Data {
                self.state = State::Idle;
                ev.push(Event::Changed);
            }
        }
    }

    /// A data burst with its colour code and type.
    pub fn data_burst(&mut self, b: &Burst, cc: u8, dt: DataType, t: f64, ev: &mut Vec<Event>) {
        // A repeater's idle bursts are not anyone being heard.
        if dt != DataType::Idle {
            self.last_heard = Some(t);
        }
        let info = burst::info(b);
        match dt {
            DataType::VoiceLcHeader => match lc::decode_full(&info, lc::MASK_HEADER) {
                Some((l, fixed)) if l.is_voice() || l.protected() => {
                    self.lc_ok += 1;
                    self.count(fixed, 196);
                    // A new call's header ends the old one in its slot.
                    let same = self.call.as_ref().is_some_and(|c| c.src == Some(l.src()) && c.dst == Some(l.dst()));
                    if self.call.is_some() && !same {
                        self.end_call(t, ev);
                    }
                    self.start_call(t, false, ev);
                    self.apply_lc(&l, cc, ev);
                    if let Some(c) = self.call.as_mut() {
                        c.last = t;
                    }
                }
                _ => self.lc_bad += 1,
            },
            DataType::TerminatorLc => match lc::decode_full(&info, lc::MASK_TERMINATOR) {
                Some((l, fixed)) => {
                    self.lc_ok += 1;
                    self.count(fixed, 196);
                    if self.call.is_some() {
                        self.apply_lc(&l, cc, ev);
                        if let Some(c) = self.call.as_mut() {
                            c.last = t;
                        }
                        self.end_call(t, ev);
                    }
                }
                None => self.lc_bad += 1,
            },
            DataType::PiHeader => {
                let (blk, _) = data::decode_block12(&info);
                if data::crc_ok(&blk, data::MASK_PI_HEADER) {
                    match self.call.as_mut() {
                        Some(c) if !c.encrypted => {
                            c.encrypted = true;
                            ev.push(Event::Changed);
                        }
                        Some(_) => {}
                        None => self.pi_pending = true,
                    }
                }
            }
            DataType::DataHeader => {
                let (blk, fixed) = data::decode_block12(&info);
                if let Some(h) = Header::parse(&blk) {
                    self.count(fixed, 196);
                    if h.blocks > 0 && matches!(h.dpf, data::DPF_UNCONFIRMED | data::DPF_CONFIRMED | data::DPF_SHORT_DEFINED) {
                        self.msg = Some((Message::new(h), t));
                        if self.state != State::Data && self.call.is_none() {
                            self.state = State::Data;
                            ev.push(Event::Changed);
                        }
                    }
                }
            }
            DataType::Rate12Data | DataType::Rate34Data => {
                if self.msg.is_none() {
                    return;
                }
                let (block, errs, bits): (Vec<u8>, u32, u32) = if dt == DataType::Rate12Data {
                    let (b, fixed) = data::decode_block12(&info);
                    (b.to_vec(), fixed, 196)
                } else {
                    let (b, errs) = data::decode_rate34(&info);
                    (b.to_vec(), errs, 98)
                };
                self.count(errs, bits);
                let (m, tm) = self.msg.as_mut().expect("checked above");
                *tm = t;
                if m.push(&block) {
                    let (m, _) = self.msg.take().expect("checked above");
                    if let Some(p) = m.payload() {
                        self.data_messages += 1;
                        ev.push(Event::Message(data::read(&m.header, &p), m.header));
                    }
                    if self.state == State::Data {
                        self.state = State::Idle;
                        ev.push(Event::Changed);
                    }
                }
            }
            _ => {}
        }
    }

    /// Is this sync-less burst a voice burst of the call in progress? Only
    /// right after a voice burst of this slot (B-F follow A).
    pub fn expects_voice(&self) -> bool {
        matches!(self.voice_idx, Some(i) if i < 5)
    }

    /// A voice burst: A (with its sync, `emb` None) or B-F.
    pub fn voice_burst(&mut self, b: &Burst, emb: Option<Emb>, cc: Option<u8>, t: f64, ev: &mut Vec<Event>) -> Option<Voice> {
        self.last_heard = Some(t);
        let idx = match emb {
            None => 0,
            Some(_) => self.voice_idx.map(|i| i + 1).unwrap_or(1),
        };
        self.voice_idx = Some(idx);
        self.start_call(t, true, ev);
        if let Some(e) = emb {
            if e.pi {
                if let Some(c) = self.call.as_mut() {
                    if !c.encrypted {
                        c.encrypted = true;
                        ev.push(Event::Changed);
                    }
                }
            }
            if (1..=4).contains(&idx) {
                if let Some((l, fixed)) = self.emb_lc.push(&burst::embedded(b), e.lcss) {
                    self.count(fixed, 128);
                    if l.is_voice() {
                        self.apply_lc(&l, e.cc, ev);
                    } else if let Some(a) = self.alias.push(&l) {
                        if let Some(c) = self.call.as_mut() {
                            if c.alias.as_deref() != Some(a.as_str()) {
                                c.alias = Some(a.clone());
                                ev.push(Event::Alias(a));
                                ev.push(Event::Changed);
                            }
                        }
                    }
                }
            } else if idx == 0 || idx == 5 {
                self.emb_lc.reset();
            }
        }
        let encrypted = self.call.as_ref().is_some_and(|c| c.encrypted);
        if let Some(c) = self.call.as_mut() {
            c.last = t;
            if c.cc.is_none() {
                c.cc = cc;
            }
        }
        let mut pcm = Vec::with_capacity(3 * FRAME_SAMPLES);
        let mut errs = 0;
        for f in ambe::frames_of(b) {
            let d = ambe::decode(&f);
            errs += d.errs;
            self.voice_frames += 1;
            if !encrypted {
                if let Some(v) = self.vocoder.as_mut() {
                    pcm.extend_from_slice(&v.decode(&d.data, d.errs_c0, d.errs));
                }
            }
        }
        self.count(errs, 3 * 47);
        (!pcm.is_empty()).then_some(Voice { t, pcm })
    }

    /// The slot's summary for `meta`.
    pub fn summary(&self) -> Value {
        let c = self.call.as_ref().or(self.last_call.as_ref());
        let num = |x: Option<u32>| x.map(|v| Value::Num(v as f64)).unwrap_or(Value::Null);
        let call_type = c.and_then(|c| c.group).map(|g| Value::Text(if g { "group" } else { "private" }.into())).unwrap_or(Value::Null);
        Value::Map(vec![
            ("slot".into(), Value::Num(self.n as f64)),
            ("state".into(), Value::Text(self.state.code().into())),
            ("src".into(), num(c.and_then(|c| c.src))),
            ("dst".into(), num(c.and_then(|c| c.dst))),
            ("callType".into(), call_type),
            ("cc".into(), c.and_then(|c| c.cc).map(|v| Value::Num(v as f64)).unwrap_or(Value::Null)),
            ("alias".into(), c.and_then(|c| c.alias.clone()).map(Value::Text).unwrap_or(Value::Null)),
            ("encrypted".into(), Value::Bool(c.is_some_and(|c| c.encrypted))),
            ("emergency".into(), Value::Bool(c.is_some_and(|c| c.emergency))),
            ("callStart".into(), c.map(|c| Value::Num(c.start)).unwrap_or(Value::Null)),
            ("lastHeard".into(), self.last_heard.map(Value::Num).unwrap_or(Value::Null)),
            ("rssiDb".into(), self.rssi_db.map(|r| Value::Num((r as f64 * 10.0).round() / 10.0)).unwrap_or(Value::Null)),
            ("ber".into(), self.ber().map(|b| Value::Num((b * 1e4).round() / 1e4)).unwrap_or(Value::Null)),
        ])
    }
}
