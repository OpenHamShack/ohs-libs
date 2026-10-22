//! The one decoder interface (RADIO-SDR-PLAN §4.1): every decoder, from
//! RTTY now to DAB+ and DMR later, is a `Decoder` fed `Input` and writing
//! `Output`s to a `Sink`. The host (host.rs) runs them; the catalogue
//! (catalogue.rs) lists their static `Info`.
//!
//! Rules that cut across every decoder:
//!
//! - **No user-visible text.** Errors, reasons and labels are snake_case
//!   codes; the app turns them into catalog sentences (I18N.md rules 1, 8).
//! - **Time comes from the samples, never the clock.** `t0` and `index` give
//!   every output its time, so a recording played back keeps its own.
//! - **Receive only by construction.** Nothing in this crate transmits.

use rustfft::num_complex::Complex32;
use std::collections::BTreeMap;

/// A block of samples for one session. `index` is the number of the
/// block's first sample since the session started; a jump is a gap.
/// `t0` is the UTC (seconds) of that first sample.
#[derive(Clone, Copy, Debug)]
pub enum Input<'a> {
    /// Complex baseband centred on the session's frequency.
    Iq { samples: &'a [Complex32], rate: f64, t0: f64, index: u64 },
    /// Real audio, such as the VFO's 12 kHz.
    Audio { samples: &'a [f32], rate: f64, t0: f64, index: u64 },
}

impl Input<'_> {
    pub fn rate(&self) -> f64 {
        match self {
            Input::Iq { rate, .. } | Input::Audio { rate, .. } => *rate,
        }
    }
    pub fn index(&self) -> u64 {
        match self {
            Input::Iq { index, .. } | Input::Audio { index, .. } => *index,
        }
    }
    pub fn t0(&self) -> f64 {
        match self {
            Input::Iq { t0, .. } | Input::Audio { t0, .. } => *t0,
        }
    }
    pub fn len(&self) -> usize {
        match self {
            Input::Iq { samples, .. } => samples.len(),
            Input::Audio { samples, .. } => samples.len(),
        }
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Family {
    Amateur,
    Broadcast,
    DigitalVoice,
}

impl Family {
    pub fn code(self) -> &'static str {
        match self {
            Family::Amateur => "amateur",
            Family::Broadcast => "broadcast",
            Family::DigitalVoice => "digital_voice",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputKind {
    Iq,
    Audio,
}

impl InputKind {
    pub fn code(self) -> &'static str {
        match self {
            InputKind::Iq => "iq",
            InputKind::Audio => "audio",
        }
    }
    pub fn parse(s: &str) -> Option<InputKind> {
        match s {
            "iq" => Some(InputKind::Iq),
            "audio" => Some(InputKind::Audio),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutputKind {
    Text,
    Frame,
    Image,
    Audio,
    Meta,
}

impl OutputKind {
    pub fn code(self) -> &'static str {
        match self {
            OutputKind::Text => "text",
            OutputKind::Frame => "frame",
            OutputKind::Image => "image",
            OutputKind::Audio => "audio",
            OutputKind::Meta => "meta",
        }
    }
}

/// How a client shows a decoder's results, so a new decoder needs no client
/// release (RADIO-SDR-PLAN §4.5).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Panel {
    Text,
    Slots,
    Frames { columns: &'static [&'static str] },
    Image,
    Meta,
    Audio,
}

impl Panel {
    pub fn code(self) -> &'static str {
        match self {
            Panel::Text => "text",
            Panel::Slots => "slots",
            Panel::Frames { .. } => "frames",
            Panel::Image => "image",
            Panel::Meta => "meta",
            Panel::Audio => "audio",
        }
    }
}

/// A codec a decoder cannot work without, or needs only for its audio.
/// Never linked: opened at run time from the user's own install (codecs/).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum CodecId {
    Codec2,
    Faad2,
    FdkAac,
    Mbelib,
}

impl CodecId {
    pub const ALL: [CodecId; 4] = [CodecId::Codec2, CodecId::Faad2, CodecId::FdkAac, CodecId::Mbelib];

    pub fn code(self) -> &'static str {
        match self {
            CodecId::Codec2 => "codec2",
            CodecId::Faad2 => "faad2",
            CodecId::FdkAac => "fdk-aac",
            CodecId::Mbelib => "mbelib",
        }
    }
    pub fn parse(s: &str) -> Option<CodecId> {
        CodecId::ALL.into_iter().find(|c| c.code() == s)
    }
}

/// A parameter's value. Parameters are small and few, so one loose type
/// serves every decoder.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Num(f64),
    Bool(bool),
    Text(String),
}

#[derive(Clone, Copy, Debug)]
pub enum ParamKind {
    /// Any number from min to max.
    Range { min: f64, max: f64 },
    /// One of these numbers (RTTY's baud rates and shifts).
    Choice(&'static [f64]),
    Bool,
}

/// One parameter. `key` doubles as the label's catalog key in the app
/// (`decoder.<id>.param.<key>`): there is never text here.
#[derive(Clone, Copy, Debug)]
pub struct ParamSpec {
    pub key: &'static str,
    pub kind: ParamKind,
    pub default: ParamDefault,
}

#[derive(Clone, Copy, Debug)]
pub enum ParamDefault {
    Num(f64),
    Bool(bool),
}

/// The catalogue entry of one decoder kind: static, one per kind.
#[derive(Debug)]
pub struct Info {
    pub id: &'static str,
    pub family: Family,
    /// Which inputs it takes natively; the host adapts audio to IQ for an
    /// IQ-only decoder (host.rs).
    pub inputs: &'static [InputKind],
    /// Preferred input rate.
    pub rate_hz: f64,
    /// The channel it needs.
    pub bandwidth_hz: f64,
    pub outputs: &'static [OutputKind],
    pub panel: Panel,
    pub params: &'static [ParamSpec],
    pub needs: &'static [CodecId],
}

/// Parameters as given, by key. Unknown keys are refused by the decoder's
/// `configure`, not ignored: a typo should not silently fall back.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Params(pub BTreeMap<String, Value>);

impl Params {
    pub fn new() -> Self {
        Params(BTreeMap::new())
    }
    pub fn with(mut self, key: &str, v: Value) -> Self {
        self.0.insert(key.to_string(), v);
        self
    }
    pub fn num(&self, key: &str) -> Option<f64> {
        match self.0.get(key) {
            Some(Value::Num(x)) => Some(*x),
            Some(Value::Bool(b)) => Some(if *b { 1.0 } else { 0.0 }),
            _ => None,
        }
    }
    pub fn flag(&self, key: &str) -> Option<bool> {
        match self.0.get(key) {
            Some(Value::Bool(b)) => Some(*b),
            Some(Value::Num(x)) => Some(*x != 0.0),
            _ => None,
        }
    }
}

/// Check `params` against `specs`: every key known, every value of its
/// kind and in range. Decoders call this first in `configure`.
pub fn check_params(params: &Params, specs: &[ParamSpec]) -> Result<(), DecodeError> {
    for (k, v) in &params.0 {
        let Some(spec) = specs.iter().find(|s| s.key == k) else {
            return Err(DecodeError::BadInput("unknown_param"));
        };
        let ok = match (spec.kind, v) {
            (ParamKind::Range { min, max }, Value::Num(x)) => x.is_finite() && *x >= min && *x <= max,
            (ParamKind::Choice(c), Value::Num(x)) => c.iter().any(|y| (y - x).abs() < 1e-6),
            (ParamKind::Bool, Value::Bool(_)) => true,
            (ParamKind::Bool, Value::Num(x)) => *x == 0.0 || *x == 1.0,
            _ => false,
        };
        if !ok {
            return Err(DecodeError::BadInput("bad_param"));
        }
    }
    Ok(())
}

pub type Fields = Vec<(String, Value)>;

#[derive(Clone, Debug, PartialEq)]
pub enum ImageEvent {
    Start { width: u32, height: u32, format: &'static str },
    Lines { from: u32, rgb: Vec<u8> },
    Done { file: String },
}

/// What a decoder produces. Five kinds cover every family in the plan.
#[derive(Clone, Debug, PartialEq)]
pub enum Output {
    /// Streaming characters.
    Text { text: String, offset_hz: f32, snr_db: Option<f32> },
    /// A POCSAG message, an RDS group, a DMR header.
    Frame { kind: &'static str, fields: Fields, raw: Option<Vec<u8>> },
    Image { event: ImageEvent },
    /// WFM, DAB+, voice (M3-M4).
    Audio { pcm: Vec<f32>, rate: u32, channels: u8 },
    /// RDS PS/RT, a DAB service list, a talkgroup.
    Meta { fields: Fields },
}

impl Output {
    pub fn kind(&self) -> OutputKind {
        match self {
            Output::Text { .. } => OutputKind::Text,
            Output::Frame { .. } => OutputKind::Frame,
            Output::Image { .. } => OutputKind::Image,
            Output::Audio { .. } => OutputKind::Audio,
            Output::Meta { .. } => OutputKind::Meta,
        }
    }
}

/// Where a decoder writes its results, each with the time (UTC seconds,
/// from the samples) it belongs to.
#[derive(Default, Debug)]
pub struct Sink {
    pub items: Vec<(f64, Output)>,
}

impl Sink {
    pub fn new() -> Self {
        Sink { items: Vec::new() }
    }
    pub fn push(&mut self, t: f64, out: Output) {
        self.items.push((t, out));
    }
    pub fn take(&mut self) -> Vec<(f64, Output)> {
        std::mem::take(&mut self.items)
    }
}

/// A snake_case code with no text: the app words it.
pub type Code = &'static str;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DecodeError {
    /// Wrong rate or kind: a bug in the caller.
    BadInput(Code),
    /// Asked for audio with no codec: the session keeps decoding everything else.
    NeedsCodec(CodecId),
    /// The loaded codec refused a frame (counted; not fatal).
    Codec(CodecId, Code),
    /// The session goes to `error`.
    Internal(Code),
}

impl DecodeError {
    pub fn code(&self) -> &'static str {
        match self {
            DecodeError::BadInput(c) | DecodeError::Internal(c) | DecodeError::Codec(_, c) => c,
            DecodeError::NeedsCodec(_) => "needs_codec",
        }
    }
}

/// What a decoder says about itself now. Counters are (code, value) pairs so
/// each decoder can count what matters to it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Status {
    /// The decoder hears its signal (RTTY: the squelch is open).
    pub sync: bool,
    pub snr_db: Option<f32>,
    pub offset_hz: Option<f32>,
    /// A measured rate: RTTY's baud.
    pub baud: Option<f32>,
    pub counters: Vec<(&'static str, u64)>,
}

pub trait Decoder: Send {
    fn info(&self) -> &'static Info;
    /// Live where the decoder allows; otherwise the host starts a new one.
    fn configure(&mut self, params: &Params) -> Result<(), DecodeError>;
    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError>;
    /// A retune, or a gap the decoder cannot bridge.
    fn reset(&mut self);
    fn status(&self) -> Status;
}
