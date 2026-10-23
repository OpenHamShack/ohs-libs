//! MSC data groups (EN 300 401 §5.3.3) and the Multimedia Object Transfer
//! protocol on them (ETSI EN 301 234): the objects SlideShow's pictures
//! (TS 101 499, header mode) and SPI's carousel (TS 102 371, directory
//! mode) arrive as.
//!
//! A data group carries one segment of an object's header (type 3), body
//! (type 4) or directory (type 6), tagged with the object's transport id.
//! `MotDecoder` gathers the segments and hands back each object once it is
//! whole: its content type, its header parameters and its body.
//!
//! **Memory is bounded, whatever is sent.** A broadcast is not trusted to
//! be sane: an object whose header announces more than `max_object` bytes
//! (SlideShow's own ceiling, 450 KB) is refused at once, and one that grows
//! past it without a header is dropped; at most `max_objects` objects are
//! assembled at once and `max_total` bytes held, the least recently heard
//! going first; and an object not heard from for `timeout_s` (sample time,
//! never the clock) is dropped. Objects already handed over are remembered
//! by transport id (a few hundred, for as long as the timeout) so a
//! carousel's repetitions are not decoded again.
//!
//! Not read: compressed objects (CompressionType, which only SPI's
//! "advanced" profile uses), scrambled bodies (type 5) and CA (CAInfo):
//! they are counted and dropped, as a receiver without them must.

use super::coding::crc16_ccitt;
use std::collections::{BTreeMap, VecDeque};

/// MOT content types (EN 301 234 §6.1, TS 101 756 table 17).
pub const CT_IMAGE: u8 = 2;
pub const CT_TRANSPORT: u8 = 5;
pub const CT_APPLICATION: u8 = 7;
pub const IMAGE_JFIF: u16 = 1;
pub const IMAGE_PNG: u16 = 3;

/// MOT header parameters we read (EN 301 234 §6.3).
pub const P_CONTENT_NAME: u8 = 0x0c;
pub const P_COMPRESSION: u8 = 0x11;
pub const P_CA_INFO: u8 = 0x23;

/// One MSC data group, its CRC checked.
#[derive(Clone, Debug, PartialEq)]
pub struct DataGroup {
    pub ty: u8,
    pub continuity: u8,
    pub repetition: u8,
    pub last: bool,
    pub segment: u16,
    pub transport_id: Option<u16>,
    /// The data field, after the session header.
    pub data: Vec<u8>,
}

/// Parse an MSC data group. `Err("crc")` when its CRC fails, `Err("short")`
/// when it is cut off.
pub fn parse_data_group(g: &[u8]) -> Result<DataGroup, &'static str> {
    if g.len() < 2 {
        return Err("short");
    }
    let (ext, crc, seg, ua) = (g[0] & 0x80 != 0, g[0] & 0x40 != 0, g[0] & 0x20 != 0, g[0] & 0x10 != 0);
    let end = if crc {
        if g.len() < 4 {
            return Err("short");
        }
        let n = g.len() - 2;
        if crc16_ccitt(&g[..n]) != u16::from_be_bytes([g[n], g[n + 1]]) {
            return Err("crc");
        }
        n
    } else {
        g.len()
    };
    let mut k = 2 + if ext { 2 } else { 0 };
    let (mut last, mut segment) = (false, 0);
    if seg {
        if k + 2 > end {
            return Err("short");
        }
        last = g[k] & 0x80 != 0;
        segment = u16::from_be_bytes([g[k] & 0x7f, g[k + 1]]);
        k += 2;
    }
    let mut transport_id = None;
    if ua {
        if k >= end {
            return Err("short");
        }
        let li = (g[k] & 0x0f) as usize;
        let tid = g[k] & 0x10 != 0;
        if k + 1 + li > end {
            return Err("short");
        }
        if tid && li >= 2 {
            transport_id = Some(u16::from_be_bytes([g[k + 1], g[k + 2]]));
        }
        k += 1 + li;
    }
    Ok(DataGroup {
        ty: g[0] & 0x0f,
        continuity: g[1] >> 4,
        repetition: g[1] & 0x0f,
        last,
        segment,
        transport_id,
        data: g[k..end].to_vec(),
    })
}

/// An MOT object's header: its core and its extension's parameters.
#[derive(Clone, Debug, PartialEq)]
pub struct Header {
    pub body_size: usize,
    pub content_type: u8,
    pub subtype: u16,
    pub params: Vec<(u8, Vec<u8>)>,
}

impl Header {
    /// Parse a header (core and extension) from the start of `b`; with its
    /// length in bytes.
    pub fn parse(b: &[u8]) -> Option<(Header, usize)> {
        if b.len() < 7 {
            return None;
        }
        let body_size = (u32::from_be_bytes([b[0], b[1], b[2], b[3]]) >> 4) as usize;
        let header_size = ((b[3] as usize & 0x0f) << 9) | (b[4] as usize) << 1 | (b[5] as usize >> 7);
        let content_type = (b[5] >> 1) & 0x3f;
        let subtype = ((b[5] as u16 & 1) << 8) | b[6] as u16;
        if header_size < 7 || header_size > b.len() {
            return None;
        }
        let mut params = Vec::new();
        let mut k = 7;
        while k < header_size {
            let (pli, id) = (b[k] >> 6, b[k] & 0x3f);
            k += 1;
            let len = match pli {
                0 => 0,
                1 => 1,
                2 => 4,
                _ => {
                    if k >= header_size {
                        return None;
                    }
                    if b[k] & 0x80 != 0 {
                        if k + 2 > header_size {
                            return None;
                        }
                        k += 2;
                        (u16::from_be_bytes([b[k - 2], b[k - 1]]) & 0x7fff) as usize
                    } else {
                        k += 1;
                        (b[k - 1] & 0x7f) as usize
                    }
                }
            };
            if k + len > header_size {
                return None;
            }
            params.push((id, b[k..k + len].to_vec()));
            k += len;
        }
        Some((Header { body_size, content_type, subtype, params }, header_size))
    }

    pub fn param(&self, id: u8) -> Option<&[u8]> {
        self.params.iter().find(|p| p.0 == id).map(|p| p.1.as_slice())
    }

    /// ContentName, without its character set byte (§6.2.2.1.1).
    pub fn content_name(&self) -> Option<String> {
        let p = self.param(P_CONTENT_NAME)?;
        let (cs, name) = (p.first()? >> 4, &p[1..]);
        Some(super::tables::label_text(name, if cs == 0 { 4 } else { cs }))
    }

    /// A text parameter (UTF-8, as SlideShow's CategoryTitle and
    /// ClickThroughURL are), or None.
    pub fn text(&self, id: u8) -> Option<String> {
        let p = self.param(id)?;
        let s = String::from_utf8_lossy(p).trim_end_matches('\0').trim().to_string();
        (!s.is_empty()).then_some(s)
    }
}

/// A whole object.
#[derive(Clone, Debug, PartialEq)]
pub struct MotObject {
    pub transport_id: u16,
    pub header: Header,
    pub body: Vec<u8>,
}

/// The decoder's bounds (module docs).
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub max_object: usize,
    pub max_objects: usize,
    pub max_total: usize,
    pub max_directory: usize,
    pub timeout_s: f64,
}

impl Limits {
    /// SlideShow (TS 101 499 §9.1: a slide is at most 450 kbytes).
    pub const SLIDESHOW: Limits =
        Limits { max_object: 460_800, max_objects: 4, max_total: 2 * 460_800, max_directory: 0, timeout_s: 600.0 };
    /// SPI's carousel: many small objects (logos, a few files a day of
    /// programme information), a directory of up to 64 KB.
    pub const SPI: Limits =
        Limits { max_object: 460_800, max_objects: 128, max_total: 4 << 20, max_directory: 65_536, timeout_s: 900.0 };
}

/// Segments of one entity (a header, a body or a directory).
#[derive(Default, Clone, Debug)]
struct Segments {
    parts: BTreeMap<u16, Vec<u8>>,
    last: Option<u16>,
    bytes: usize,
}

impl Segments {
    /// Add a segment (data field: segmentation header, then the segment).
    fn add(&mut self, dg: &DataGroup) {
        if dg.data.len() < 2 || self.parts.contains_key(&dg.segment) {
            return;
        }
        let size = (u16::from_be_bytes([dg.data[0], dg.data[1]]) & 0x1fff) as usize;
        let seg = &dg.data[2..(2 + size).min(dg.data.len())];
        self.bytes += seg.len();
        self.parts.insert(dg.segment, seg.to_vec());
        if dg.last {
            self.last = Some(dg.segment);
        }
    }

    fn whole(&self) -> Option<Vec<u8>> {
        let last = self.last?;
        if self.parts.len() != last as usize + 1 || !self.parts.keys().copied().eq(0..=last) {
            return None;
        }
        Some(self.parts.values().flatten().copied().collect())
    }
}

#[derive(Default, Clone, Debug)]
struct Assembly {
    header: Option<Header>,
    header_segs: Segments,
    body: Segments,
    heard: f64,
}

/// What `MotDecoder` counts.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct MotCounts {
    pub groups_bad: u64,
    pub objects: u64,
    pub oversize: u64,
    pub timed_out: u64,
    pub evicted: u64,
    pub unsupported: u64,
}

pub struct MotDecoder {
    limits: Limits,
    open: BTreeMap<u16, Assembly>,
    /// The directory (directory mode), by its own transport id, and its
    /// segments while it is assembled.
    directory: Option<(u16, BTreeMap<u16, Header>)>,
    dir_segs: BTreeMap<u16, (Segments, f64)>,
    /// Transport ids handed over (and when) or refused: not assembled again.
    done: VecDeque<(u16, f64)>,
    refused: VecDeque<(u16, f64)>,
    pub counts: MotCounts,
}

impl MotDecoder {
    pub fn new(limits: Limits) -> Self {
        MotDecoder {
            limits,
            open: BTreeMap::new(),
            directory: None,
            dir_segs: BTreeMap::new(),
            done: VecDeque::new(),
            refused: VecDeque::new(),
            counts: MotCounts::default(),
        }
    }

    /// The directory's entries, once one is whole: (transport id, header).
    pub fn directory(&self) -> Option<&BTreeMap<u16, Header>> {
        self.directory.as_ref().map(|d| &d.1)
    }

    /// Has every object the directory lists been handed over (or refused)
    /// since it arrived: the carousel received once round.
    pub fn carousel_complete(&self) -> bool {
        self.directory.as_ref().is_some_and(|d| !d.1.is_empty() && d.1.keys().all(|&tid| self.is_done(tid)))
    }

    /// Bytes held now (segments being assembled).
    pub fn held(&self) -> usize {
        self.open.values().map(|a| a.body.bytes + a.header_segs.bytes).sum::<usize>()
            + self.dir_segs.values().map(|d| d.0.bytes).sum::<usize>()
    }

    fn is_done(&self, tid: u16) -> bool {
        self.done.iter().any(|d| d.0 == tid) || self.refused.iter().any(|d| d.0 == tid)
    }

    fn remember(list: &mut VecDeque<(u16, f64)>, tid: u16, t: f64) {
        if list.len() >= 512 {
            list.pop_front();
        }
        list.push_back((tid, t));
    }

    fn refuse(&mut self, tid: u16, t: f64) {
        self.open.remove(&tid);
        self.counts.oversize += 1;
        Self::remember(&mut self.refused, tid, t);
    }

    /// Forget what has not been heard from for the timeout.
    fn expire(&mut self, t: f64) {
        let limit = self.limits.timeout_s;
        let before = self.open.len();
        self.open.retain(|_, a| t - a.heard <= limit);
        self.counts.timed_out += (before - self.open.len()) as u64;
        self.dir_segs.retain(|_, d| t - d.1 <= limit);
        while self.done.front().is_some_and(|d| t - d.1 > limit) {
            self.done.pop_front();
        }
        while self.refused.front().is_some_and(|d| t - d.1 > limit) {
            self.refused.pop_front();
        }
    }

    /// Hold no more than the limits allow: the least recently heard go.
    fn bound(&mut self) {
        while self.open.len() > self.limits.max_objects || self.held() > self.limits.max_total {
            let Some((&oldest, _)) = self.open.iter().min_by(|a, b| a.1.heard.total_cmp(&b.1.heard)) else { break };
            self.open.remove(&oldest);
            self.counts.evicted += 1;
        }
    }

    /// One data group's bytes (CRC unchecked), at sample time `t`.
    pub fn push_bytes(&mut self, g: &[u8], t: f64) -> Vec<MotObject> {
        match parse_data_group(g) {
            Ok(dg) => self.push(&dg, t),
            Err(_) => {
                self.counts.groups_bad += 1;
                Vec::new()
            }
        }
    }

    /// One data group, at sample time `t`: the objects it completed.
    pub fn push(&mut self, dg: &DataGroup, t: f64) -> Vec<MotObject> {
        self.expire(t);
        let Some(tid) = dg.transport_id else { return Vec::new() };
        match dg.ty {
            3 | 4 => {
                if self.is_done(tid) {
                    return Vec::new();
                }
                let a = self.open.entry(tid).or_default();
                a.heard = t;
                if dg.ty == 3 {
                    a.header_segs.add(dg);
                    if a.header.is_none() {
                        if let Some(h) = a.header_segs.whole().and_then(|b| Header::parse(&b).map(|x| x.0)) {
                            a.header = Some(h);
                        }
                    }
                } else {
                    a.body.add(dg);
                }
                let size = a.header.as_ref().map(|h| h.body_size).or_else(|| {
                    self.directory.as_ref().and_then(|d| d.1.get(&tid)).map(|h| h.body_size)
                });
                let a = self.open.get(&tid).unwrap();
                if size.is_some_and(|s| s > self.limits.max_object) || a.body.bytes > self.limits.max_object {
                    self.refuse(tid, t);
                    return Vec::new();
                }
                let out = self.try_finish(tid, t);
                self.bound();
                out.into_iter().collect()
            }
            6 => {
                if self.limits.max_directory == 0 || self.directory.as_ref().is_some_and(|d| d.0 == tid) {
                    return Vec::new();
                }
                let e = self.dir_segs.entry(tid).or_default();
                e.1 = t;
                e.0.add(dg);
                if e.0.bytes > self.limits.max_directory {
                    self.dir_segs.remove(&tid);
                    self.counts.oversize += 1;
                    return Vec::new();
                }
                let Some(bytes) = e.0.whole() else { return Vec::new() };
                self.dir_segs.remove(&tid);
                let Some(entries) = parse_directory(&bytes) else {
                    self.counts.groups_bad += 1;
                    return Vec::new();
                };
                // A new directory: what it does not list is gone; what it
                // lists and we hold may now be whole.
                self.open.retain(|k, _| entries.contains_key(k));
                self.directory = Some((tid, entries));
                let tids: Vec<u16> = self.open.keys().copied().collect();
                tids.into_iter().filter_map(|k| self.try_finish(k, t)).collect()
            }
            5 | 7 => {
                self.counts.unsupported += 1;
                Vec::new()
            }
            _ => Vec::new(),
        }
    }

    fn try_finish(&mut self, tid: u16, t: f64) -> Option<MotObject> {
        let a = self.open.get(&tid)?;
        let header = a.header.clone().or_else(|| self.directory.as_ref().and_then(|d| d.1.get(&tid)).cloned())?;
        let body = if header.body_size == 0 { Vec::new() } else { a.body.whole()? };
        if body.len() != header.body_size {
            // A size that disagrees with the header: a broken object.
            self.open.remove(&tid);
            self.counts.groups_bad += 1;
            return None;
        }
        self.open.remove(&tid);
        Self::remember(&mut self.done, tid, t);
        if header.param(P_COMPRESSION).is_some() || header.param(P_CA_INFO).is_some() {
            self.counts.unsupported += 1;
            return None;
        }
        self.counts.objects += 1;
        Some(MotObject { transport_id: tid, header, body })
    }
}

/// An MOT directory (EN 301 234 §7.2.4.1, figure 30): its entries'
/// transport ids and headers.
pub fn parse_directory(b: &[u8]) -> Option<BTreeMap<u16, Header>> {
    if b.len() < 13 || b[0] & 0x80 != 0 {
        return None; // too short, or compressed (CF set: not defined)
    }
    let n = u16::from_be_bytes([b[4], b[5]]) as usize;
    let ext = u16::from_be_bytes([b[11], b[12]]) as usize;
    let mut k = 13 + ext;
    let mut out = BTreeMap::new();
    for _ in 0..n {
        if k + 2 > b.len() {
            return None;
        }
        let tid = u16::from_be_bytes([b[k], b[k + 1]]);
        let (h, len) = Header::parse(&b[k + 2..])?;
        out.insert(tid, h);
        k += 2 + len;
    }
    Some(out)
}

/// The size of an image from its own header: PNG's IHDR, or a JPEG's
/// start-of-frame marker. None for anything else.
pub fn image_size(b: &[u8]) -> Option<(u32, u32)> {
    if b.len() >= 24 && b.starts_with(b"\x89PNG\r\n\x1a\n") && &b[12..16] == b"IHDR" {
        let w = u32::from_be_bytes([b[16], b[17], b[18], b[19]]);
        let h = u32::from_be_bytes([b[20], b[21], b[22], b[23]]);
        return Some((w, h));
    }
    if b.len() >= 4 && b[0] == 0xff && b[1] == 0xd8 {
        let mut k = 2;
        while k + 4 <= b.len() {
            if b[k] != 0xff {
                return None;
            }
            let m = b[k + 1];
            if m == 0xff {
                k += 1;
                continue;
            }
            let len = u16::from_be_bytes([b[k + 2], b[k + 3]]) as usize;
            // SOF0-SOF15 but DHT (C4), JPG (C8) and DAC (CC).
            if (0xc0..=0xcf).contains(&m) && !matches!(m, 0xc4 | 0xc8 | 0xcc) {
                if k + 9 > b.len() {
                    return None;
                }
                let h = u16::from_be_bytes([b[k + 5], b[k + 6]]) as u32;
                let w = u16::from_be_bytes([b[k + 7], b[k + 8]]) as u32;
                return Some((w, h));
            }
            k += 2 + len;
        }
    }
    None
}

/// The MIME type of an image object (SlideShow allows only these two).
pub fn image_mime(h: &Header) -> Option<&'static str> {
    match (h.content_type, h.subtype) {
        (CT_IMAGE, IMAGE_JFIF) => Some("image/jpeg"),
        (CT_IMAGE, IMAGE_PNG) => Some("image/png"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dab::pad::{PadDecoder, PadEncoder};
    use crate::dab::tx::{content_name, mot_header, mot_header_mode, mot_param, test_jpeg};

    /// A slide's MOT object: (header, body).
    fn slide(name: &str, body: &[u8]) -> Vec<u8> {
        mot_header(
            body.len(),
            CT_IMAGE,
            IMAGE_JFIF,
            &[
                content_name(name),
                mot_param(0x05, &[0, 0, 0, 0]), // TriggerTime: now
                mot_param(0x26, b"News"),
                mot_param(0x27, b"https://example.org/story"),
            ],
        )
    }

    /// Data groups through X-PAD frames and back: the groups out.
    fn through_xpad(groups: Vec<Vec<u8>>, subfield: usize) -> (Vec<Vec<u8>>, usize) {
        let mut enc = PadEncoder::with_mot(subfield);
        enc.queue_groups(groups);
        let mut dec = PadDecoder::new();
        let mut out = Vec::new();
        let mut frames = 0;
        while enc.groups_queued() > 0 {
            let (x, f) = enc.frame();
            let mut before = vec![0x33; 20];
            before.extend(x);
            dec.push(&before, f);
            out.extend(dec.take_mot());
            frames += 1;
        }
        (out, frames)
    }

    #[test]
    fn a_slide_is_reassembled_across_segments_and_x_pad_frames() {
        let body = test_jpeg(320, 240, 5000);
        let groups = mot_header_mode(0x1234, &slide("slide-1.jpg", &body), &body, 1024);
        assert!(groups.len() >= 6, "several body segments");
        let (got, frames) = through_xpad(groups, 16);
        assert!(frames > 300, "carried over many frames: {frames}");
        let mut mot = MotDecoder::new(Limits::SLIDESHOW);
        let mut objs = Vec::new();
        for (i, g) in got.iter().enumerate() {
            objs.extend(mot.push_bytes(g, i as f64 * 0.024));
        }
        assert_eq!(objs.len(), 1);
        let o = &objs[0];
        assert_eq!((o.transport_id, o.body.as_slice()), (0x1234, body.as_slice()));
        assert_eq!(image_mime(&o.header), Some("image/jpeg"));
        assert_eq!(o.header.content_name().as_deref(), Some("slide-1.jpg"));
        assert_eq!(o.header.text(0x26).as_deref(), Some("News"));
        assert_eq!(o.header.text(0x27).as_deref(), Some("https://example.org/story"));
        assert_eq!(image_size(&o.body), Some((320, 240)));
        // A carousel's repeat is not handed over again.
        for g in &got {
            assert!(mot.push_bytes(g, 30.0).is_empty());
        }
    }

    #[test]
    fn a_segment_whose_crc_fails_is_dropped_and_the_repeat_completes_it() {
        let body = test_jpeg(64, 48, 3000);
        let groups = mot_header_mode(7, &slide("s.jpg", &body), &body, 512);
        let mut bad = groups.clone();
        let n = bad[3].len();
        bad[3][n / 2] ^= 0x10; // one bit of a body segment
        let mut mot = MotDecoder::new(Limits::SLIDESHOW);
        let first: Vec<MotObject> = bad.iter().flat_map(|g| mot.push_bytes(g, 1.0)).collect();
        assert!(first.is_empty(), "nothing whole with a segment missing");
        assert_eq!(mot.counts.groups_bad, 1);
        // The next turn of the carousel brings the segment.
        let again: Vec<MotObject> = groups.iter().flat_map(|g| mot.push_bytes(g, 9.0)).collect();
        assert_eq!(again.len(), 1);
        assert_eq!(again[0].body, body);
    }

    #[test]
    fn an_oversize_object_is_refused_and_nothing_of_it_is_held() {
        // A header announcing more than SlideShow's 450 KB.
        let body = test_jpeg(4000, 3000, 470_000);
        let groups = mot_header_mode(9, &slide("big.jpg", &body), &body, 8000);
        let mut mot = MotDecoder::new(Limits::SLIDESHOW);
        for g in &groups {
            assert!(mot.push_bytes(g, 2.0).is_empty());
            assert!(mot.held() <= 8200, "held {}", mot.held());
        }
        assert_eq!((mot.counts.oversize, mot.counts.objects), (1, 0));
        // Without its header (joined mid-object) it is refused once it grows past the cap.
        let mut mot = MotDecoder::new(Limits::SLIDESHOW);
        for g in groups.iter().filter(|g| g[0] & 0x0f == 4) {
            mot.push_bytes(g, 3.0);
        }
        assert_eq!(mot.counts.oversize, 1);
        assert_eq!(mot.held(), 0);
    }

    #[test]
    fn objects_assembled_at_once_and_their_age_are_bounded() {
        let mut mot = MotDecoder::new(Limits::SLIDESHOW);
        // Ten objects' first segments only: no more than four are kept.
        for tid in 0..10u16 {
            let body = test_jpeg(10, 10, 3000);
            let g = mot_header_mode(tid, &slide("x.jpg", &body), &body, 1000);
            mot.push_bytes(&g[0], tid as f64);
            mot.push_bytes(&g[1], tid as f64);
        }
        assert!(mot.open.len() <= 4);
        assert_eq!(mot.counts.evicted, 6);
        // Ten minutes later with nothing heard, the rest go.
        let body = test_jpeg(10, 10, 10);
        let g = mot_header_mode(99, &slide("y.jpg", &body), &body, 1000);
        mot.push_bytes(&g[0], 10.0 + 601.0);
        assert_eq!(mot.open.len(), 1);
        assert_eq!(mot.counts.timed_out, 4);
    }

    #[test]
    fn a_header_is_parsed_with_every_length_form() {
        let long = vec![b'x'; 300];
        let h = mot_header(5, CT_IMAGE, IMAGE_PNG, &[mot_param(1, &[]), mot_param(2, &[9]), mot_param(3, &[1, 2, 3, 4]), mot_param(4, &long)]);
        let (got, len) = Header::parse(&h).unwrap();
        assert_eq!(len, h.len());
        assert_eq!((got.body_size, got.content_type, got.subtype), (5, CT_IMAGE, IMAGE_PNG));
        assert_eq!(got.param(1), Some(&[][..]));
        assert_eq!(got.param(2), Some(&[9][..]));
        assert_eq!(got.param(3), Some(&[1, 2, 3, 4][..]));
        assert_eq!(got.param(4).map(|p| p.len()), Some(300));
    }
}
