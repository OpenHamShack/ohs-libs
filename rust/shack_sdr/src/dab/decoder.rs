//! The `dab` decoder (RADIO-SDR-PLAN §8.5 M3, brought forward for Radio,
//! Brian 23/10/2026): one Band III block's IQ in; the ensemble, its
//! services and the chosen service's sound out.
//!
//! **Input.** IQ centred on the block, at 2.048 MS/s (the elementary rate,
//! taken as it is) or any higher rate, resampled here (a Pluto runs DAB at
//! 2.4 MS/s, above its no-FIR floor). Below 2.048 MS/s is refused
//! (`bad_input`, `rate`): the block is 1.536 MHz wide and the OFDM grid is
//! 2.048 MS/s. Whether the receiver's span holds the block is the engine's
//! to say (`outside_span`, `Host::set_outside`).
//!
//! **Parameters.** `service`: the SId in hex ("C221") to play, "" for none;
//! it may name a service not heard yet, and changes live. `block`: the
//! Band III block's name ("11D"), for the record (the engine tunes; an
//! unknown name is refused).
//!
//! **Output.** A `meta` result whenever what it hears changes:
//! `{"sync", "ensemble": {"id", "label", "shortLabel"} | null, "services":
//! [{"sid", "label", "shortLabel", "bitrate", "kind" ("dab" | "dab+"),
//! "subchannel", "protection", "why", "needs"}], "playing": sid | null,
//! "why": code | null, "audio": {"codec", "rate", "channels"} | null}`;
//! and the playing service's sound as `Output::Audio`, which the host puts
//! in the session's audio ring for the app to pull.
//!
//! **Codes** (`why`, the app words them): `no_signal` (no null symbol: no
//! DAB here), `no_sync` (a null but no phase reference), `fic_errors`
//! (in sync but the FIC fails its CRCs: too weak), `service_not_found`,
//! `needs_codec:faad2` (a DAB+ service chosen with no faad2; the session
//! still lists everything, and old DAB services still play), `codec_config`
//! / `codec_open` (faad2 refused this stream), `unsupported_protection`.
//!
//! **faad2** is opened only from the file the codec loader's probe passed
//! (codecs::library), never linked; a test can hand in a stand-in
//! (`Dab::with_aac`).

use super::audio::{self, Aac, AacFactory, DabPlusFormat, Mp2Stream, Superframes};
use super::fic::{Ensemble, FicDecoder, Subchannel};
use super::msc::SubchannelDecoder;
use super::ofdm::{Demod, Event};
use super::pad::{self, PadDecoder};
use super::tables::{self, CIF_SYMBOLS, CU_BITS, FIC_CODED_BITS, RATE, SYMBOL_BITS};
use crate::decoder::*;
use crate::dsp::Resampler;
use rustfft::num_complex::Complex32;

const PARAMS: &[ParamSpec] = &[
    ParamSpec { key: "service", kind: ParamKind::Text, default: ParamDefault::Text("") },
    ParamSpec { key: "block", kind: ParamKind::Text, default: ParamDefault::Text("") },
];

pub static INFO: Info = Info {
    id: "dab",
    family: Family::Broadcast,
    inputs: &[InputKind::Iq],
    rate_hz: RATE,
    bandwidth_hz: 1_536_000.0,
    outputs: &[OutputKind::Meta, OutputKind::Audio],
    panel: Panel::Meta,
    params: PARAMS,
    needs: &[CodecId::Faad2],
    needs_for: &[(CodecId::Faad2, &["dab+"])],
    min_rate_hz: RATE,
};

/// What plays.
enum Stream {
    Mp2(Mp2Stream),
    Plus { sf: Superframes, aac: Option<Box<dyn Aac>>, format: Option<DabPlusFormat>, why: Option<&'static str>, pad: PadDecoder },
}

impl Stream {
    /// The playing service's dynamic label (programme text), when one is whole.
    fn dls(&self) -> Option<&str> {
        match self {
            Stream::Mp2(m) => m.pad.label.as_deref(),
            Stream::Plus { pad, .. } => pad.label.as_deref(),
        }
    }
}

struct Player {
    sid: u32,
    sub: SubchannelDecoder,
    stream: Stream,
}

#[derive(Default, Clone, Copy)]
struct Counts {
    frames: u64,
    misses: u64,
    audio_frames: u64,
    audio_errors: u64,
    aus: u64,
    au_errors: u64,
    codec_errors: u64,
}

pub struct Dab {
    service: Option<u32>,
    block: String,
    rate: f64,
    resampler: Option<Resampler<Complex32>>,
    /// The next input index expected, to see gaps.
    next_index: Option<u64>,
    /// UTC of input sample 0 (t0 - index / rate), for every output's time.
    t_base: f64,
    demod: Demod,
    fic: FicDecoder,
    ens: Ensemble,
    player: Option<Player>,
    sync: bool,
    /// Frames in a row whose FIC gave nothing.
    deaf: u32,
    why: Option<String>,
    /// Frames since the ensemble's services were first seen, to give a
    /// chosen service time to appear before saying it is not there.
    frames_with_services: u64,
    aac_factory: Option<AacFactory>,
    last_meta: Option<Fields>,
    counts: Counts,
    snr_db: Option<f32>,
}

impl Default for Dab {
    fn default() -> Self {
        Self::new()
    }
}

/// "C221" for a 16-bit SId, eight digits for a 32-bit one.
pub fn sid_hex(sid: u32) -> String {
    if sid > 0xffff {
        format!("{sid:08X}")
    } else {
        format!("{sid:04X}")
    }
}

fn parse_sid(s: &str) -> Result<Option<u32>, DecodeError> {
    let t = s.trim();
    if t.is_empty() {
        return Ok(None);
    }
    let t = t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")).unwrap_or(t);
    if t.len() > 8 {
        return Err(DecodeError::BadInput("bad_param"));
    }
    u32::from_str_radix(t, 16).map(Some).map_err(|_| DecodeError::BadInput("bad_param"))
}

fn faad_factory() -> AacFactory {
    Box::new(|asc: &[u8]| -> Result<Box<dyn Aac>, &'static str> {
        let lib = crate::codecs::library(CodecId::Faad2).ok_or("needs_codec:faad2")?;
        Ok(Box::new(audio::faad::Faad::open(lib, asc)?))
    })
}

impl Dab {
    pub fn new() -> Self {
        Dab {
            service: None,
            block: String::new(),
            rate: RATE,
            resampler: None,
            next_index: None,
            t_base: 0.0,
            demod: Demod::new(),
            fic: FicDecoder::new(),
            ens: Ensemble::default(),
            player: None,
            sync: false,
            deaf: 0,
            why: None,
            frames_with_services: 0,
            aac_factory: None,
            last_meta: None,
            counts: Counts::default(),
            snr_db: None,
        }
    }

    /// A decoder whose DAB+ audio goes through `factory` instead of faad2
    /// (tests, or another AAC library one day).
    pub fn with_aac(factory: AacFactory) -> Self {
        Dab { aac_factory: Some(factory), ..Self::new() }
    }

    /// Is an AAC decoder there to be had?
    fn aac_available(&self) -> bool {
        self.aac_factory.is_some() || crate::codecs::library(CodecId::Faad2).is_some()
    }

    fn make_aac(&self, asc: &[u8]) -> Result<Box<dyn Aac>, &'static str> {
        match &self.aac_factory {
            Some(f) => f(asc),
            None => faad_factory()(asc),
        }
    }

    pub fn ensemble(&self) -> &Ensemble {
        &self.ens
    }

    /// The MSC symbols (within a CIF) the playing sub-channel occupies.
    fn mask(&self) -> [bool; CIF_SYMBOLS] {
        let mut m = [false; CIF_SYMBOLS];
        if let Some(p) = &self.player {
            let from = p.sub.sub.start as usize * CU_BITS;
            let to = from + p.sub.sub.protection.size() * CU_BITS;
            for j in from / SYMBOL_BITS..=((to - 1) / SYMBOL_BITS).min(CIF_SYMBOLS - 1) {
                m[j] = true;
            }
        }
        m
    }

    /// Start (or keep) the player for the chosen service, once the FIC has
    /// said where it is.
    fn update_player(&mut self) {
        let Some(sid) = self.service else {
            self.player = None;
            return;
        };
        let Some(comp) = self.ens.services.get(&sid).and_then(|s| s.audio().cloned()) else {
            self.player = None;
            return;
        };
        let Some(sub) = self.ens.subchannels.get(&comp.subch).cloned() else { return };
        if let Some(p) = &self.player {
            if p.sid == sid && p.sub.sub == sub {
                return;
            }
        }
        let Some(dec) = SubchannelDecoder::new(sub.clone()) else {
            self.player = None;
            self.why = Some("unsupported_protection".into());
            return;
        };
        let stream = if comp.ascty == 63 {
            Stream::Plus {
                sf: Superframes::new(sub.protection.bitrate().unwrap_or(8)),
                aac: None,
                format: None,
                why: None,
                pad: PadDecoder::new(),
            }
        } else {
            Stream::Mp2(Mp2Stream::new())
        };
        self.player = Some(Player { sid, sub: dec, stream });
    }

    fn kind_of(&self, ascty: u8) -> &'static str {
        if ascty == 63 {
            "dab+"
        } else {
            "dab"
        }
    }

    /// Is the ensemble fully known: its label, and every audio service's
    /// label and sub-channel? A band scan can stop listening here.
    pub fn complete(&self) -> bool {
        let audio: Vec<_> = self.ens.services.values().filter(|s| s.audio().is_some()).collect();
        self.ens.eid.is_some()
            && self.ens.label.is_some()
            && !audio.is_empty()
            && audio.iter().all(|s| s.label.is_some() && self.ens.subchannels.contains_key(&s.audio().unwrap().subch))
    }

    /// The feed's picture of what is heard.
    fn snapshot(&self) -> Fields {
        let text = |s: &Option<String>| s.clone().map(Value::Text).unwrap_or(Value::Null);
        let aac = self.aac_available();
        let ensemble = match self.ens.eid {
            Some(eid) => Value::Map(vec![
                ("id".into(), Value::Text(format!("{eid:04X}"))),
                ("label".into(), text(&self.ens.label)),
                ("shortLabel".into(), text(&self.ens.short_label)),
            ]),
            None => Value::Null,
        };
        let mut services = Vec::new();
        for s in self.ens.services.values() {
            let Some(c) = s.audio() else { continue };
            if c.tmid != 0 {
                continue;
            }
            let sub: Option<&Subchannel> = self.ens.subchannels.get(&c.subch);
            let kind = self.kind_of(c.ascty);
            let needs: Vec<Value> = if kind == "dab+" && !aac { vec![Value::Text("faad2".into())] } else { vec![] };
            services.push(Value::Map(vec![
                ("sid".into(), Value::Text(sid_hex(s.sid))),
                ("label".into(), text(&s.label)),
                ("shortLabel".into(), text(&s.short_label)),
                ("bitrate".into(), sub.and_then(|x| x.protection.bitrate()).map(|b| Value::Num(b as f64)).unwrap_or(Value::Null)),
                ("kind".into(), Value::Text(kind.into())),
                ("subchannel".into(), Value::Num(c.subch as f64)),
                ("protection".into(), sub.map(|x| Value::Text(x.protection.level_code())).unwrap_or(Value::Null)),
                ("why".into(), if needs.is_empty() { Value::Null } else { Value::Text("needs_codec:faad2".into()) }),
                ("needs".into(), Value::List(needs)),
            ]));
        }
        let audio = match &self.player {
            Some(Player { stream: Stream::Mp2(m), .. }) if m.frames > 0 => Value::Map(vec![("codec".into(), Value::Text("mp2".into()))]),
            Some(Player { stream: Stream::Plus { format: Some(f), aac: Some(_), .. }, .. }) => {
                Value::Map(vec![("codec".into(), Value::Text(f.codec_code().into()))])
            }
            _ => Value::Null,
        };
        vec![
            ("sync".into(), Value::Bool(self.sync)),
            ("ensemble".into(), ensemble),
            ("services".into(), Value::List(services)),
            ("playing".into(), self.player.as_ref().map(|p| Value::Text(sid_hex(p.sid))).unwrap_or(Value::Null)),
            ("why".into(), self.why.clone().map(Value::Text).unwrap_or(Value::Null)),
            ("audio".into(), audio),
            ("block".into(), if self.block.is_empty() { Value::Null } else { Value::Text(self.block.clone()) }),
            ("dls".into(), self.player.as_ref().and_then(|p| p.stream.dls()).map(|t| Value::Text(t.into())).unwrap_or(Value::Null)),
            ("complete".into(), Value::Bool(self.complete())),
            ("tii".into(), super::tii::to_value(self.demod.tii.heard())),
        ]
    }

    /// One CIF's logical frame through the playing stream.
    fn play(&mut self, lf: &[u8], t: f64, out: &mut Sink) {
        let plus = match self.player.as_mut() {
            None => return,
            Some(Player { stream: Stream::Mp2(m), .. }) => {
                let before = m.errors;
                for (pcm, rate, ch) in m.push(lf) {
                    self.counts.audio_frames += 1;
                    out.push(t, Output::Audio { pcm, rate, channels: ch });
                }
                self.counts.audio_errors += m.errors - before;
                return;
            }
            Some(Player { stream: Stream::Plus { sf, .. }, .. }) => sf.push(lf),
        };
        let Some(o) = plus else { return };
        self.counts.au_errors += o.au_errors;
        self.counts.aus += o.aus.len() as u64;
        // Open the AAC decoder for this format: when the format is new or
        // changed, or when faad2 has turned up since it was missing. A
        // format faad2 refused is not retried until it changes.
        let (open, fmt) = match &self.player {
            Some(Player { stream: Stream::Plus { format, aac, why, .. }, .. }) => {
                (*format != o.format || (aac.is_none() && *why == Some("needs_codec:faad2") && self.aac_available()), o.format)
            }
            _ => return,
        };
        if open {
            let made = if self.aac_available() { Some(self.make_aac(&fmt.map(|f| f.asc()).unwrap_or_default())) } else { None };
            if let Some(Player { stream: Stream::Plus { aac, format, why, .. }, .. }) = self.player.as_mut() {
                *format = fmt;
                (*aac, *why) = match made {
                    Some(Ok(a)) => (Some(a), None),
                    Some(Err(code)) => (None, Some(code)),
                    None => (None, Some("needs_codec:faad2")),
                };
            }
        }
        // The PAD at each AU's start: the label needs no faad2.
        if let Some(Player { stream: Stream::Plus { pad: pd, .. }, .. }) = self.player.as_mut() {
            for au in &o.aus {
                if let Some((x, f)) = pad::dabplus_pad(au) {
                    pd.push(x, f);
                }
            }
        }
        if let Some(Player { stream: Stream::Plus { aac: Some(dec), .. }, .. }) = self.player.as_mut() {
            for au in &o.aus {
                match dec.decode(au) {
                    Ok((pcm, rate, ch)) => {
                        self.counts.audio_frames += 1;
                        out.push(t, Output::Audio { pcm, rate, channels: ch });
                    }
                    Err(_) => self.counts.codec_errors += 1,
                }
            }
        }
    }

    fn handle(&mut self, ev: Event, out: &mut Sink) {
        match ev {
            Event::Miss(m) => {
                self.counts.misses += 1;
                self.sync = false;
                self.why = Some(m.code().into());
                self.snr_db = None;
            }
            Event::Frame(f) => {
                self.counts.frames += 1;
                self.snr_db = Some(f.snr_db);
                let t = self.t_base + f.start / RATE;
                let mut good = 0;
                for c in 0..4 {
                    for fib in self.fic.decode(&f.fic[c * FIC_CODED_BITS..(c + 1) * FIC_CODED_BITS]) {
                        good += 1;
                        self.ens.parse_fib(&fib);
                    }
                }
                self.sync = good > 0;
                // Locked but reading nothing for most of a second: the lock
                // is wrong (or the signal gone), so look for the frame again.
                self.deaf = if self.sync { 0 } else { self.deaf + 1 };
                if self.deaf >= 8 {
                    self.deaf = 0;
                    self.demod.resync();
                }
                if !self.ens.services.is_empty() {
                    self.frames_with_services += 1;
                }
                self.update_player();
                // Each CIF through the playing sub-channel: its logical
                // frame 15 CIFs back comes out (time interleaving).
                for (c, cif) in f.cifs.iter().enumerate() {
                    let lf = self.player.as_mut().and_then(|p| p.sub.push_cif(cif));
                    if let Some(lf) = lf {
                        self.play(&lf, t + c as f64 * 0.024, out);
                    }
                }
                self.why = if !self.sync {
                    Some("fic_errors".into())
                } else if let Some(sid) = self.service {
                    match &self.player {
                        None if self.frames_with_services > 20 && !self.ens.services.contains_key(&sid) => Some("service_not_found".into()),
                        Some(Player { stream: Stream::Plus { why: Some(w), .. }, .. }) => Some((*w).into()),
                        Some(Player { stream: Stream::Plus { .. }, .. }) if !self.aac_available() => Some("needs_codec:faad2".into()),
                        _ => None,
                    }
                } else {
                    None
                };
            }
        }
    }
}

impl Decoder for Dab {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        if let Some(s) = params.text("service") {
            let sid = parse_sid(s)?;
            if sid != self.service {
                self.service = sid;
                self.player = None;
                self.why = None;
            }
        }
        if let Some(b) = params.text("block") {
            if !b.is_empty() && tables::block_centre(b).is_none() {
                return Err(DecodeError::BadInput("bad_param"));
            }
            self.block = b.trim().to_uppercase();
        }
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let Input::Iq { samples, rate, t0, index } = input else {
            return Err(DecodeError::BadInput("input_kind"));
        };
        if rate < RATE - 1.0 {
            return Err(DecodeError::BadInput("rate"));
        }
        if self.next_index != Some(index) {
            if self.next_index.is_some() {
                // A gap: the frame timing is lost; the ensemble is kept.
                self.demod.reset();
                if let Some(p) = self.player.as_mut() {
                    p.sub.reset();
                }
            }
            if (rate - self.rate).abs() > 1e-6 || self.resampler.is_none() && (rate - RATE).abs() > 1.0 {
                self.rate = rate;
                // Up to 2.56 MS/s nothing in the input can fold back into
                // the block's +-768 kHz at 2.048 MS/s (its Nyquist is at most
                // 1.28 MHz = 2.048 - 0.768), so the filter need only keep
                // the block flat enough for DQPSK: a 16-tap span, half the
                // cost of 32 at 2.4 MS/s and as clean over the sweep. Faster
                // inputs can carry the next block's edge, which a longer
                // filter must stop.
                let taps = if rate <= 2_560_000.0 { 16 } else { 48 };
                self.resampler = ((rate - RATE).abs() > 1.0).then(|| Resampler::new(rate, RATE, 0.75, taps));
            }
            // Our 2.048 MS/s sample n is input sample n x rate / RATE.
            let ours = self.demod.end() as f64;
            self.t_base = t0 - ours / RATE;
        }
        self.next_index = Some(index + samples.len() as u64);
        match self.resampler.as_mut() {
            Some(r) => {
                let mut y = Vec::with_capacity((samples.len() as f64 * RATE / rate) as usize + 64);
                r.process(samples, &mut y);
                self.demod.push(&y);
            }
            None => self.demod.push(samples),
        }
        loop {
            let mask = self.mask();
            let Some(ev) = self.demod.next(&mask) else { break };
            self.handle(ev, out);
        }
        let snap = self.snapshot();
        if self.last_meta.as_ref() != Some(&snap) {
            out.push(self.t_base + self.demod.end() as f64 / RATE, Output::Meta { fields: snap.clone() });
            self.last_meta = Some(snap);
        }
        if let Some(Player { stream: Stream::Plus { why: Some("needs_codec:faad2"), .. }, .. }) = &self.player {
            return Err(DecodeError::NeedsCodec(CodecId::Faad2));
        }
        Ok(())
    }

    fn reset(&mut self) {
        // A retune: a new block, so nothing heard before holds.
        self.demod.reset();
        self.demod.tii.clear();
        self.fic = FicDecoder::new();
        self.ens = Ensemble::default();
        self.player = None;
        self.sync = false;
        self.why = None;
        self.frames_with_services = 0;
        self.next_index = None;
    }

    fn status(&self) -> Status {
        let c = &self.counts;
        let (sf_found, rs_fixed, rs_failed) = match &self.player {
            Some(Player { stream: Stream::Plus { sf, .. }, .. }) => (sf.found, sf.rs_corrected, sf.rs_failed),
            _ => (0, 0, 0),
        };
        Status {
            sync: self.sync,
            snr_db: self.snr_db,
            offset_hz: self.demod.locked().then_some(self.demod.offset_hz as f32),
            baud: None,
            counters: vec![
                ("frames", c.frames),
                ("misses", c.misses),
                ("resyncs", self.demod.resyncs),
                ("fibsOk", self.fic.fibs_ok),
                ("fibsBad", self.fic.fibs_bad),
                ("audioFrames", c.audio_frames),
                ("audioErrors", c.audio_errors),
                ("superframes", sf_found),
                ("rsCorrected", rs_fixed),
                ("rsFailed", rs_failed),
                ("aus", c.aus),
                ("auErrors", c.au_errors),
                ("codecErrors", c.codec_errors),
            ],
        }
    }
}

/// What `survey` found in a block.
#[derive(Clone, Debug)]
pub struct Survey {
    /// The feed's last picture (as the `meta` result).
    pub meta: Fields,
    /// Seconds of signal before the FIC was first read, and before the
    /// ensemble was complete; None if never.
    pub sync_s: Option<f64>,
    pub complete_s: Option<f64>,
    /// Seconds of the buffer it used.
    pub listened_s: f64,
}

/// "Is there an ensemble here, and what is in it?", for a band scan: a
/// fresh decoder with nothing playing (only the FIC's symbols are
/// demodulated) runs on `samples` until the ensemble is complete, or until
/// `give_up_s` of it has passed with no sync, or the samples end.
pub fn survey(samples: &[Complex32], rate: f64, give_up_s: f64) -> Result<Survey, DecodeError> {
    let mut d = Dab::new();
    let block = (rate / 20.0) as usize;
    let mut last = Vec::new();
    let (mut sync_s, mut complete_s) = (None, None);
    let mut used = 0usize;
    for chunk in samples.chunks(block.max(1)) {
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: chunk, rate, t0: 0.0, index: used as u64 }, &mut sink)?;
        used += chunk.len();
        let t = used as f64 / rate;
        for (_, o) in sink.take() {
            if let Output::Meta { fields } = o {
                last = fields;
            }
        }
        if d.sync && sync_s.is_none() {
            sync_s = Some(t);
        }
        if d.complete() {
            complete_s = Some(t);
            break;
        }
        if sync_s.is_none() && t >= give_up_s {
            break;
        }
    }
    if last.is_empty() {
        last = d.snapshot();
    }
    Ok(Survey { meta: last, sync_s, complete_s, listened_s: used as f64 / rate })
}
