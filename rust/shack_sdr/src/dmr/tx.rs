//! The DMR test transmitter (RADIO-SDR-PLAN §7.1): valid bursts with
//! known IDs, colour code, LCs, embedded LC, talker alias and AMBE+2
//! payload bits, as a repeater (both slots, CACH, idle bursts), a radio
//! (MS, one slot) or a TDMA direct-mode pair, modulated as 4FSK with DMR's
//! root-raised-cosine pulse, with noise and a frequency offset.
//!
//! The voice payload is 49-bit vectors chosen by a seed (or the network's
//! silence frame), and the signal hands back every vector it sent, so the
//! decoder's FEC can be checked bit for bit without any vocoder. Receive
//! only: this models a transmitter for tests and the simulator; nothing in
//! the crate can key a radio.

use super::ambe;
use super::burst::{self, cach, put_emb, put_embedded, put_info, put_slot_type, put_sync, Burst, DataType, Emb, SyncKind, Tact};
use super::burst::{BURST_BITS, CACH_SYMBOLS, SLOT_SYMBOLS};
use super::data::{self, Header};
use super::demod::{rrc, HZ_PER_UNIT, RATE, ROLLOFF, SPS};
use super::lc::{self, alias_lcs, encode_embedded_lc, encode_full, Lc, ServiceOptions};
use crate::dsp::Resampler;
use crate::testsig::Rng;
use rustfft::num_complex::Complex32;

/// Who transmits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// A repeater: both slots, a CACH before each burst, idle bursts in a
    /// slot with nothing to send.
    Bs,
    /// One radio on a simplex channel: its slot's bursts only.
    Ms,
    /// TDMA direct mode: two radios in the two slots, syncs naming them.
    Direct,
}

/// What a voice call's 49-bit frames are.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Payload {
    /// Pseudo-random vectors from this seed.
    Random(u64),
    /// The network's silence frame throughout.
    Silence,
}

#[derive(Clone, Debug)]
pub struct VoiceCall {
    pub src: u32,
    pub dst: u32,
    pub group: bool,
    /// Voice superframes (six bursts, 360 ms each).
    pub superframes: usize,
    pub alias: Option<String>,
    pub encrypted: bool,
    pub emergency: bool,
    pub payload: Payload,
    /// Leave out the voice LC header (the receiver must enter late).
    pub no_header: bool,
    /// Leave out the terminator (the call ends by timing out).
    pub no_terminator: bool,
}

impl VoiceCall {
    pub fn new(src: u32, dst: u32, superframes: usize) -> Self {
        VoiceCall {
            src,
            dst,
            group: true,
            superframes,
            alias: None,
            encrypted: false,
            emergency: false,
            payload: Payload::Random(src as u64),
            no_header: false,
            no_terminator: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Sms {
    pub src: u32,
    pub dst: u32,
    pub group: bool,
    pub text: String,
    pub rate34: bool,
}

/// One thing a slot sends, in order.
#[derive(Clone, Debug)]
pub enum Item {
    Voice(VoiceCall),
    Sms(Sms),
    /// This many of the slot's bursts with nothing (a repeater sends idle).
    Gap(usize),
}

#[derive(Clone, Debug)]
pub struct DmrCase {
    pub mode: Mode,
    pub cc: u8,
    /// What slots 1 and 2 send. In MS mode only slot 1's is sent.
    pub slots: [Vec<Item>; 2],
    /// Slot periods (30 ms) of idle repeater, or silence, before and after.
    pub lead: usize,
    pub tail: usize,
    pub rate: f64,
    /// Carrier over the noise in the 12.5 kHz channel, dB; None: no noise.
    pub snr_db: Option<f64>,
    pub offset_hz: f64,
    pub seed: u64,
}

impl Default for DmrCase {
    fn default() -> Self {
        DmrCase {
            mode: Mode::Bs,
            cc: 1,
            slots: [Vec::new(), Vec::new()],
            lead: 8,
            tail: 8,
            rate: RATE,
            snr_db: None,
            offset_hz: 0.0,
            seed: 1,
        }
    }
}

/// What was sent.
pub struct Signal {
    pub iq: Vec<Complex32>,
    /// Each slot's 49-bit voice vectors, in order.
    pub voice: [Vec<[u8; ambe::DATA_BITS]>; 2],
    /// Slot periods in all.
    pub periods: usize,
}

fn sync_kind(mode: Mode, slot: u8) -> SyncKind {
    match mode {
        Mode::Bs => SyncKind::Bs,
        Mode::Ms => SyncKind::Ms,
        Mode::Direct => SyncKind::Direct(slot),
    }
}

fn data_burst(mode: Mode, slot: u8, cc: u8, dt: DataType, info: &[u8; 196]) -> Burst {
    let mut b = [0u8; BURST_BITS];
    put_info(&mut b, info);
    put_slot_type(&mut b, cc, dt as u8);
    put_sync(&mut b, burst::sync_pattern(sync_kind(mode, slot), false));
    b
}

/// A repeater's idle burst.
pub fn idle_burst(cc: u8) -> Burst {
    data_burst(Mode::Bs, 1, cc, DataType::Idle, &data::encode_block12(&[0u8; 12]))
}

fn frames_for(p: Payload, n: usize) -> Vec<[u8; ambe::DATA_BITS]> {
    match p {
        Payload::Silence => {
            let d = ambe::decode(&ambe::bits_of(&ambe::SILENCE)).data;
            vec![d; n]
        }
        Payload::Random(seed) => {
            let mut rng = Rng::new(seed ^ 0xD3A_5EED);
            (0..n).map(|_| std::array::from_fn(|_| (rng.next_u64() >> 63) as u8)).collect()
        }
    }
}

/// A voice call's bursts in one slot, and the vectors in them.
pub fn voice_bursts(c: &VoiceCall, mode: Mode, slot: u8, cc: u8) -> (Vec<Burst>, Vec<[u8; ambe::DATA_BITS]>) {
    let opts = ServiceOptions { privacy: c.encrypted, emergency: c.emergency, ..Default::default() };
    let lc = Lc::voice(c.group, c.src, c.dst, opts);
    let mut out = Vec::new();
    if !c.no_header {
        for _ in 0..2 {
            out.push(data_burst(mode, slot, cc, DataType::VoiceLcHeader, &encode_full(&lc, lc::MASK_HEADER)));
        }
        if c.encrypted {
            // A PI header: algorithm and key, which we never use.
            let mut pi = [0u8; 12];
            pi[0] = 0x21;
            pi[1] = 0x01;
            data::put_crc(&mut pi, data::MASK_PI_HEADER);
            out.push(data_burst(mode, slot, cc, DataType::PiHeader, &data::encode_block12(&pi)));
        }
    }
    // Superframes take turns: the call's LC, then each alias LC, then the
    // LC again, as radios do.
    let mut embedded: Vec<Lc> = vec![lc];
    if let Some(a) = &c.alias {
        embedded.extend(alias_lcs(a));
    }
    let frames = frames_for(c.payload, c.superframes * 18);
    let vsync = burst::sync_pattern(sync_kind(mode, slot), true);
    for sf in 0..c.superframes {
        let frags = encode_embedded_lc(&embedded[sf % embedded.len()]);
        for k in 0..6 {
            let mut b = [0u8; BURST_BITS];
            let f: [[u8; ambe::FRAME_BITS]; 3] = std::array::from_fn(|j| ambe::encode(&frames[sf * 18 + k * 3 + j]));
            ambe::put_frames(&mut b, &f);
            if k == 0 {
                put_sync(&mut b, vsync);
            } else {
                let (bits, lcss) = if k <= 4 { frags[k - 1] } else { ([0u8; 32], 0) };
                put_emb(&mut b, Emb { cc, pi: c.encrypted, lcss });
                put_embedded(&mut b, &bits);
            }
            out.push(b);
        }
    }
    if !c.no_terminator {
        out.push(data_burst(mode, slot, cc, DataType::TerminatorLc, &encode_full(&lc, lc::MASK_TERMINATOR)));
    }
    (out, frames)
}

/// An SMS's bursts: a data header and its blocks.
pub fn sms_bursts(s: &Sms, mode: Mode, slot: u8, cc: u8) -> Vec<Burst> {
    let dg = data::sms_datagram([12, 0, 0, (s.src & 0xFF) as u8], [12, 0, 0, (s.dst & 0xFF) as u8], &s.text);
    let h = Header { group: s.group, dpf: data::DPF_UNCONFIRMED, sap: data::SAP_IP, pad: 0, dst: s.dst, src: s.src, blocks: 0 };
    let (hb, blocks) = data::build_message(h, &dg, s.rate34);
    let mut out = vec![data_burst(mode, slot, cc, DataType::DataHeader, &data::encode_block12(&hb))];
    for blk in blocks {
        let (dt, info) = if s.rate34 {
            (DataType::Rate34Data, data::encode_rate34(&blk.try_into().expect("18")))
        } else {
            (DataType::Rate12Data, data::encode_block12(&blk.try_into().expect("12")))
        };
        out.push(data_burst(mode, slot, cc, dt, &info));
    }
    out
}

/// Every slot period's symbols (the CACH's 12 then the burst's 132), None
/// where the transmitter is off; and the vectors sent.
pub fn periods(case: &DmrCase) -> (Vec<Option<Vec<f32>>>, [Vec<[u8; ambe::DATA_BITS]>; 2]) {
    let mut per_slot: [Vec<Option<Burst>>; 2] = [Vec::new(), Vec::new()];
    let mut voice: [Vec<[u8; ambe::DATA_BITS]>; 2] = [Vec::new(), Vec::new()];
    for s in 0..2 {
        if case.mode == Mode::Ms && s == 1 {
            continue;
        }
        for item in &case.slots[s] {
            match item {
                Item::Voice(c) => {
                    let (b, f) = voice_bursts(c, case.mode, s as u8 + 1, case.cc);
                    per_slot[s].extend(b.into_iter().map(Some));
                    voice[s].extend(f);
                }
                Item::Sms(m) => per_slot[s].extend(sms_bursts(m, case.mode, s as u8 + 1, case.cc).into_iter().map(Some)),
                Item::Gap(n) => per_slot[s].extend(std::iter::repeat_n(None, *n)),
            }
        }
    }
    let body = 2 * per_slot[0].len().max(per_slot[1].len());
    let total = case.lead + body + case.tail;
    let mut out = Vec::with_capacity(total);
    for p in 0..total {
        // Period 0 is slot 1; the lead is an even number of periods so the
        // body starts on slot 1.
        let slot = (p % 2) as u8 + 1;
        let k = p.checked_sub(case.lead + case.lead % 2).map(|q| q / 2);
        let b = k.and_then(|k| per_slot[slot as usize - 1].get(k).copied().flatten());
        let burst = match (b, case.mode) {
            (Some(b), _) => Some(b),
            (None, Mode::Bs) => Some(idle_burst(case.cc)),
            (None, _) => None,
        };
        let Some(burst) = burst else {
            out.push(None);
            continue;
        };
        let mut sym = Vec::with_capacity(SLOT_SYMBOLS);
        if case.mode == Mode::Bs {
            let c = cach(Tact { at: false, slot, lcss: 0 });
            sym.extend(c.chunks(2).map(|d| burst::dibit_to_symbol((d[0] << 1) | d[1])));
        } else {
            // A radio's guard time: no carrier (the symbols are not sent).
            sym.extend(std::iter::repeat_n(f32::NAN, CACH_SYMBOLS));
        }
        sym.extend(burst.chunks(2).map(|d| burst::dibit_to_symbol((d[0] << 1) | d[1])));
        out.push(Some(sym));
    }
    (out, voice)
}

/// The case as IQ at `case.rate`.
pub fn dmr_iq(case: &DmrCase) -> Signal {
    let (per, voice) = periods(case);
    let n = per.len() * SLOT_SYMBOLS * SPS;
    // Symbol impulses and the carrier's on/off, at 48 kS/s.
    let mut imp = vec![0f32; n];
    let mut on = vec![false; n];
    for (p, s) in per.iter().enumerate() {
        let Some(s) = s else { continue };
        for (k, &v) in s.iter().enumerate() {
            let i = (p * SLOT_SYMBOLS + k) * SPS;
            if v.is_finite() {
                imp[i] = v * SPS as f32;
                on[i..i + SPS].iter_mut().for_each(|o| *o = true);
            }
        }
    }
    let h = rrc(SPS, 8, ROLLOFF);
    let half = h.len() / 2;
    let mut freq = vec![0f32; n];
    for (i, &x) in imp.iter().enumerate() {
        if x == 0.0 {
            continue;
        }
        for (j, &t) in h.iter().enumerate() {
            let k = i + j;
            if k >= half && k - half < n {
                freq[k - half] += x * t;
            }
        }
    }
    let mut rng = Rng::new(case.seed);
    let mut phase = 0f64;
    let mut iq48 = Vec::with_capacity(n);
    for i in 0..n {
        let f = case.offset_hz + freq[i] as f64 * HZ_PER_UNIT;
        phase += 2.0 * std::f64::consts::PI * f / RATE;
        if phase > std::f64::consts::PI {
            phase -= 2.0 * std::f64::consts::PI;
        }
        let a = if on[i] { 0.5f32 } else { 0.0 };
        iq48.push(Complex32::new(a * phase.cos() as f32, a * phase.sin() as f32));
    }
    let mut iq = if (case.rate - RATE).abs() > 1e-6 {
        let mut r = Resampler::new(RATE, case.rate, 0.5, 24);
        let mut o = Vec::new();
        r.process(&iq48, &mut o);
        o
    } else {
        iq48
    };
    if let Some(snr) = case.snr_db {
        // Carrier power 0.25; the noise in 12.5 kHz is snr below it, so its
        // total over the sampled band is that times rate / 12.5 kHz.
        let sigma2 = 0.25 / 10f64.powf(snr / 10.0) * case.rate / 12_500.0;
        let s = sigma2.sqrt() as f32;
        for z in iq.iter_mut() {
            let g = rng.cgauss();
            *z += Complex32::new(s * g.re as f32, s * g.im as f32);
        }
    }
    Signal { iq, voice, periods: per.len() }
}

/// What a simulator's repeater plays, over and over (about 7 s): on slot 1
/// a talkgroup call with a talker alias, then a short message; on slot 2,
/// overlapping it, a private call, then an encrypted one. The IDs are under
/// country code 999, which no country has, so they name nobody.
pub fn showcase() -> [Vec<Item>; 2] {
    let mut tg = VoiceCall::new(9_990_001, 99, 8);
    tg.alias = Some("MX0TST Test".into());
    let mut private = VoiceCall::new(9_990_002, 9_990_001, 4);
    private.group = false;
    private.alias = Some("MX0ABC".into());
    let mut secret = VoiceCall::new(9_990_003, 98, 3);
    secret.encrypted = true;
    let sms = Sms { src: 9_990_002, dst: 9_990_001, group: false, text: "Hello from the DMR simulator".into(), rate34: false };
    [
        vec![Item::Voice(tg), Item::Gap(12), Item::Sms(sms), Item::Gap(30)],
        vec![Item::Gap(20), Item::Voice(private), Item::Gap(12), Item::Voice(secret), Item::Gap(8)],
    ]
}
