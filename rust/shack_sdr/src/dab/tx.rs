//! A DAB transmitter model, for tests (RADIO-SDR-PLAN §7.1: our own
//! generators, so nothing needs a transmitter): an ensemble's FIC, an old
//! DAB service (MP2 from our encoder, UEP or EEP) and a DAB+ service
//! (superframes with RS and the Fire code; the AUs are marked test bytes,
//! not audio, since AAC is only ever the user's own library and we neither
//! encode nor decode it), through the whole chain of EN 300 401 to mode I
//! OFDM at 2.048 MS/s; then a channel: noise, frequency offset, multipath
//! (static or fading), a sample clock error, and another sample rate.
//! Receive-only code has no use for it beyond tests: nothing transmits.
//!
//! The data side too: MSC data groups and MOT objects in header mode
//! (SlideShow) and directory mode (SPI's carousel), carried in a
//! service's X-PAD or in packets on a data sub-channel, the binary SPI
//! files (TS 102 371) and any FIG a test wants to add (`TxEnsemble.figs`).

use super::coding;
use super::packet;
use super::pad::{scf_crc_len, PadEncoder};
use super::rs;
use super::tii;
use super::tables::{self, Protection, CIFS, CIF_BITS, CU_BITS, K, SYMBOL_BITS, TU, GUARD, TNULL};
use crate::dsp::Resampler;
use crate::mp2::encode::Encoder as Mp2Encoder;
use crate::testsig::Rng;
use rustfft::num_complex::{Complex32, Complex64};
use rustfft::FftPlanner;
use std::collections::VecDeque;

/// What a service carries.
#[derive(Clone, Debug, PartialEq)]
pub enum TxAudio {
    /// Old DAB: MP2 of a tone pair (left `tone_hz`, right 1.5 x it) at
    /// `bitrate` kbit/s, 48 or 24 kHz.
    Mp2 { bitrate: u16, rate: u32, stereo: bool, tone_hz: f64 },
    /// DAB+: superframes of `bitrate` kbit/s with these audio parameters;
    /// the AUs are test bytes (`dabplus_au`).
    DabPlus { bitrate: u16, dac48: bool, sbr: bool, stereo: bool, ps: bool },
}

#[derive(Clone, Debug, PartialEq)]
pub struct TxService {
    pub sid: u16,
    pub label: String,
    pub subch: u8,
    pub start: u16,
    pub protection: Protection,
    pub audio: TxAudio,
    /// Dynamic labels: (from which audio frame or AU on, text), in order.
    /// Old DAB sends short X-PAD, DAB+ variable size.
    pub dls: Vec<(u64, String)>,
    /// DL Plus tags sent with every label: (content type, first
    /// character, length).
    pub dl_plus: Vec<(u8, u8, u8)>,
    /// MSC data groups for its X-PAD (SlideShow's MOT), sent in turn and
    /// then again (a carousel); with any, the X-PAD is variable size.
    pub xpad_groups: Vec<Vec<u8>>,
}

/// A packet-mode data service: its data groups on `address`, sent in turn
/// and then again (a carousel), in packets of `packet` bytes, padding
/// filling the rest of each logical frame.
#[derive(Clone, Debug, PartialEq)]
pub struct TxData {
    /// A data service's 32-bit SId.
    pub sid: u32,
    pub label: String,
    pub subch: u8,
    pub start: u16,
    pub protection: Protection,
    pub scid: u16,
    pub address: u16,
    /// The user application FIG 0/13 signals (SPI is 0x007).
    pub user_app: u16,
    pub packet: usize,
    pub groups: Vec<Vec<u8>>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TxEnsemble {
    pub eid: u16,
    pub label: String,
    pub services: Vec<TxService>,
    pub data: Vec<TxData>,
    /// More FIGs (each whole, its header first), sent in turn in the FIBs
    /// the model leaves free: about 30 a second.
    pub figs: Vec<Vec<u8>>,
}

impl TxEnsemble {
    /// The test ensemble: one old DAB service at 128 kbit/s UEP level 3
    /// and one DAB+ service at 64 kbit/s EEP 3-A.
    pub fn standard() -> Self {
        TxEnsemble {
            eid: 0xc1ce,
            label: "Shack Test Mux".into(),
            services: vec![
                TxService {
                    sid: 0xc221,
                    label: "Shack MP2 Tone".into(),
                    subch: 1,
                    start: 0,
                    protection: Protection::Uep { index: 35 },
                    audio: TxAudio::Mp2 { bitrate: 128, rate: 48_000, stereo: true, tone_hz: 1000.0 },
                    dls: vec![(0, "Shack MP2 Tone - now: 1 kHz left, 1.5 kHz right".into()), (100, "Second label for C221".into())],
                    dl_plus: vec![],
                    xpad_groups: vec![],
                },
                TxService {
                    sid: 0xc3a7,
                    label: "Shack DAB+ Data".into(),
                    subch: 2,
                    start: 96,
                    protection: Protection::Eep { option: 0, level: 3, size: 48 },
                    audio: TxAudio::DabPlus { bitrate: 64, dac48: true, sbr: true, stereo: true, ps: false },
                    dls: vec![(0, "DAB+ test AUs from the Shack transmitter model".into())],
                    dl_plus: vec![],
                    xpad_groups: vec![],
                },
            ],
            data: vec![],
            figs: vec![],
        }
    }

    /// The standard ensemble with a 16 kbit/s packet-mode data service after
    /// the DAB+ one (sub-channel 3, EEP 3-A), carrying `groups` on address
    /// 1 and signalled as user application `user_app`.
    pub fn with_data(user_app: u16, groups: Vec<Vec<u8>>) -> Self {
        let mut e = Self::standard();
        e.data.push(TxData {
            sid: 0xe1c0_0001,
            label: "Shack SPI".into(),
            subch: 3,
            start: 144,
            protection: Protection::Eep { option: 0, level: 3, size: 12 },
            scid: 0x123,
            address: 1,
            user_app,
            packet: 24,
            groups,
        });
        e
    }
}

/// AU `n` of DAB+ superframe `m` for a service: test bytes a fake AAC
/// decoder can check, starting with (m, n) so a lost or reordered AU shows.
pub fn dabplus_au(m: u64, n: usize, len: usize) -> Vec<u8> {
    (0..len)
        .map(|i| match i {
            0 => (m >> 8) as u8,
            1 => m as u8,
            2 => n as u8,
            _ => (i as u64 * 31 + m * 7 + n as u64 * 13) as u8,
        })
        .collect()
}

/// DAB+'s audio super frame header values (TS 102 563 Table 2).
pub fn dabplus_aus(dac48: bool, sbr: bool) -> (usize, usize) {
    match (dac48, sbr) {
        (false, true) => (2, 5),
        (true, true) => (3, 6),
        (false, false) => (4, 8),
        (true, false) => (6, 11),
    }
}

/// The data_stream_element that carries an AU's PAD (TS 102 563 §5.4.1):
/// element ID 4, tag 0, no alignment, then its length and the bytes.
fn dse(pad: &[u8]) -> Vec<u8> {
    let mut v = vec![4 << 5];
    if pad.len() >= 255 {
        v.extend([255, (pad.len() - 255) as u8]);
    } else {
        v.push(pad.len() as u8);
    }
    v.extend_from_slice(pad);
    v
}

/// One superframe (TS 102 563 §5.2, §6): 120 x s bytes, s = bitrate / 8.
/// Each AU starts with its PAD when `pad` gives some, then test bytes.
pub fn dabplus_superframe(m: u64, bitrate: u16, dac48: bool, sbr: bool, stereo: bool, ps: bool) -> Vec<u8> {
    dabplus_superframe_pad(m, bitrate, dac48, sbr, stereo, ps, &mut |_| None)
}

/// As `dabplus_superframe`, with `pad(n)` giving AU n's PAD (X-PAD as sent, F-PAD).
#[allow(clippy::too_many_arguments)]
pub fn dabplus_superframe_pad(
    m: u64,
    bitrate: u16,
    dac48: bool,
    sbr: bool,
    stereo: bool,
    ps: bool,
    pad: &mut dyn FnMut(usize) -> Option<(Vec<u8>, [u8; 2])>,
) -> Vec<u8> {
    let s = bitrate as usize / 8;
    let size = 110 * s;
    let (num, start0) = dabplus_aus(dac48, sbr);
    let mut a = vec![0u8; size];
    a[2] = (dac48 as u8) << 6 | (sbr as u8) << 5 | (stereo as u8) << 4 | (ps as u8) << 3;
    // Equal AUs, the remainder in the last; each AU's 2 bytes of CRC after it.
    let each = (size - start0) / num;
    let mut starts = vec![start0];
    for n in 1..num {
        starts.push(start0 + n * each);
    }
    let mut bits = coding::unpack(&a[..start0]);
    for (n, st) in starts.iter().enumerate().skip(1) {
        for k in 0..12 {
            bits[24 + (n - 1) * 12 + k] = (*st >> (11 - k) & 1) as u8;
        }
    }
    a[..start0].copy_from_slice(&coding::pack(&bits)[..start0]);
    for n in 0..num {
        let end = if n + 1 < num { starts[n + 1] } else { size };
        let mut au = match pad(n) {
            Some((x, f)) => dse(&[x, f.to_vec()].concat()),
            None => Vec::new(),
        };
        let rest = end - starts[n] - 2 - au.len();
        au.extend(dabplus_au(m, n, rest));
        a[starts[n]..end - 2].copy_from_slice(&au);
        let c = coding::crc16_ccitt(&au);
        a[end - 2..end].copy_from_slice(&c.to_be_bytes());
    }
    let f = coding::firecode(&a[2..11]);
    a[0..2].copy_from_slice(&f.to_be_bytes());
    // RS over the virtual interleaver's rows, parity after the data.
    let mut out = a.clone();
    out.resize(120 * s, 0);
    for i in 0..s {
        let row: Vec<u8> = (0..110).map(|j| a[i + j * s]).collect();
        let p = rs::parity(&row);
        for (r, v) in p.iter().enumerate() {
            out[110 * s + r * s + i] = *v;
        }
    }
    out
}

/// A data service's packets, as they come.
struct DataSource {
    groups: Vec<Vec<u8>>,
    next: usize,
    address: u16,
    packet: usize,
    ci: u8,
    queue: VecDeque<Vec<u8>>,
}

impl DataSource {
    fn frame(&mut self, bytes: usize) -> Vec<u8> {
        let mut lf = Vec::with_capacity(bytes);
        while lf.len() + self.packet <= bytes {
            if self.queue.is_empty() && !self.groups.is_empty() {
                let g = &self.groups[self.next % self.groups.len()];
                self.next += 1;
                self.queue.extend(packet::packetise(g, self.address, self.packet, &mut self.ci));
            }
            match self.queue.pop_front() {
                Some(p) => lf.extend(p),
                None => lf.extend(packet::padding_packet(self.packet)),
            }
        }
        lf.resize(bytes, 0);
        lf
    }
}

/// A service's logical frames, as they come.
struct Source {
    data: Option<DataSource>,
    xpad_groups: Vec<Vec<u8>>,
    audio: TxAudio,
    mp2: Option<Mp2Encoder>,
    pad: PadEncoder,
    dls: Vec<(u64, String)>,
    dl_plus: Vec<(u8, u8, u8)>,
    /// Audio frames (MP2) or AUs (DAB+) made so far.
    made: u64,
    pending: VecDeque<Vec<u8>>,
    frame_bytes: usize,
    t: u64,
    superframes: u64,
}

impl Source {
    fn next(&mut self) -> Vec<u8> {
        if let Some(d) = self.data.as_mut() {
            return d.frame(self.frame_bytes);
        }
        // A carousel: queue the X-PAD's data groups again once all are sent.
        if !self.xpad_groups.is_empty() && self.pad.groups_queued() == 0 {
            self.pad.queue_groups(self.xpad_groups.clone());
        }
        if self.pending.is_empty() {
            match self.audio.clone() {
                TxAudio::Mp2 { rate, stereo, tone_hz, .. } => {
                    let enc = self.mp2.as_mut().unwrap();
                    let nch = if stereo { 2 } else { 1 };
                    let pcm: Vec<f32> = (0..1152u64)
                        .flat_map(|i| {
                            let t = (self.t + i) as f64 / rate as f64;
                            let l = 0.5 * (2.0 * std::f64::consts::PI * tone_hz * t).sin();
                            let r = 0.5 * (2.0 * std::f64::consts::PI * 1.5 * tone_hz * t).sin();
                            [l as f32, r as f32].into_iter().take(nch)
                        })
                        .collect();
                    self.t += 1152;
                    let mut f = enc.frame(&pcm);
                    // PAD at the frame's end: X-PAD, the scale-factor CRC
                    // (left zero: nothing here checks it), the F-PAD.
                    if let Some((_, text)) = self.dls.iter().rev().find(|d| d.0 <= self.made) {
                        if self.pad.set_label(text) && !self.dl_plus.is_empty() {
                            self.pad.set_dl_plus(&self.dl_plus, true, true);
                        }
                    }
                    let (x, fp) = self.pad.frame();
                    let h = enc.header;
                    let scf = scf_crc_len(h.lsf, h.bitrate, h.mode == 3);
                    let n = f.len();
                    f[n - 2 - scf - x.len()..n - 2 - scf].copy_from_slice(&x);
                    f[n - 2..].copy_from_slice(&fp);
                    self.made += 1;
                    for c in f.chunks(self.frame_bytes) {
                        self.pending.push_back(c.to_vec());
                    }
                }
                TxAudio::DabPlus { bitrate, dac48, sbr, stereo, ps } => {
                    let (pad, dls, made, tags) = (&mut self.pad, &self.dls, &mut self.made, &self.dl_plus);
                    let sf = dabplus_superframe_pad(self.superframes, bitrate, dac48, sbr, stereo, ps, &mut |_| {
                        let text = dls.iter().rev().find(|d| d.0 <= *made).map(|d| d.1.clone())?;
                        if pad.set_label(&text) && !tags.is_empty() {
                            pad.set_dl_plus(tags, true, true);
                        }
                        *made += 1;
                        Some(pad.frame())
                    });
                    self.superframes += 1;
                    for c in sf.chunks(self.frame_bytes) {
                        self.pending.push_back(c.to_vec());
                    }
                }
            }
        }
        self.pending.pop_front().unwrap()
    }
}

struct Sub {
    start: usize,
    map: Vec<usize>,
    prbs: Vec<u8>,
    /// Codewords of the last 16 logical frames, newest last.
    history: VecDeque<Vec<u8>>,
    source: Source,
}

/// The transmitter: call `frame()` for each 96 ms transmission frame.
pub struct Transmitter {
    pub ensemble: TxEnsemble,
    subs: Vec<Sub>,
    cif: u64,
    /// FIG 0/1 and 0/2, sent every frame; and the labels (FIG 1/0, 1/1),
    /// each once a second, the repetition rate §8.1.13-14 asks for, so a
    /// receiver's time to the full service list is measured as on air.
    mci: Vec<Vec<u8>>,
    labels: Vec<Vec<u8>>,
    /// The data services' FIGs and the ensemble's extra ones, in turn in
    /// the FIBs of CIFs 1-3 of each frame, and which is next.
    extra: Vec<Vec<u8>>,
    next_extra: usize,
    interleave: Vec<i32>,
    prev: Vec<Complex64>,
    fft: std::sync::Arc<dyn rustfft::Fft<f64>>,
    /// This transmitter's TII (main, sub), sent in the null symbol of the
    /// frames that hold CIFs 0-3 mod 8; None sends a silent null.
    pub tii: Option<(u8, u8)>,
}

/// The FIG header and its data.
pub fn fig(ty: u8, data: Vec<u8>) -> Vec<u8> {
    let mut v = vec![ty << 5 | data.len() as u8];
    v.extend(data);
    v
}

fn label16(s: &str) -> ([u8; 16], u16) {
    let mut b = [b' '; 16];
    for (i, c) in s.bytes().take(16).enumerate() {
        b[i] = c;
    }
    // The short label: the first eight characters that are not spaces.
    let mut flags = 0u16;
    let mut n = 0;
    for (i, c) in s.bytes().take(16).enumerate() {
        if c != b' ' && n < 8 {
            flags |= 1 << (15 - i);
            n += 1;
        }
    }
    (b, flags)
}

impl Transmitter {
    pub fn new(ensemble: TxEnsemble) -> Self {
        let mut subs = Vec::new();
        // FIG 0/1: every sub-channel.
        let mut d = vec![0x01u8];
        let subchannels = ensemble.services.iter().map(|s| (s.subch, s.start, s.protection)).chain(ensemble.data.iter().map(|s| (s.subch, s.start, s.protection)));
        for (subch, start, protection) in subchannels {
            d.push(subch << 2 | (start >> 8) as u8);
            d.push(start as u8);
            match protection {
                Protection::Uep { index } => d.push(index & 0x3f),
                Protection::Eep { option, level, size } => {
                    d.push(0x80 | option << 4 | (level - 1) << 2 | (size >> 8) as u8);
                    d.push(size as u8);
                }
            }
        }
        let mut mci = vec![fig(0, d)];
        // FIG 0/2: every service, one audio component each.
        let mut d = vec![0x02u8];
        for s in &ensemble.services {
            d.extend(s.sid.to_be_bytes());
            d.push(1);
            let ascty = if matches!(s.audio, TxAudio::DabPlus { .. }) { 63 } else { 0 };
            d.push(ascty);
            d.push(s.subch << 2 | 2);
        }
        mci.push(fig(0, d));
        // The data services: FIG 0/2 (P/D 1: 32-bit SIds; one packet-mode
        // component by its SCId), 0/3 (where it is), 0/13 (what it is),
        // and their labels (1/5), sent with the extra FIGs.
        let mut extra = Vec::new();
        for s in &ensemble.data {
            let mut d = vec![0x22u8];
            d.extend(s.sid.to_be_bytes());
            d.push(1);
            d.extend((0xc000 | s.scid << 2 | 2).to_be_bytes());
            extra.push(fig(0, d));
            let mut d = vec![0x03u8];
            d.extend((s.scid << 4).to_be_bytes());
            d.push(60); // DG flag 0, DSCTy 60: MOT
            d.extend(((s.subch as u16) << 10 | s.address & 0x3ff).to_be_bytes());
            extra.push(fig(0, d));
            let mut d = vec![0x2du8];
            d.extend(s.sid.to_be_bytes());
            d.push(0x01); // SCIdS 0, one application
            d.extend((s.user_app << 5 | 1).to_be_bytes());
            d.push(0x01); // SPI: the basic profile
            extra.push(fig(0, d));
            let (lb, fl) = label16(&s.label);
            let mut d = vec![0x05u8];
            d.extend(s.sid.to_be_bytes());
            d.extend(lb);
            d.extend(fl.to_be_bytes());
            extra.push(fig(1, d));
        }
        extra.extend(ensemble.figs.iter().cloned());
        let mut labels = Vec::new();
        // FIG 1/0 and 1/1: labels, EBU Latin.
        let (lb, fl) = label16(&ensemble.label);
        let mut d = vec![0x00u8];
        d.extend(ensemble.eid.to_be_bytes());
        d.extend(lb);
        d.extend(fl.to_be_bytes());
        labels.push(fig(1, d));
        for s in &ensemble.services {
            let (lb, fl) = label16(&s.label);
            let mut d = vec![0x01u8];
            d.extend(s.sid.to_be_bytes());
            d.extend(lb);
            d.extend(fl.to_be_bytes());
            labels.push(fig(1, d));
        }
        for s in &ensemble.services {
            let p = s.protection.profile().expect("a valid profile");
            let frame_bytes = p.info_bits() / 8;
            let short = matches!(s.audio, TxAudio::Mp2 { .. }) && s.xpad_groups.is_empty();
            let pad = if s.xpad_groups.is_empty() { PadEncoder::new(short) } else { PadEncoder::with_mot(48) };
            let mp2 = match s.audio {
                TxAudio::Mp2 { bitrate, rate, stereo, .. } => {
                    let mut e = Mp2Encoder::new(bitrate, rate, if stereo { 0 } else { 3 }, 0);
                    // Room at the end for the X-PAD, the scale-factor CRC and the F-PAD.
                    e.reserve = pad.xpad_len() + scf_crc_len(e.header.lsf, bitrate, !stereo) + 2;
                    Some(e)
                }
                _ => None,
            };
            subs.push(Sub {
                start: s.start as usize * CU_BITS,
                map: coding::puncture_map(&p),
                prbs: coding::prbs(p.info_bits()),
                history: VecDeque::new(),
                source: Source {
                    data: None,
                    xpad_groups: s.xpad_groups.clone(),
                    audio: s.audio.clone(),
                    mp2,
                    pad,
                    dls: s.dls.clone(),
                    dl_plus: s.dl_plus.clone(),
                    made: 0,
                    pending: VecDeque::new(),
                    frame_bytes,
                    t: 0,
                    superframes: 0,
                },
            });
        }
        for s in &ensemble.data {
            let p = s.protection.profile().expect("a valid profile");
            subs.push(Sub {
                start: s.start as usize * CU_BITS,
                map: coding::puncture_map(&p),
                prbs: coding::prbs(p.info_bits()),
                history: VecDeque::new(),
                source: Source {
                    data: Some(DataSource { groups: s.groups.clone(), next: 0, address: s.address, packet: s.packet, ci: 0, queue: VecDeque::new() }),
                    xpad_groups: vec![],
                    audio: TxAudio::DabPlus { bitrate: 8, dac48: false, sbr: false, stereo: false, ps: false },
                    mp2: None,
                    pad: PadEncoder::new(false),
                    dls: vec![],
                    dl_plus: vec![],
                    made: 0,
                    pending: VecDeque::new(),
                    frame_bytes: p.info_bits() / 8,
                    t: 0,
                    superframes: 0,
                },
            });
        }
        Transmitter {
            ensemble,
            subs,
            cif: 0,
            mci,
            labels,
            extra,
            next_extra: 0,
            interleave: tables::frequency_interleave(),
            prev: Vec::new(),
            fft: FftPlanner::new().plan_fft_inverse(TU),
            tii: None,
        }
    }

    /// Three FIBs for CIF `cif` (FIG 0/0 first in the first FIB of the
    /// frame's first CIF, §6.4.1), as 768 bits.
    fn fic_block(&mut self, cif: u64) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(96);
        for f in 0..3 {
            let mut fib: Vec<u8> = Vec::with_capacity(32);
            if f == 0 && cif % 4 == 0 {
                let c = (cif % 5000) as u16;
                let mut d = vec![0x00u8];
                d.extend(self.ensemble.eid.to_be_bytes());
                d.push((c / 250) as u8 & 0x1f);
                d.push((c % 250) as u8);
                fib.extend(fig(0, d));
            }
            // The frame's first FIB of each CIF carries the MCI (FIG 0/1,
            // 0/2), the second a label when one is due: label k in frame
            // k x 3 of every ten (each about once a second).
            let frame = cif / 4;
            let figs: Vec<Vec<u8>> = match (f, cif % 4) {
                (1, 0) => self.mci.clone(),
                (2, 0) => {
                    let k = (frame % 10) as usize;
                    if k % 3 == 0 { self.labels.get(k / 3).cloned().into_iter().collect() } else { vec![] }
                }
                (_, 1..=3) if !self.extra.is_empty() => {
                    // As many of the extra FIGs, in turn, as fit.
                    let mut v = Vec::new();
                    let mut room = 30;
                    for _ in 0..self.extra.len() {
                        let g = &self.extra[self.next_extra % self.extra.len()];
                        if g.len() > room {
                            break;
                        }
                        room -= g.len();
                        v.push(g.clone());
                        self.next_extra += 1;
                    }
                    v
                }
                _ => vec![],
            };
            for g in figs {
                if fib.len() + g.len() <= 30 {
                    fib.extend(g.iter().copied());
                }
            }
            if fib.len() < 30 {
                fib.push(0xff);
            }
            fib.resize(30, 0);
            let c = coding::crc16_ccitt(&fib);
            fib.extend(c.to_be_bytes());
            bytes.extend(fib);
        }
        let mut bits = coding::unpack(&bytes);
        let prbs = coding::prbs(bits.len());
        for (b, p) in bits.iter_mut().zip(prbs) {
            *b ^= p;
        }
        let map = coding::puncture_map(&tables::fic_profile());
        coding::puncture(&coding::encode(&bits), &map)
    }

    /// One CIF of the MSC (55 296 bits), unused CUs filled with the PRBS.
    fn cif_bits(&mut self) -> Vec<u8> {
        let mut cif = coding::prbs(CIF_BITS);
        for s in &mut self.subs {
            let lf = s.source.next();
            let mut bits = coding::unpack(&lf);
            for (b, p) in bits.iter_mut().zip(&s.prbs) {
                *b ^= p;
            }
            let cw = coding::puncture(&coding::encode(&bits), &s.map);
            let n = cw.len();
            s.history.push_back(cw);
            if s.history.len() > 16 {
                s.history.pop_front();
            }
            // Table 21: bit i of this CIF comes from the logical frame
            // TIME_INTERLEAVE[i % 16] CIFs back (zeros before the start).
            let h = s.history.len();
            for i in 0..n {
                let back = tables::TIME_INTERLEAVE[i % 16];
                cif[s.start + i] = if back < h { s.history[h - 1 - back][i] } else { 0 };
            }
        }
        cif
    }

    /// One OFDM symbol's 2552 samples from its carriers.
    fn symbol(&self, z: &[Complex64]) -> Vec<Complex64> {
        let mut buf = vec![Complex64::new(0.0, 0.0); TU];
        for k in (-768i32..=768).filter(|&k| k != 0) {
            buf[(k + TU as i32) as usize % TU] = z[(k + 768) as usize];
        }
        self.fft.process(&mut buf);
        let scale = 1.0 / (K as f64).sqrt();
        let mut out: Vec<Complex64> = buf[TU - GUARD..].iter().map(|v| v * scale).collect();
        out.extend(buf.iter().map(|v| v * scale));
        out
    }

    /// A null symbol carrying TII (§14.8): each pair (k0, k0 + 1) with the
    /// phase reference's phase at k0, as strong as a data carrier, over the
    /// whole null with its last TU samples the useful part (g_TII,k: the
    /// first TNULL - TU are its cyclic prefix).
    fn tii_null(&self, main: u8, sub: u8, prs: &[Complex64]) -> Vec<Complex64> {
        let ks = tii::carriers(main, sub).expect("a valid TII");
        let mut buf = vec![Complex64::new(0.0, 0.0); TU];
        for pair in ks.chunks(2) {
            let phase = prs[(pair[0] + 768) as usize];
            for &k in pair {
                buf[(k + TU as i32) as usize % TU] = phase;
            }
        }
        self.fft.process(&mut buf);
        let scale = 1.0 / (K as f64).sqrt();
        (0..TNULL).map(|n| buf[(n + TU - (TNULL - TU)) % TU] * scale).collect()
    }

    /// The next 96 ms transmission frame, 196 608 samples at 2.048 MS/s, with
    /// mean power 1 outside the null symbol.
    pub fn frame(&mut self) -> Vec<Complex32> {
        // The phase reference symbol: z[k + 768], k = -768..768, z[768] = 0.
        let mut z = vec![Complex64::new(0.0, 0.0); 2 * 768 + 1];
        for k in -768i32..=768 {
            if k != 0 {
                let q = tables::prs_quarter_turns(k) as f64;
                z[(k + 768) as usize] = Complex64::from_polar(1.0, q * std::f64::consts::FRAC_PI_2);
            }
        }
        let mut out = match self.tii {
            Some((main, sub)) if self.cif % 8 == 0 => self.tii_null(main, sub, &z),
            _ => vec![Complex64::new(0.0, 0.0); TNULL],
        };
        out.extend(self.symbol(&z));
        self.prev = z;
        // FIC (symbols 2-4) and MSC (5-76) bits.
        let mut fic = Vec::with_capacity(3 * SYMBOL_BITS);
        let mut msc = Vec::with_capacity(CIFS * CIF_BITS);
        for c in 0..CIFS as u64 {
            let cif = self.cif + c;
            fic.extend(self.fic_block(cif));
            msc.extend(self.cif_bits());
        }
        self.cif += CIFS as u64;
        for p in fic.chunks(SYMBOL_BITS).chain(msc.chunks(SYMBOL_BITS)) {
            // §14.5-14.7: QPSK, frequency interleaving, differential.
            let mut z = self.prev.clone();
            let r = std::f64::consts::FRAC_1_SQRT_2;
            for (n, &k) in self.interleave.iter().enumerate() {
                let q = Complex64::new(r * (1.0 - 2.0 * p[n] as f64), r * (1.0 - 2.0 * p[n + K] as f64));
                let i = (k + 768) as usize;
                z[i] = self.prev[i] * q;
            }
            out.extend(self.symbol(&z));
            self.prev = z;
        }
        out.iter().map(|v| Complex32::new(v.re as f32, v.im as f32)).collect()
    }
}

/// One propagation path: delay in samples (at 2.048 MS/s), gain, and the
/// Doppler spread of its fading in Hz (0: static).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Path {
    pub delay: usize,
    pub gain: f64,
    pub doppler_hz: f64,
}

/// The channel between the transmitter and the receiver.
#[derive(Clone, Debug, PartialEq)]
pub struct Channel {
    /// SNR in the 1.536 MHz the block occupies; None: no noise.
    pub snr_db: Option<f64>,
    pub offset_hz: f64,
    pub paths: Vec<Path>,
    /// Receiver sample clock error, ppm (its rate is (1 + ppm) x nominal).
    pub clock_ppm: f64,
    /// The receiver's sample rate (2.048 MS/s, or 2.4 for a Pluto).
    pub rate: f64,
    pub seed: u64,
    /// Seconds of noise before the first frame (the receiver starts at a
    /// random point, not on a frame).
    pub lead_s: f64,
}

impl Default for Channel {
    fn default() -> Self {
        Channel { snr_db: None, offset_hz: 0.0, paths: vec![], clock_ppm: 0.0, rate: tables::RATE, seed: 1, lead_s: 0.0317 }
    }
}

/// A fading gain: a sum of sinusoids at Doppler frequencies, unit mean power.
struct Fader {
    terms: Vec<(f64, f64, f64)>,
}

impl Fader {
    fn new(fd: f64, rng: &mut Rng) -> Self {
        let n = 16;
        let terms = (0..n)
            .map(|_| {
                let a = 2.0 * std::f64::consts::PI * rng.uniform();
                (fd * a.cos(), 2.0 * std::f64::consts::PI * rng.uniform(), 2.0 * std::f64::consts::PI * rng.uniform())
            })
            .collect();
        Fader { terms }
    }
    fn at(&self, t: f64) -> Complex64 {
        let n = self.terms.len() as f64;
        let mut z = Complex64::new(0.0, 0.0);
        for &(f, p, q) in &self.terms {
            z += Complex64::new((2.0 * std::f64::consts::PI * f * t + p).cos(), (2.0 * std::f64::consts::PI * f * t + q).sin());
        }
        z / (n).sqrt()
    }
}

/// `frames` transmission frames of `ensemble` through `ch`.
pub fn dab_iq(ensemble: &TxEnsemble, frames: usize, ch: &Channel) -> Vec<Complex32> {
    sfn_iq(ensemble, &[Site { tii: None, delay: 0, gain: 1.0 }], frames, ch)
}

/// One transmitter of a single-frequency network, as the receiver has it:
/// its TII (main, sub), and its delay (samples at 2.048 MS/s) and gain.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Site {
    pub tii: Option<(u8, u8)>,
    pub delay: usize,
    pub gain: f64,
}

/// `frames` frames of `ensemble` from every site of a single-frequency
/// network (the same frames, each with its own TII), summed at the
/// receiver, then through `ch`.
pub fn sfn_iq(ensemble: &TxEnsemble, sites: &[Site], frames: usize, ch: &Channel) -> Vec<Complex32> {
    let mut rng = Rng::new(ch.seed);
    let lead = (ch.lead_s * tables::RATE) as usize;
    let mut x: Vec<Complex64> = vec![Complex64::new(0.0, 0.0); lead + frames * tables::TF];
    for site in sites {
        let mut tx = Transmitter::new(ensemble.clone());
        tx.tii = site.tii;
        let mut n = lead + site.delay;
        for _ in 0..frames {
            for v in tx.frame() {
                if n >= x.len() {
                    break;
                }
                x[n] += Complex64::new(v.re as f64, v.im as f64) * site.gain;
                n += 1;
            }
        }
    }
    // Multipath.
    let x = if ch.paths.is_empty() {
        x
    } else {
        let faders: Vec<Option<Fader>> = ch.paths.iter().map(|p| (p.doppler_hz > 0.0).then(|| Fader::new(p.doppler_hz, &mut rng))).collect();
        let mut y = vec![Complex64::new(0.0, 0.0); x.len()];
        for (p, f) in ch.paths.iter().zip(&faders) {
            for n in p.delay..x.len() {
                let g = match f {
                    // The fading changes slowly: one gain per 64 samples.
                    Some(f) => f.at(((n / 64) * 64) as f64 / tables::RATE) * p.gain,
                    None => Complex64::new(p.gain, 0.0),
                };
                y[n] += x[n - p.delay] * g;
            }
        }
        y
    };
    // Frequency offset.
    let w = 2.0 * std::f64::consts::PI * ch.offset_hz / tables::RATE;
    let x: Vec<Complex32> =
        x.iter().enumerate().map(|(n, v)| v * Complex64::from_polar(1.0, w * n as f64)).map(|v| Complex32::new(v.re as f32, v.im as f32)).collect();
    // The receiver's clock and rate.
    let out_rate = ch.rate * (1.0 + ch.clock_ppm * 1e-6);
    let mut x = if (out_rate - tables::RATE).abs() > 1e-6 {
        let mut r = Resampler::<Complex32>::new(tables::RATE, out_rate, 0.8, 32);
        let mut y = Vec::with_capacity((x.len() as f64 * out_rate / tables::RATE) as usize + 64);
        r.process(&x, &mut y);
        y
    } else {
        x
    };
    // Noise: the signal's power is 1 and occupies 1.536 MHz of the rate's
    // width, so the noise power per sample is (rate / 1.536 MHz) / SNR.
    if let Some(snr) = ch.snr_db {
        let n0 = ch.rate / 1_536_000.0 / 10f64.powf(snr / 10.0);
        let sigma = (n0 / 2.0).sqrt();
        for v in x.iter_mut() {
            *v += Complex32::new((sigma * rng.gauss()) as f32, (sigma * rng.gauss()) as f32);
        }
    }
    x
}

/// An MSC data group (EN 300 401 §5.3.3): with a session header (segment
/// number, last flag and transport id) and the CRC.
pub fn data_group(ty: u8, continuity: u8, last: bool, segment: u16, transport_id: u16, data: &[u8]) -> Vec<u8> {
    let mut g = vec![0x40 | 0x20 | 0x10 | (ty & 0x0f), (continuity & 0x0f) << 4];
    g.extend(((last as u16) << 15 | segment & 0x7fff).to_be_bytes());
    g.push(0x10 | 2); // transport id flag, two bytes follow
    g.extend(transport_id.to_be_bytes());
    g.extend_from_slice(data);
    let crc = coding::crc16_ccitt(&g);
    g.extend(crc.to_be_bytes());
    g
}

/// One MOT header extension parameter (EN 301 234 §6.2), its data field
/// in the shortest form.
pub fn mot_param(id: u8, data: &[u8]) -> Vec<u8> {
    let mut p = match data.len() {
        0 => vec![id],
        1 => vec![0x40 | id],
        4 => vec![0x80 | id],
        n if n < 128 => vec![0xc0 | id, n as u8],
        n => vec![0xc0 | id, 0x80 | (n >> 8) as u8, n as u8],
    };
    p.extend_from_slice(data);
    p
}

/// A ContentName parameter (character set 4, ISO Latin 1: ASCII here).
pub fn content_name(name: &str) -> Vec<u8> {
    let mut d = vec![4 << 4];
    d.extend(name.as_bytes());
    mot_param(0x0c, &d)
}

/// An MOT header: the core (body size, header size, content type and
/// subtype) and the given parameters.
pub fn mot_header(body_size: usize, content_type: u8, subtype: u16, params: &[Vec<u8>]) -> Vec<u8> {
    let ext: Vec<u8> = params.concat();
    let size = 7 + ext.len();
    let v: u64 = (body_size as u64 & 0x0fff_ffff) << 28 | (size as u64 & 0x1fff) << 15 | (content_type as u64 & 0x3f) << 9 | subtype as u64 & 0x1ff;
    let mut h = v.to_be_bytes()[1..].to_vec();
    h.extend(ext);
    h
}

/// An entity cut into MOT segments, each in a data group of `ty`, every
/// segment `seg` bytes but the last.
fn mot_segments(ty: u8, tid: u16, bytes: &[u8], seg: usize) -> Vec<Vec<u8>> {
    let chunks: Vec<&[u8]> = if bytes.is_empty() { vec![&[][..]] } else { bytes.chunks(seg).collect() };
    let n = chunks.len();
    chunks
        .iter()
        .enumerate()
        .map(|(i, c)| {
            let mut d = (c.len() as u16 & 0x1fff).to_be_bytes().to_vec(); // repetition count 0
            d.extend_from_slice(c);
            data_group(ty, i as u8, i + 1 == n, i as u16, tid, &d)
        })
        .collect()
}

/// An MOT object in header mode (SlideShow): its header's data groups
/// (type 3), then its body's (type 4).
pub fn mot_header_mode(tid: u16, header: &[u8], body: &[u8], seg: usize) -> Vec<Vec<u8>> {
    let mut v = mot_segments(3, tid, header, seg);
    v.extend(mot_segments(4, tid, body, seg));
    v
}

/// A carousel in directory mode (SPI): the directory's data groups (type
/// 6, transport id `dir_tid`), then each object's body (type 4).
pub fn mot_directory_mode(dir_tid: u16, objects: &[(u16, Vec<u8>, Vec<u8>)], seg: usize) -> Vec<Vec<u8>> {
    let mut entries = Vec::new();
    for (tid, header, _) in objects {
        entries.extend(tid.to_be_bytes());
        entries.extend_from_slice(header);
    }
    let size = 13 + entries.len();
    let mut dir = (size as u32 & 0x3fff_ffff).to_be_bytes().to_vec();
    dir.extend((objects.len() as u16).to_be_bytes());
    dir.extend([0, 0, 0]); // no carousel period
    dir.extend((seg as u16 & 0x1fff).to_be_bytes());
    dir.extend([0, 0]); // no directory extension
    dir.extend(entries);
    let mut v = mot_segments(6, dir_tid, &dir, seg);
    for (tid, _, body) in objects {
        v.extend(mot_segments(4, *tid, body, seg));
    }
    v
}

/// The header of a picture whose size is all a receiver reads first: a
/// PNG's signature and IHDR (and its CRC field, zero), then `fill` bytes.
/// Not a picture anyone could show; the decoders read only the header.
pub fn test_png(width: u32, height: u32, fill: usize) -> Vec<u8> {
    let mut b = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR".to_vec();
    b.extend(width.to_be_bytes());
    b.extend(height.to_be_bytes());
    b.extend([8, 6, 0, 0, 0, 0, 0, 0, 0]);
    b.extend((0..fill).map(|i| (i * 7 + 3) as u8));
    b
}

/// A JPEG's start and its SOF0 marker (as `test_png`, a header only).
pub fn test_jpeg(width: u32, height: u32, fill: usize) -> Vec<u8> {
    let mut b = vec![0xff, 0xd8, 0xff, 0xe0, 0, 4, 0, 0, 0xff, 0xc0, 0, 11, 8];
    b.extend((height as u16).to_be_bytes());
    b.extend((width as u16).to_be_bytes());
    b.extend([1, 1, 0x11, 0]);
    b.extend((0..fill).map(|i| (i * 13 + 5) as u8));
    b
}

/// SPI's binary encoding (TS 102 371 §4): an element or attribute with its
/// tag and length.
pub fn spi_node(tag: u8, data: &[u8]) -> Vec<u8> {
    let mut v = vec![tag];
    match data.len() {
        n if n < 0xfe => v.push(n as u8),
        n if n < 0x1_0000 => {
            v.push(0xfe);
            v.extend((n as u16).to_be_bytes());
        }
        n => {
            v.push(0xff);
            v.extend(&(n as u32).to_be_bytes()[1..]);
        }
    }
    v.extend_from_slice(data);
    v
}

/// A DAB bearerURI's binary id (§4.7.6.1) for a 16-bit SId.
pub fn spi_bearer(ecc: u8, eid: u16, sid: u16) -> Vec<u8> {
    let mut v = vec![0x40, ecc];
    v.extend(eid.to_be_bytes());
    v.extend(sid.to_be_bytes());
    v
}

/// A service information file (SPI 7/0): the ensemble and, for each
/// service, its name and logos (url, width, height).
pub fn spi_si_file(ecc: u8, eid: u16, services: &[(u16, &str, Vec<(&str, u16, u16)>)]) -> Vec<u8> {
    let mut ens = spi_node(0x80, &[&[ecc][..], &eid.to_be_bytes()].concat());
    for (sid, name, logos) in services {
        let mut s = spi_node(0x12, &spi_node(0x01, name.as_bytes()));
        let mut media = Vec::new();
        for (url, w, h) in logos {
            let mut m = spi_node(0x82, url.as_bytes());
            m.extend(spi_node(0x83, &[2])); // logo_unrestricted: sized by width and height
            m.extend(spi_node(0x84, &w.to_be_bytes()));
            m.extend(spi_node(0x85, &h.to_be_bytes()));
            media.extend(spi_node(0x2b, &m));
        }
        s.extend(spi_node(0x13, &media));
        s.extend(spi_node(0x29, &spi_node(0x80, &spi_bearer(ecc, eid, *sid))));
        ens.extend(spi_node(0x28, &s));
    }
    spi_node(0x03, &spi_node(0x26, &ens))
}

/// A timePoint (§4.7.4), long form, no LTO.
pub fn spi_time(mjd: u32, h: u32, m: u32, s: u32) -> Vec<u8> {
    let v: u32 = (mjd & 0x1ffff) << 14 | 1 << 11 | (h & 0x1f) << 6 | (m & 0x3f);
    let mut b = v.to_be_bytes().to_vec();
    b.extend([(s as u8) << 2, 0]);
    b
}

/// A programme information file (SPI 7/1) for one service: programmes as
/// (name, start timePoint, duration in seconds, short description).
pub fn spi_pi_file(ecc: u8, eid: u16, sid: u16, programmes: &[(&str, Vec<u8>, u16, Option<&str>)]) -> Vec<u8> {
    let mut sched = Vec::new();
    for (name, start, dur, desc) in programmes {
        let mut p = spi_node(0x12, &spi_node(0x01, name.as_bytes()));
        let mut loc = spi_node(0x2c, &[spi_node(0x80, start), spi_node(0x81, &dur.to_be_bytes())].concat());
        loc.extend(spi_node(0x2d, &spi_node(0x80, &spi_bearer(ecc, eid, sid))));
        p.extend(spi_node(0x19, &loc));
        if let Some(d) = desc {
            p.extend(spi_node(0x13, &spi_node(0x1a, &spi_node(0x01, d.as_bytes()))));
        }
        sched.extend(spi_node(0x1c, &p));
    }
    spi_node(0x02, &spi_node(0x21, &sched))
}

/// A picture that shows, for the showcase's slides and logos: horizontal
/// bands of two colours inside a white border, as a PNG by sstv::png's
/// encoder. Its rows repeat, so it deflates to a few hundred bytes.
pub fn showcase_png(width: u32, height: u32, a: [u8; 3], b: [u8; 3]) -> Vec<u8> {
    let mut rgb = Vec::with_capacity((width * height * 3) as usize);
    for y in 0..height {
        for x in 0..width {
            let edge = x < 4 || y < 4 || x + 4 >= width || y + 4 >= height;
            let band = y / (height / 4).max(1) % 2 == 0;
            rgb.extend(if edge { [255, 255, 255] } else if band { a } else { b });
        }
    }
    crate::sstv::png::encode(&rgb, width, height)
}

impl TxEnsemble {
    /// The standard ensemble with everything the data side can show, for an
    /// app's simulator: a PNG slide in each service's X-PAD, programme
    /// types, languages, country and DL Plus (on C221's own label) in the
    /// FIC and PAD, and a 32 kbit/s SPI service with both logos of each
    /// service, their service
    /// information and a day of programmes on `mjd`, in four six-hour
    /// programmes from 00:00 UTC.
    pub fn showcase(mjd: u32) -> Self {
        let (ecc, eid) = (0xe1, 0xc1ce);
        let mut e = Self::standard();
        let colours = [([200, 40, 40], [250, 200, 60]), ([40, 90, 200], [120, 220, 240])];
        for (i, s) in e.services.iter_mut().enumerate() {
            let body = showcase_png(320, 240, colours[i].0, colours[i].1);
            let h = mot_header(
                body.len(),
                2,
                3,
                &[content_name(&format!("slide-{:04x}.png", s.sid)), mot_param(0x05, &[0, 0, 0, 0]), mot_param(0x26, b"Shack test card")],
            );
            s.xpad_groups = mot_header_mode(0x0100 + i as u16, &h, &body, 1024);
        }
        // C221's first label only (an app's loop never reaches the second),
        // with DL Plus naming parts of it: ITEM.TITLE "1 kHz left", ITEM.ARTIST
        // "Shack MP2 Tone".
        e.services[0].dls.truncate(1);
        e.services[0].dl_plus = vec![(1, 22, 10), (4, 0, 14)];
        let mut objs = Vec::new();
        let mut si = Vec::new();
        let mut tid = 10u16;
        for (i, s) in e.services.iter().enumerate() {
            let mut logos = Vec::new();
            for (w, h) in [(128u32, 128u32), (320, 240)] {
                let name = format!("{:04x}_{w}x{h}.png", s.sid);
                let body = showcase_png(w, h, colours[i].1, colours[i].0);
                objs.push((tid, mot_header(body.len(), 2, 3, &[content_name(&name)]), body));
                tid += 1;
                logos.push((name, w as u16, h as u16));
            }
            si.push((s.sid, s.label.clone(), logos));
            let progs: Vec<(String, Vec<u8>, u16, Option<String>)> = (0..4)
                .map(|k| (format!("{} {}", s.label, ["Night", "Morning", "Afternoon", "Evening"][k]), spi_time(mjd, 6 * k as u32, 0, 0), 21_600, Some("Test tones from the Shack".to_string())))
                .collect();
            let progs: Vec<(&str, Vec<u8>, u16, Option<&str>)> = progs.iter().map(|p| (p.0.as_str(), p.1.clone(), p.2, p.3.as_deref())).collect();
            let pi = spi_pi_file(ecc, eid, s.sid, &progs);
            objs.push((tid, mot_header(pi.len(), 7, 1, &[content_name(&format!("{:04x}_pi.dat", s.sid)), mot_param(0x27, &spi_bearer(ecc, eid, s.sid))]), pi));
            tid += 1;
        }
        let si: Vec<(u16, &str, Vec<(&str, u16, u16)>)> =
            si.iter().map(|(sid, label, logos)| (*sid, label.as_str(), logos.iter().map(|l| (l.0.as_str(), l.1, l.2)).collect())).collect();
        let si = spi_si_file(ecc, eid, &si);
        objs.push((tid, mot_header(si.len(), 7, 0, &[content_name("si.dat")]), si));
        let mut e2 = Self::with_data(0x007, mot_directory_mode(1, &objs, 512));
        e2.data[0].protection = Protection::Eep { option: 0, level: 3, size: 24 };
        e2.services = e.services;
        e2.figs = vec![
            fig(0, vec![0x0d, 0xc2, 0x21, 0x01, 0x00, 0x42, 0x0c, 0x3c]),
            fig(0, vec![0x0d, 0xc3, 0xa7, 0x01, 0x00, 0x42, 0x0c, 0x3c]),
            fig(0, vec![0x11, 0xc2, 0x21, 0x00, 0x0a, 0xc3, 0xa7, 0x00, 0x09]),
            fig(0, vec![0x05, 0x01, 0x09, 0x02, 0x09]),
            fig(0, vec![0x09, 0x00, ecc, 0x01]),
        ];
        e2
    }
}
