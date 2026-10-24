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
//! in the session's audio ring for the app to pull. The service
//! information the FIC carries (fic.rs) is in each service's map too:
//! `pty`, `language`, `userApps`, `hasSlideshow`, `announcements`,
//! `clusters`, `components`, `alsoOn` (service following: other
//! ensembles, FM with RDS), and `now`/`next` from SPI's programme guide;
//! and beside them `ensemble`'s `ecc`, `country`, `ltoMinutes` and `utc`,
//! the active `announcements`, `dlPlus` (the playing label's DL Plus
//! tags), `slide` (the latest slide's details) and `spi`.
//!
//! **Pictures** come as `Output::Frame`s with their bytes in `raw`, never
//! in the feed's text: `slide` (SlideShow, TS 101 499: from the playing
//! service's X-PAD, or its packet-mode component), `logo` (a station logo
//! from the ensemble's SPI carousel, one per service and `variant`,
//! `square` or `wide`), and `schedule` (a service's programme list from
//! SPI, no bytes). Nothing is ever fetched: the links a slide or SPI
//! carries are passed on as text.
//!
//! **SPI costs a second sub-channel's decoding**, so it runs only while a
//! service plays, only for an SPI sub-channel of SPI_MAX_KBPS or less, and
//! for at most SPI_ON_S of sample time before resting SPI_REST_S (sooner
//! once the whole carousel has come round once); its thread time is counted
//! (`spiCpuMs`). The band scan's `survey` never decodes it.
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
use super::codes;
use super::fic::{Ensemble, FicDecoder, PacketComponent, Subchannel};
use super::mot::{self, Limits, MotDecoder, MotObject};
use super::msc::SubchannelDecoder;
use super::packet::PacketReader;
use super::spi::{Spi, SpiEvent};
use super::ofdm::{Demod, Event};
use super::pad::{self, PadDecoder};
use super::tables::{self, CIF_SYMBOLS, CU_BITS, FIC_CODED_BITS, RATE, SYMBOL_BITS};
use crate::decoder::*;
use crate::dsp::Resampler;
use rustfft::num_complex::Complex32;
use std::time::Duration;

/// SPI's bounds (module docs).
const SPI_MAX_KBPS: u16 = 64;
const SPI_ON_S: f64 = 600.0;
const SPI_REST_S: f64 = 1800.0;

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

impl Stream {
    fn pad_mut(&mut self) -> &mut PadDecoder {
        match self {
            Stream::Mp2(m) => &mut m.pad,
            Stream::Plus { pad, .. } => pad,
        }
    }

    fn pad(&self) -> &PadDecoder {
        match self {
            Stream::Mp2(m) => &m.pad,
            Stream::Plus { pad, .. } => pad,
        }
    }
}

struct Player {
    sid: u32,
    sub: SubchannelDecoder,
    stream: Stream,
    /// SlideShow from the X-PAD, and the latest slide's details.
    slides: MotDecoder,
    slide: Option<Fields>,
}

/// What a data pipe's objects are for.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Purpose {
    Spi,
    /// The playing service's packet-mode SlideShow.
    Slides,
}

/// A packet-mode data component being read: its sub-channel, its address's
/// data groups, and MOT on them.
struct DataPipe {
    purpose: Purpose,
    at: PacketComponent,
    sub: SubchannelDecoder,
    reader: PacketReader,
    mot: MotDecoder,
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
    /// Packet-mode data components being read (SPI, packet SlideShow).
    pipes: Vec<DataPipe>,
    spi: Spi,
    /// Sample time SPI started listening, and until when it rests.
    spi_since: Option<f64>,
    spi_rest_until: Option<f64>,
    spi_cpu: Duration,
    /// The latest sample time (UTC), for now and next.
    now_t: f64,
    slides: u64,
    logos: u64,
}

impl Default for Dab {
    fn default() -> Self {
        Self::new()
    }
}

/// "C221" for a 16-bit SId, eight digits for a 32-bit one.
pub fn sid_hex(sid: u32) -> String {
    codes::sid_hex(sid)
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
            pipes: Vec::new(),
            spi: Spi::new(),
            spi_since: None,
            spi_rest_until: None,
            spi_cpu: Duration::ZERO,
            now_t: 0.0,
            slides: 0,
            logos: 0,
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

    /// The MSC symbols (within a CIF) the playing sub-channel and the data
    /// pipes' occupy.
    fn mask(&self) -> [bool; CIF_SYMBOLS] {
        let mut m = [false; CIF_SYMBOLS];
        let subs = self.player.iter().map(|p| &p.sub.sub).chain(self.pipes.iter().map(|d| &d.sub.sub));
        for sub in subs {
            let from = sub.start as usize * CU_BITS;
            let to = from + sub.protection.size() * CU_BITS;
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
        self.player = Some(Player { sid, sub: dec, stream, slides: MotDecoder::new(Limits::SLIDESHOW), slide: None });
    }

    /// The X-PAD application type the playing service's SlideShow starts
    /// its data groups with (FIG 0/13), 12 until it says.
    fn slideshow_xpad_app(&self, sid: u32) -> u8 {
        let s = self.ens.services.get(&sid);
        let apps = s.and_then(|s| s.apps.get(&0)).map(|v| v.as_slice()).unwrap_or(&[]);
        apps.iter()
            .find(|a| a.ty == codes::UA_SLIDESHOW)
            .and_then(|a| a.data.first())
            .map(|b| b & 0x1f)
            .filter(|&x| x >= 12)
            .unwrap_or(12)
    }

    /// Start or stop the packet-mode pipes: the playing service's packet
    /// SlideShow, and SPI when its turn (module docs) has come.
    fn update_pipes(&mut self, t: f64) {
        let mut want: Vec<(Purpose, PacketComponent)> = Vec::new();
        if let Some(p) = &self.player {
            if let Some(Some(pc)) = self.ens.app_carrier(p.sid, codes::UA_SLIDESHOW) {
                if pc.data_groups {
                    want.push((Purpose::Slides, pc));
                }
            }
        }
        let spi = self.ens.spi().filter(|(_, pc)| {
            self.ens.subchannels.get(&pc.subch).and_then(|s| s.protection.bitrate()).is_some_and(|b| b <= SPI_MAX_KBPS)
        });
        let resting = self.spi_rest_until.is_some_and(|u| t < u);
        if self.player.is_some() && spi.is_some() && !resting {
            let since = *self.spi_since.get_or_insert(t);
            let round = self.pipes.iter().any(|d| d.purpose == Purpose::Spi && d.mot.carousel_complete());
            if t - since > SPI_ON_S || round {
                self.spi_rest_until = Some(t + SPI_REST_S);
                self.spi_since = None;
            } else if let Some((_, pc)) = spi {
                want.push((Purpose::Spi, pc));
            }
        } else if !resting {
            self.spi_since = None;
        }
        self.pipes.retain(|d| want.iter().any(|w| w.0 == d.purpose && w.1 == d.at));
        for (purpose, at) in want {
            if self.pipes.iter().any(|d| d.purpose == purpose && d.at == at) {
                continue;
            }
            let Some(sub) = self.ens.subchannels.get(&at.subch).cloned() else { continue };
            let Some(dec) = SubchannelDecoder::new(sub) else { continue };
            let limits = if purpose == Purpose::Spi { Limits::SPI } else { Limits::SLIDESHOW };
            self.pipes.push(DataPipe { purpose, reader: PacketReader::new(at.address), at, sub: dec, mot: MotDecoder::new(limits) });
        }
    }

    /// A slide out, when the object is one (JPEG or PNG).
    fn slide(&mut self, sid: u32, o: &MotObject, t: f64, out: &mut Sink) {
        let h = &o.header;
        let Some(mime) = mot::image_mime(h) else { return };
        let text = |v: Option<String>| v.map(Value::Text).unwrap_or(Value::Null);
        let (w, hgt) = mot::image_size(&o.body).map(|(w, h)| (Value::Num(w as f64), Value::Num(h as f64))).unwrap_or((Value::Null, Value::Null));
        let category = h.param(0x25).filter(|p| p.len() >= 2);
        let fields: Fields = vec![
            ("sid".into(), Value::Text(sid_hex(sid))),
            ("contentType".into(), Value::Text(mime.into())),
            ("name".into(), text(h.content_name())),
            ("categoryTitle".into(), text(h.text(0x26))),
            ("categoryId".into(), category.map(|p| Value::Num(p[0] as f64)).unwrap_or(Value::Null)),
            ("slideId".into(), category.map(|p| Value::Num(p[1] as f64)).unwrap_or(Value::Null)),
            ("clickThroughUrl".into(), text(h.text(0x27))),
            ("width".into(), w),
            ("height".into(), hgt),
            ("size".into(), Value::Num(o.body.len() as f64)),
        ];
        self.slides += 1;
        if let Some(p) = self.player.as_mut().filter(|p| p.sid == sid) {
            p.slide = Some(fields.clone());
        }
        out.push(t, Output::Frame { kind: "slide", fields, raw: Some(o.body.clone()) });
    }

    /// The playing service's X-PAD slides, after its PAD has been read.
    fn pad_slides(&mut self, t: f64, out: &mut Sink) {
        let app = match &self.player {
            Some(p) => self.slideshow_xpad_app(p.sid),
            None => return,
        };
        let Some(p) = self.player.as_mut() else { return };
        let pad = p.stream.pad_mut();
        pad.mot_app = app;
        let groups = pad.take_mot();
        let mut objs = Vec::new();
        for g in groups {
            objs.extend(p.slides.push_bytes(&g, t));
        }
        let sid = p.sid;
        for o in objs {
            self.slide(sid, &o, t, out);
        }
    }

    /// One CIF through each data pipe.
    fn feed_pipes(&mut self, cif: &[super::coding::Soft], t: f64, out: &mut Sink) {
        if self.pipes.is_empty() {
            return;
        }
        let t_cpu = crate::cpu::thread_time();
        let mut done: Vec<(Purpose, MotObject, Option<std::collections::BTreeMap<u16, mot::Header>>)> = Vec::new();
        for d in &mut self.pipes {
            let Some(lf) = d.sub.push_cif(cif) else { continue };
            for g in d.reader.push_frame(&lf) {
                for o in d.mot.push_bytes(&g, t) {
                    done.push((d.purpose, o, d.mot.directory().cloned()));
                }
            }
        }
        for (purpose, o, dir) in done {
            match purpose {
                Purpose::Slides => {
                    if let Some(sid) = self.player.as_ref().map(|p| p.sid) {
                        self.slide(sid, &o, t, out);
                    }
                }
                Purpose::Spi => {
                    let services: Vec<u32> = self.ens.services.values().filter(|s| s.audio().is_some()).map(|s| s.sid).collect();
                    for ev in self.spi.push(&o, &services, dir.as_ref()) {
                        self.spi_event(ev, t, out);
                    }
                }
            }
        }
        if self.pipes.iter().any(|d| d.purpose == Purpose::Spi) {
            self.spi_cpu += crate::cpu::thread_time().saturating_sub(t_cpu);
        }
    }

    fn spi_event(&mut self, ev: SpiEvent, t: f64, out: &mut Sink) {
        match ev {
            SpiEvent::Logo { sid, variant, mime, name, width, height, bytes } => {
                self.logos += 1;
                let fields: Fields = vec![
                    ("sid".into(), Value::Text(sid_hex(sid))),
                    ("variant".into(), Value::Text(variant.into())),
                    ("contentType".into(), Value::Text(mime.into())),
                    ("name".into(), Value::Text(name)),
                    ("width".into(), Value::Num(width as f64)),
                    ("height".into(), Value::Num(height as f64)),
                    ("size".into(), Value::Num(bytes.len() as f64)),
                ];
                out.push(t, Output::Frame { kind: "logo", fields, raw: Some(bytes) });
            }
            SpiEvent::Schedule { sid } => {
                self.spi.prune(t);
                let progs = self.spi.schedules.get(&sid).cloned().unwrap_or_default();
                let fields: Fields = vec![
                    ("sid".into(), Value::Text(sid_hex(sid))),
                    ("programmes".into(), Value::List(progs.iter().map(programme_value).collect())),
                ];
                out.push(t, Output::Frame { kind: "schedule", fields, raw: None });
            }
        }
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
            && audio.iter().all(|s| s.best_label().is_some() && self.ens.subchannels.contains_key(&s.audio().unwrap().subch))
    }

    /// The feed's picture of what is heard.
    fn snapshot(&self) -> Fields {
        let text = |s: &Option<String>| s.clone().map(Value::Text).unwrap_or(Value::Null);
        let aac = self.aac_available();
        let e = &self.ens;
        let ensemble = match e.eid {
            Some(eid) => Value::Map(vec![
                ("id".into(), Value::Text(format!("{eid:04X}"))),
                ("label".into(), text(&e.ext_label.clone().or(e.label.clone()))),
                ("shortLabel".into(), text(&if e.ext_label.is_some() { e.ext_short_label.clone().or(e.short_label.clone()) } else { e.short_label.clone() })),
                ("ecc".into(), e.ecc.map(|x| Value::Text(format!("{x:02X}"))).unwrap_or(Value::Null)),
                ("country".into(), e.ecc.and_then(|x| codes::country(x, (eid >> 12) as u8)).map(|c| Value::Text(c.into())).unwrap_or(Value::Null)),
                ("ltoMinutes".into(), e.lto.map(|h| Value::Num(h as f64 * 30.0)).unwrap_or(Value::Null)),
                // To the minute: a meta every second would crowd the results.
                ("utc".into(), e.utc().map(|t| Value::Text(codes::iso_utc((t / 60.0).floor() * 60.0))).unwrap_or(Value::Null)),
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
            let (now, next) = self.spi.now_next(s.sid, self.now_t);
            let mut apps: Vec<Value> = Vec::new();
            for a in s.apps.values().flatten() {
                let code = codes::user_app(a.ty).map(String::from).unwrap_or_else(|| format!("ua_{:03x}", a.ty));
                if !apps.contains(&Value::Text(code.clone())) {
                    apps.push(Value::Text(code));
                }
            }
            services.push(Value::Map(vec![
                ("sid".into(), Value::Text(sid_hex(s.sid))),
                ("label".into(), text(&s.best_label().cloned())),
                ("shortLabel".into(), text(&s.best_short_label().cloned())),
                ("bitrate".into(), sub.and_then(|x| x.protection.bitrate()).map(|b| Value::Num(b as f64)).unwrap_or(Value::Null)),
                ("kind".into(), Value::Text(kind.into())),
                ("subchannel".into(), Value::Num(c.subch as f64)),
                ("protection".into(), sub.map(|x| Value::Text(x.protection.level_code())).unwrap_or(Value::Null)),
                ("why".into(), if needs.is_empty() { Value::Null } else { Value::Text("needs_codec:faad2".into()) }),
                ("needs".into(), Value::List(needs)),
                ("pty".into(), s.pty.map(|p| Value::Map(vec![
                    ("code".into(), Value::Num(p.code as f64)),
                    ("id".into(), codes::pty_code(e.inter_table, p.code).map(|c| Value::Text(c.into())).unwrap_or(Value::Null)),
                    ("dynamic".into(), Value::Bool(p.dynamic)),
                ])).unwrap_or(Value::Null)),
                ("language".into(), e.language_of(c).and_then(codes::language).map(|l| Value::Text(l.into())).unwrap_or(Value::Null)),
                ("userApps".into(), Value::List(apps)),
                ("hasSlideshow".into(), Value::Bool(s.has_app(codes::UA_SLIDESHOW))),
                ("announcements".into(), Value::List(codes::announcement_types(s.asu).into_iter().map(|a| Value::Text(a.into())).collect())),
                ("clusters".into(), Value::List(s.clusters.iter().map(|&c| Value::Num(c as f64)).collect())),
                ("components".into(), Value::List((1..16u8).filter_map(|i| s.component_label(i).map(|l| Value::Map(vec![
                    ("scids".into(), Value::Num(i as f64)),
                    ("label".into(), Value::Text(l.clone())),
                ]))).collect())),
                ("alsoOn".into(), also_on(e, s.sid)),
                ("now".into(), now.map(programme_value).unwrap_or(Value::Null)),
                ("next".into(), next.map(programme_value).unwrap_or(Value::Null)),
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
            ("dlPlus".into(), self.player.as_ref().and_then(|p| p.stream.pad().dl_plus.as_ref()).map(dl_plus_value).unwrap_or(Value::Null)),
            ("slide".into(), self.player.as_ref().and_then(|p| p.slide.clone()).map(Value::Map).unwrap_or(Value::Null)),
            ("announcements".into(), Value::List(e.switching.iter().map(|(cluster, sw)| Value::Map(vec![
                ("cluster".into(), Value::Num(*cluster as f64)),
                ("types".into(), Value::List(codes::announcement_types(sw.flags).into_iter().map(|a| Value::Text(a.into())).collect())),
                ("subchannel".into(), Value::Num(sw.subch as f64)),
                ("new".into(), Value::Bool(sw.new)),
                ("services".into(), Value::List(e.services.values()
                    .filter(|s| *cluster == 0xff || s.clusters.contains(cluster))
                    .filter(|s| s.audio().is_some())
                    .map(|s| Value::Text(sid_hex(s.sid))).collect())),
            ])).collect())),
            ("spi".into(), self.spi_value()),
            ("complete".into(), Value::Bool(self.complete())),
            ("tii".into(), super::tii::to_value(self.demod.tii.heard())),
        ]
    }

    /// SPI's state for the feed: where it is and whether we listen now.
    fn spi_value(&self) -> Value {
        let Some((sid, pc)) = self.ens.spi() else { return Value::Null };
        let kbps = self.ens.subchannels.get(&pc.subch).and_then(|s| s.protection.bitrate());
        let state = if kbps.is_none_or(|b| b > SPI_MAX_KBPS) {
            "too_wide"
        } else if self.pipes.iter().any(|d| d.purpose == Purpose::Spi) {
            "listening"
        } else if self.spi_rest_until.is_some_and(|u| self.now_t < u) {
            "resting"
        } else {
            "idle"
        };
        Value::Map(vec![
            ("sid".into(), Value::Text(sid_hex(sid))),
            ("subchannel".into(), Value::Num(pc.subch as f64)),
            ("state".into(), Value::Text(state.into())),
            ("logos".into(), Value::Num(self.logos as f64)),
            ("schedules".into(), Value::Num(self.spi.schedules.len() as f64)),
        ])
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
                self.now_t = t;
                self.update_pipes(t);
                // Each CIF through the playing sub-channel: its logical
                // frame 15 CIFs back comes out (time interleaving). And
                // through the data pipes, which are interleaved the same.
                for (c, cif) in f.cifs.iter().enumerate() {
                    let tc = t + c as f64 * 0.024;
                    let lf = self.player.as_mut().and_then(|p| p.sub.push_cif(cif));
                    if let Some(lf) = lf {
                        self.play(&lf, tc, out);
                        self.pad_slides(tc, out);
                    }
                    self.feed_pipes(cif, tc, out);
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
                for d in &mut self.pipes {
                    d.sub.reset();
                    d.reader.reset();
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
        self.pipes.clear();
        self.spi = Spi::new();
        self.spi_since = None;
        self.spi_rest_until = None;
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
                ("slides", self.slides),
                ("logos", self.logos),
                ("motObjects", self.player.iter().map(|p| p.slides.counts.objects).chain(self.pipes.iter().map(|d| d.mot.counts.objects)).sum()),
                ("motErrors", self.player.iter().map(|p| p.slides.counts.groups_bad).chain(self.pipes.iter().map(|d| d.mot.counts.groups_bad)).sum()),
                ("motRefused", self.player.iter().map(|p| p.slides.counts.oversize).chain(self.pipes.iter().map(|d| d.mot.counts.oversize)).sum()),
                ("packetErrors", self.pipes.iter().map(|d| d.reader.counts.crc_errors).sum()),
                ("spiCpuMs", self.spi_cpu.as_millis() as u64),
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

/// A programme for the feed.
fn programme_value(p: &super::spi::Programme) -> Value {
    Value::Map(vec![
        ("name".into(), Value::Text(p.name.clone())),
        ("start".into(), Value::Text(codes::iso_utc(p.start))),
        ("durationS".into(), Value::Num(p.duration_s as f64)),
        ("description".into(), p.description.clone().map(Value::Text).unwrap_or(Value::Null)),
    ])
}

/// DL Plus for the feed: the tags Radio's clients show by name, and every
/// tag as sent.
fn dl_plus_value(d: &super::pad::DlPlus) -> Value {
    let get = |ct: u8| d.get(ct).map(|t| Value::Text(t.into())).unwrap_or(Value::Null);
    Value::Map(vec![
        ("itemTitle".into(), get(1)),
        ("itemArtist".into(), get(4)),
        ("itemAlbum".into(), get(2)),
        ("programmeNow".into(), get(33)),
        ("programmeHost".into(), get(36)),
        ("stationNameLong".into(), get(32)),
        ("itemRunning".into(), Value::Bool(d.item_running)),
        ("itemToggle".into(), Value::Bool(d.item_toggle)),
        ("tags".into(), Value::List(d.tags.iter().map(|t| Value::Map(vec![
            ("type".into(), codes::dl_plus_type(t.content_type).map(|c| Value::Text(c.into())).unwrap_or(Value::Num(t.content_type as f64))),
            ("text".into(), Value::Text(t.text.clone())),
        ])).collect())),
    ])
}

/// Service following (EN 300 401 §8.1.15, TS 103 176): where else a
/// service is, from linkage sets, frequency information and other
/// ensembles' services. Listed only; nothing here retunes.
fn also_on(e: &Ensemble, sid: u32) -> Value {
    use std::collections::BTreeSet;
    // Other ensembles carrying it (FIG 0/24), with their frequencies.
    let mut eids: BTreeSet<u16> = e.oe_services.get(&sid).into_iter().flatten().copied().collect();
    if let Some(own) = e.eid {
        eids.remove(&own);
    }
    let ensembles: Vec<Value> = eids
        .iter()
        .map(|eid| {
            let khz: Vec<u32> = e.freqs.iter().filter(|(k, _)| k.0 == *eid && k.1 == 0).flat_map(|(_, f)| f.khz.clone()).collect();
            let block = khz.iter().find_map(|f| tables::BAND_III.iter().find(|b| b.1 == f * 1000).map(|b| b.0));
            Value::Map(vec![
                ("eid".into(), Value::Text(format!("{eid:04X}"))),
                ("khz".into(), Value::List(khz.iter().map(|&f| Value::Num(f as f64)).collect())),
                ("block".into(), block.map(|b| Value::Text(b.into())).unwrap_or(Value::Null)),
            ])
        })
        .collect();
    // FM with RDS: the PIs its linkage sets name (and its own SId, which
    // UK and German broadcasters make its PI), with FIG 0/21's frequencies.
    let mut pis: BTreeSet<u16> = BTreeSet::new();
    let mut hard = false;
    for set in e.links.values() {
        let member = set.key == Some(sid) || set.ids.iter().any(|i| i.kind == 0 && i.id == sid);
        if !member {
            continue;
        }
        hard |= set.hard;
        pis.extend(set.ids.iter().filter(|i| i.kind == 1).map(|i| i.id as u16));
    }
    if sid <= 0xffff && e.freqs.keys().any(|k| k.0 == sid as u16 && k.1 == 8) {
        pis.insert(sid as u16);
    }
    let fm: Vec<Value> = pis
        .iter()
        .map(|pi| {
            let khz: Vec<u32> = e.freqs.iter().filter(|(k, _)| k.0 == *pi && k.1 == 8).flat_map(|(_, f)| f.khz.clone()).collect();
            Value::Map(vec![
                ("pi".into(), Value::Text(format!("{pi:04X}"))),
                ("mhz".into(), Value::List(khz.iter().map(|&f| Value::Num(f as f64 / 1000.0)).collect())),
            ])
        })
        .collect();
    let linked: Vec<Value> = e
        .links
        .values()
        .filter(|set| set.key == Some(sid) || set.ids.iter().any(|i| i.kind == 0 && i.id == sid))
        .flat_map(|set| set.ids.iter().chain(set.key.map(|k| super::fic::LinkId { kind: 0, ecc: None, id: k }).as_ref()).filter(|i| i.kind == 0 && i.id != sid).map(|i| i.id).collect::<Vec<_>>())
        .collect::<BTreeSet<u32>>()
        .into_iter()
        .map(|x| Value::Text(sid_hex(x)))
        .collect();
    Value::Map(vec![
        ("ensembles".into(), Value::List(ensembles)),
        ("fm".into(), Value::List(fm)),
        ("services".into(), Value::List(linked)),
        ("hardLink".into(), Value::Bool(hard)),
    ])
}
