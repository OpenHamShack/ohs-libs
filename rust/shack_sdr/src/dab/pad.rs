//! Programme Associated Data (EN 300 401 §7.4): the F-PAD at the end of
//! every DAB audio frame or DAB+ AU, the X-PAD before it, and from it the
//! dynamic label (DLS, §7.4.5.2): the programme text a receiver shows.
//!
//! Where the PAD is: in old DAB, the last bytes of each MP2 frame (X-PAD,
//! then the scale-factor CRC, then the two F-PAD bytes); in DAB+, a
//! data_stream_element at the start of an AU (TS 102 563 §5.4). The
//! X-PAD's bytes are sent in reverse order (§7.4.2), so its contents
//! indicators, logically first, are the last bytes before the CRC or
//! F-PAD. dablin (Stefan Pöschel, GPL-3.0-or-later) was read for where
//! the PAD sits in each (`dab_decoder.cpp`, `dabplus_decoder.cpp`); the
//! rest is from the specification.
//!
//! `PadEncoder` is the other direction, for the DAB transmitter model.

use super::coding::crc16_ccitt;
use super::tables::label_text;

/// Bytes of a variable-size X-PAD sub-field, by its length code (§7.4.4.2).
const SUBFIELD: [usize; 8] = [4, 6, 8, 12, 16, 24, 32, 48];

/// The scale-factor CRC's length in an old DAB frame (TS 103 466, as
/// dablin reads it): 2 bytes for MPEG-1 below 56 kbit/s a channel, else 4.
pub fn scf_crc_len(lsf: bool, bitrate: u16, mono: bool) -> usize {
    if !lsf && bitrate < if mono { 56 } else { 112 } {
        2
    } else {
        4
    }
}

/// An old DAB frame's PAD: (the bytes before the scale-factor CRC, in which
/// the X-PAD ends; the F-PAD).
pub fn mp2_pad(frame: &[u8], scf_crc: usize) -> Option<(&[u8], [u8; 2])> {
    let n = frame.len();
    (n >= scf_crc + 6).then(|| (&frame[..n - 2 - scf_crc], [frame[n - 2], frame[n - 1]]))
}

/// A DAB+ AU's PAD, from its leading data_stream_element (ID 4): (X-PAD
/// bytes, F-PAD). None when the AU has none.
pub fn dabplus_pad(au: &[u8]) -> Option<(&[u8], [u8; 2])> {
    if au.len() < 3 || au[0] >> 5 != 4 {
        return None;
    }
    let (start, len) = if au[1] == 255 { (3, 255 + au[2] as usize) } else { (2, au[1] as usize) };
    if len < 2 || au.len() < start + len {
        return None;
    }
    let pad = &au[start..start + len];
    Some((&pad[..len - 2], [pad[len - 2], pad[len - 1]]))
}

/// The dynamic label as it is assembled.
#[derive(Default)]
struct Dls {
    toggle: Option<bool>,
    charset: u8,
    segments: [Option<Vec<u8>>; 8],
    last: Option<usize>,
}

/// Reads X-PAD for the dynamic label, frame after frame.
#[derive(Default)]
pub struct PadDecoder {
    /// The application type of the last sub-field, which a sub-field with
    /// no contents indicator continues; and the last X-PAD's length.
    last_app: u8,
    last_len: usize,
    group: Vec<u8>,
    dls: Dls,
    /// The latest complete label (None until one is).
    pub label: Option<String>,
    pub groups_ok: u64,
    pub groups_bad: u64,
}

impl PadDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// One frame's PAD: `before` ends with the X-PAD (in the order sent),
    /// `fpad` its two F-PAD bytes. True when the label changed.
    pub fn push(&mut self, before: &[u8], fpad: [u8; 2]) -> bool {
        if fpad[0] >> 6 != 0 {
            return false; // an F-PAD type we do not know
        }
        let ind = (fpad[0] >> 4) & 3;
        let ci = fpad[1] & 0x02 != 0;
        // The X-PAD in logical order: the bytes before, last first.
        let r: Vec<u8> = before.iter().rev().copied().collect();
        // (application type, data, whether a contents indicator named it: a
        // start type continues rather than starts when none did).
        let mut subs: Vec<(u8, &[u8], bool)> = Vec::new();
        match ind {
            1 => {
                // Short X-PAD: 4 bytes, a contents indicator first if flagged.
                if r.len() < 4 {
                    return false;
                }
                if ci {
                    self.last_app = r[0] & 0x1f;
                    subs.push((self.last_app, &r[1..4], true));
                } else {
                    subs.push((self.last_app, &r[0..4], false));
                }
            }
            2 => {
                if ci {
                    let mut lens = Vec::new();
                    let mut k = 0;
                    while k < 4 && k < r.len() {
                        let (len, app) = (SUBFIELD[(r[k] >> 5) as usize], r[k] & 0x1f);
                        k += 1;
                        if app == 0 {
                            break; // the end marker
                        }
                        lens.push((app, len));
                    }
                    let mut at = k;
                    let mut total = 0;
                    for (app, len) in lens {
                        if at + len > r.len() {
                            break;
                        }
                        subs.push((app, &r[at..at + len], true));
                        self.last_app = app;
                        at += len;
                        total += len;
                    }
                    self.last_len = total;
                } else if self.last_len > 0 && self.last_len <= r.len() {
                    // The same length as last time, continuing the last sub-field's application.
                    subs.push((self.last_app, &r[..self.last_len], false));
                }
            }
            _ => return false,
        }
        let mut changed = false;
        for (app, data, named) in subs {
            match app {
                2 if named => {
                    self.group.clear();
                    self.group.extend_from_slice(data);
                }
                2 | 3 if !self.group.is_empty() => self.group.extend_from_slice(data),
                _ => continue,
            }
            changed |= self.take_groups();
        }
        changed
    }

    /// The length of the DLS data group in `self.group`, once its header is in.
    fn group_len(&self) -> Option<usize> {
        let g = &self.group;
        if g.len() < 2 {
            return None;
        }
        let c = g[0] & 0x10 != 0;
        let n = if !c {
            (g[0] & 0x0f) as usize + 1
        } else {
            match g[0] & 0x0f {
                1 => 0,                             // remove label
                2 => (g[1] & 0x0f) as usize + 1,    // DL Plus: field 3 says its length
                _ => 0,
            }
        };
        Some(2 + n + 2)
    }

    fn take_groups(&mut self) -> bool {
        let Some(len) = self.group_len() else { return false };
        if self.group.len() < len {
            return false;
        }
        let g: Vec<u8> = self.group.drain(..).take(len).collect();
        if crc16_ccitt(&g[..len - 2]) != u16::from_be_bytes([g[len - 2], g[len - 1]]) {
            self.groups_bad += 1;
            return false;
        }
        self.groups_ok += 1;
        self.segment(&g[..len - 2])
    }

    /// One dynamic label segment (CRC checked). True when the label changed.
    fn segment(&mut self, g: &[u8]) -> bool {
        let toggle = g[0] & 0x80 != 0;
        let (first, last, command) = (g[0] & 0x40 != 0, g[0] & 0x20 != 0, g[0] & 0x10 != 0);
        if command {
            // "Remove label" (§7.4.5.2): the display clears.
            if g[0] & 0x0f == 1 && self.label.as_deref() != Some("") {
                self.label = Some(String::new());
                self.dls = Dls::default();
                return true;
            }
            return false;
        }
        if self.dls.toggle != Some(toggle) {
            self.dls = Dls { toggle: Some(toggle), ..Default::default() };
        }
        let seg = if first {
            self.dls.charset = g[1] >> 4;
            0
        } else {
            ((g[1] >> 4) & 7) as usize
        };
        self.dls.segments[seg] = Some(g[2..].to_vec());
        if last {
            self.dls.last = Some(seg);
        }
        let Some(n) = self.dls.last else { return false };
        if !(0..=n).all(|i| self.dls.segments[i].is_some()) {
            return false;
        }
        let bytes: Vec<u8> = (0..=n).flat_map(|i| self.dls.segments[i].clone().unwrap()).collect();
        let text = label_text(&bytes, self.dls.charset);
        if self.label.as_deref() == Some(text.as_str()) {
            return false;
        }
        self.label = Some(text);
        true
    }
}

/// X-PAD for a dynamic label, frame after frame: the transmitter model's
/// half. `short`: short X-PAD (4 bytes a frame, as old DAB often sends);
/// otherwise variable size with a 16-byte sub-field and its contents
/// indicator list in every frame.
pub struct PadEncoder {
    short: bool,
    message: Vec<u8>,
    toggle: bool,
    /// The data groups of the message's segments, and where we are in them.
    groups: Vec<Vec<u8>>,
    next_group: usize,
    pending: Vec<u8>,
    /// Bytes of the group in `pending` already sent (0: its start is next).
    sent: usize,
}

impl PadEncoder {
    pub fn new(short: bool) -> Self {
        PadEncoder { short, message: Vec::new(), toggle: false, groups: Vec::new(), next_group: 0, pending: Vec::new(), sent: 0 }
    }

    /// The X-PAD field's length in each frame.
    pub fn xpad_len(&self) -> usize {
        if self.short {
            4
        } else {
            18 // CI, end marker, 16 bytes
        }
    }

    /// Send `text` (EBU Latin, ASCII here) from the next segment on.
    pub fn set_label(&mut self, text: &str) {
        if text.as_bytes() == self.message.as_slice() {
            return;
        }
        self.message = text.as_bytes().to_vec();
        self.toggle = !self.toggle;
        let chunks: Vec<&[u8]> = self.message.chunks(16).collect();
        let n = chunks.len();
        self.groups = chunks
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let (first, last) = (i == 0, i + 1 == n);
                let b0 = (self.toggle as u8) << 7 | (first as u8) << 6 | (last as u8) << 5 | (c.len() as u8 - 1);
                let b1 = if first { 0 } else { (i as u8) << 4 }; // charset 0 / SegNum
                let mut g = vec![b0, b1];
                g.extend_from_slice(c);
                let crc = crc16_ccitt(&g);
                g.extend(crc.to_be_bytes());
                g
            })
            .collect();
        self.next_group = 0;
        self.pending.clear();
        self.sent = 0;
    }

    /// This frame's X-PAD (in the order sent: reversed) and F-PAD.
    pub fn frame(&mut self) -> (Vec<u8>, [u8; 2]) {
        if self.pending.is_empty() && !self.groups.is_empty() {
            self.pending = self.groups[self.next_group].clone();
            self.next_group = (self.next_group + 1) % self.groups.len();
            self.sent = 0;
        }
        let starting = self.sent == 0;
        let room = if self.short { if starting { 3 } else { 4 } } else { 16 };
        let take = room.min(self.pending.len());
        let mut data: Vec<u8> = self.pending.drain(..take).collect();
        self.sent += take;
        data.resize(room, 0); // padding after a group's end
        let (logical, ci) = if self.short {
            if starting {
                (std::iter::once(2u8).chain(data).collect::<Vec<u8>>(), true)
            } else {
                (data, false)
            }
        } else {
            // A 16-byte sub-field (length code 4), then the end marker.
            let app = if starting { 2 } else { 3 };
            (std::iter::once(4u8 << 5 | app).chain(std::iter::once(0)).chain(data).collect(), true)
        };
        let ind = if self.short { 1u8 } else { 2 };
        (logical.into_iter().rev().collect(), [ind << 4, (ci as u8) << 1])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn round_trip(short: bool) {
        let mut enc = PadEncoder::new(short);
        let mut dec = PadDecoder::new();
        let text = "Shack MP2 Tone - now: 1 kHz left, 1.5 kHz right";
        enc.set_label(text);
        let mut got = None;
        for _ in 0..80 {
            let (x, f) = enc.frame();
            // Some audio bytes before the X-PAD, as in a frame.
            let mut before = vec![0x5a; 30];
            before.extend(x);
            if dec.push(&before, f) {
                got = dec.label.clone();
            }
        }
        assert_eq!(got.as_deref(), Some(text));
        assert_eq!(dec.groups_bad, 0);
        // A new message replaces it.
        enc.set_label("Second");
        for _ in 0..40 {
            let (x, f) = enc.frame();
            dec.push(&x, f);
        }
        assert_eq!(dec.label.as_deref(), Some("Second"));
    }

    #[test]
    fn short_x_pad_carries_a_label() {
        round_trip(true);
    }

    #[test]
    fn variable_x_pad_carries_a_label() {
        round_trip(false);
    }

    #[test]
    fn a_damaged_segment_is_refused_by_its_crc() {
        let mut enc = PadEncoder::new(false);
        enc.set_label("Hello");
        let (mut x, f) = enc.frame();
        let n = x.len();
        x[n - 4] ^= 0x40; // a character, in the sent order
        let mut dec = PadDecoder::new();
        assert!(!dec.push(&x, f));
        assert_eq!((dec.groups_bad, dec.label.clone()), (1, None));
    }

    #[test]
    fn the_scf_crc_length_follows_the_bit_rate() {
        assert_eq!(scf_crc_len(false, 128, false), 4);
        assert_eq!(scf_crc_len(false, 96, false), 2);
        assert_eq!(scf_crc_len(false, 48, true), 2);
        assert_eq!(scf_crc_len(true, 64, true), 4);
    }
}
