//! Programme Associated Data (EN 300 401 §7.4): the F-PAD at the end of
//! every DAB audio frame or DAB+ AU, the X-PAD before it, and from it the
//! dynamic label (DLS, §7.4.5.2): the programme text a receiver shows,
//! with its DL Plus tags (ETSI TS 102 980: title, artist and the rest);
//! and the MSC data groups of an X-PAD application (MOT, §7.4.5.1: the
//! data group length indicator, application type 1, then the group in
//! application types 12 and 13), which mot.rs turns into SlideShow's
//! pictures.
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

/// MOT data groups waiting for the caller at most: a caller that stops
/// asking loses the oldest, never memory.
const MAX_GROUPS: usize = 64;
/// The longest MSC data group a length indicator may announce (its field
/// is 14 bits).
const MAX_GROUP: usize = 16_383;

/// One DL Plus tag (TS 102 980 §7.3): its content type and its text, cut
/// from the label it was sent with.
#[derive(Clone, Debug, PartialEq)]
pub struct DlTag {
    pub content_type: u8,
    pub text: String,
}

/// The DL Plus state of the label now shown.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct DlPlus {
    /// Item toggle: changes when one item (a song) gives way to the next.
    pub item_toggle: bool,
    /// Item running: an item is playing now (false between items: talk).
    pub item_running: bool,
    pub tags: Vec<DlTag>,
}

impl DlPlus {
    /// The text of the first tag of a content type.
    pub fn get(&self, content_type: u8) -> Option<&str> {
        self.tags.iter().find(|t| t.content_type == content_type).map(|t| t.text.as_str())
    }
}

/// DL Plus tags as sent, before they are matched to their label.
#[derive(Clone, Debug, PartialEq)]
struct DlCommand {
    /// The toggle bit of the label the tags belong to.
    link: bool,
    item_toggle: bool,
    item_running: bool,
    /// (content type, start, length): character positions in the label.
    tags: Vec<(u8, usize, usize)>,
}

/// The dynamic label as it is assembled.
#[derive(Default)]
struct Dls {
    toggle: Option<bool>,
    charset: u8,
    segments: [Option<Vec<u8>>; 8],
    last: Option<usize>,
}

/// Reads X-PAD for the dynamic label and an application's MOT data
/// groups, frame after frame.
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
    /// The toggle bit of `label`, which DL Plus tags name.
    label_toggle: Option<bool>,
    /// DL Plus tags waiting for their label, and those of the label shown.
    dl_command: Option<DlCommand>,
    pub dl_plus: Option<DlPlus>,
    pub groups_ok: u64,
    pub groups_bad: u64,
    /// The X-PAD application type that starts a MOT data group (FIG 0/13
    /// says; 12 when it has not), the next continuing it.
    pub mot_app: u8,
    /// The data group length indicator (type 1) being read, and the length
    /// it gave for the next group.
    dgli: Vec<u8>,
    mot_len: Option<usize>,
    mot: Vec<u8>,
    /// Whole MSC data groups for the caller (`take_mot`), oldest first.
    mot_groups: std::collections::VecDeque<Vec<u8>>,
    /// Data groups dropped: no length known for them, or a full queue.
    pub mot_lost: u64,
}

impl PadDecoder {
    pub fn new() -> Self {
        PadDecoder { mot_app: 12, ..Default::default() }
    }

    /// The MOT data groups whole since the last call, oldest first. Their
    /// CRC is the data group's own, checked by mot.rs.
    pub fn take_mot(&mut self) -> Vec<Vec<u8>> {
        self.mot_groups.drain(..).collect()
    }

    /// One MOT sub-field's bytes: `start` when it begins a data group.
    fn mot_bytes(&mut self, data: &[u8], start: bool) {
        if start {
            self.mot.clear();
            if self.mot_len.is_none() {
                // A group with no length before it cannot be cut out.
                self.mot_lost += 1;
                return;
            }
        } else if self.mot.is_empty() {
            return; // a continuation whose start we never saw
        }
        let Some(len) = self.mot_len else { return };
        self.mot.extend_from_slice(data);
        if self.mot.len() >= len {
            let g: Vec<u8> = self.mot.drain(..).take(len).collect();
            self.mot.clear();
            self.mot_len = None;
            if self.mot_groups.len() >= MAX_GROUPS {
                self.mot_groups.pop_front();
                self.mot_lost += 1;
            }
            self.mot_groups.push_back(g);
        }
    }

    /// The data group length indicator's bytes (§7.4.5.1.1: 14 bits of
    /// length and a CRC, 4 bytes, which a short X-PAD splits over frames).
    fn dgli_bytes(&mut self, data: &[u8], start: bool) {
        if start {
            self.dgli.clear();
        } else if self.dgli.is_empty() {
            return;
        }
        self.dgli.extend_from_slice(data);
        if self.dgli.len() >= 4 {
            let d = &self.dgli[..4];
            if crc16_ccitt(&d[..2]) == u16::from_be_bytes([d[2], d[3]]) {
                let n = (u16::from_be_bytes([d[0], d[1]]) & 0x3fff) as usize;
                self.mot_len = (n > 0 && n <= MAX_GROUP).then_some(n);
            } else {
                self.mot_len = None;
            }
            self.dgli.clear();
        }
    }

    /// One frame's PAD: `before` ends with the X-PAD (in the order sent),
    /// `fpad` its two F-PAD bytes. True when the label changed.
    pub fn push(&mut self, before: &[u8], fpad: [u8; 2]) -> bool {
        self.push_in(before, fpad, false)
    }

    /// One DAB+ AU's PAD: `xpad` is the X-PAD exactly (its data_stream_element
    /// bounds it), which a frame without contents indicators continues whole.
    pub fn push_xpad(&mut self, xpad: &[u8], fpad: [u8; 2]) -> bool {
        self.push_in(xpad, fpad, true)
    }

    /// A variable X-PAD with no contents indicators continues the last
    /// sub-field (§7.4.2): in old DAB, where the X-PAD's end is not marked,
    /// it is as long as the previous X-PAD was, its indicators included; in
    /// DAB+ it is the whole X-PAD. Taking only the previous sub-fields'
    /// length, as the first version did, cut every such frame short: no
    /// programme text and no slide from 9A's or 9B's DAB+ services, whose
    /// encoders send most frames that way.
    fn push_in(&mut self, before: &[u8], fpad: [u8; 2], exact: bool) -> bool {
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
                    for (app, len) in lens {
                        if at + len > r.len() {
                            break;
                        }
                        subs.push((app, &r[at..at + len], true));
                        self.last_app = app;
                        at += len;
                    }
                    self.last_len = at;
                } else {
                    // Continuing the last sub-field's application (push_in's docs).
                    let n = if exact { r.len() } else { self.last_len };
                    if n > 0 && n <= r.len() {
                        subs.push((self.last_app, &r[..n], false));
                    }
                }
            }
            _ => return false,
        }
        let mut changed = false;
        let mot = self.mot_app;
        for (app, data, named) in subs {
            match app {
                1 => {
                    self.dgli_bytes(data, named);
                    continue;
                }
                a if a == mot => {
                    self.mot_bytes(data, named);
                    continue;
                }
                a if a == mot.wrapping_add(1) => {
                    self.mot_bytes(data, false);
                    continue;
                }
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
            match g[0] & 0x0f {
                // "Remove label" (§7.4.5.2): the display clears.
                1 if self.label.as_deref() != Some("") => {
                    self.label = Some(String::new());
                    self.label_toggle = None;
                    self.dls = Dls::default();
                    self.dl_plus = None;
                    return true;
                }
                2 => return self.dl_plus_command(g),
                _ => {}
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
        if self.label.as_deref() == Some(text.as_str()) && self.label_toggle == Some(toggle) {
            return false;
        }
        self.label = Some(text);
        self.label_toggle = Some(toggle);
        // Tags sent before their label arrived apply now; tags of another
        // label do not.
        self.dl_plus = None;
        self.apply_dl_plus();
        true
    }

    /// A DL Plus command (TS 102 980 §7): field 2's top bit links it to the
    /// label with that toggle bit; only the tags command (CId 0) is read.
    fn dl_plus_command(&mut self, g: &[u8]) -> bool {
        if g.len() < 3 || g[2] >> 4 != 0 {
            return false;
        }
        let link = g[1] & 0x80 != 0;
        let cb = g[2] & 0x0f;
        let n = (cb & 3) as usize + 1;
        let mut tags = Vec::new();
        for t in g[3..].chunks_exact(3).take(n) {
            let ct = t[0] & 0x7f;
            if ct != 0 {
                tags.push((ct, (t[1] & 0x7f) as usize, (t[2] & 0x7f) as usize + 1));
            }
        }
        let cmd = DlCommand { link, item_toggle: cb & 8 != 0, item_running: cb & 4 != 0, tags };
        if self.dl_command.as_ref() == Some(&cmd) {
            return false;
        }
        self.dl_command = Some(cmd);
        self.apply_dl_plus()
    }

    /// Cut the tags out of the label they name. True when what is shown changed.
    fn apply_dl_plus(&mut self) -> bool {
        let (Some(cmd), Some(label)) = (&self.dl_command, &self.label) else { return false };
        if self.label_toggle != Some(cmd.link) {
            return false;
        }
        let chars: Vec<char> = label.chars().collect();
        let tags = cmd
            .tags
            .iter()
            .filter(|(_, at, len)| at + len <= chars.len())
            .map(|&(ct, at, len)| DlTag { content_type: ct, text: chars[at..at + len].iter().collect::<String>().trim().to_string() })
            .collect();
        let new = DlPlus { item_toggle: cmd.item_toggle, item_running: cmd.item_running, tags };
        if self.dl_plus.as_ref() == Some(&new) {
            return false;
        }
        self.dl_plus = Some(new);
        true
    }
}

/// X-PAD for a dynamic label, frame after frame: the transmitter model's
/// half. `short`: short X-PAD (4 bytes a frame, as old DAB often sends);
/// otherwise variable size with a 16-byte sub-field and its contents
/// indicator list in every frame.
pub struct PadEncoder {
    short: bool,
    /// MSC data groups to send in X-PAD (MOT, application types 12/13
    /// after a length indicator), each sub-field this many bytes: sent
    /// before any label segment while there are some.
    mot: std::collections::VecDeque<Vec<u8>>,
    mot_subfield: usize,
    mot_sent: usize,
    /// Whether this frame's turn is the label's: with both to send, frames
    /// alternate, so a carousel never starves the label.
    label_turn: bool,
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
        PadEncoder {
            short,
            mot: Default::default(),
            mot_subfield: 0,
            mot_sent: 0,
            label_turn: false,
            message: Vec::new(),
            toggle: false,
            groups: Vec::new(),
            next_group: 0,
            pending: Vec::new(),
            sent: 0,
        }
    }

    /// A variable-size encoder that can also carry MOT data groups, in
    /// sub-fields of `subfield` bytes (one of 4, 6, 8, 12, 16, 24, 32, 48).
    pub fn with_mot(subfield: usize) -> Self {
        assert!(SUBFIELD.contains(&subfield), "an X-PAD sub-field length");
        PadEncoder { mot_subfield: subfield, ..Self::new(false) }
    }

    /// Queue MSC data groups (whole, CRC included) for the X-PAD.
    pub fn queue_groups(&mut self, groups: impl IntoIterator<Item = Vec<u8>>) {
        self.mot.extend(groups);
    }

    /// Data groups still to send.
    pub fn groups_queued(&self) -> usize {
        self.mot.len()
    }

    /// The X-PAD field's length in each frame: the most a frame carries.
    pub fn xpad_len(&self) -> usize {
        if self.short {
            4
        } else {
            // CI, end marker, 16 bytes; or two CIs, an end marker, the
            // length indicator's 4 bytes and a MOT sub-field.
            18.max(if self.mot_subfield > 0 { 3 + 4 + self.mot_subfield } else { 0 })
        }
    }

    /// This frame's X-PAD for a queued data group, in logical order, or None.
    fn mot_frame(&mut self) -> Option<Vec<u8>> {
        let n = self.mot_subfield;
        let g = self.mot.front()?;
        let code = SUBFIELD.iter().position(|&l| l == n).unwrap() as u8;
        let starting = self.mot_sent == 0;
        let mut data: Vec<u8> = g[self.mot_sent..(self.mot_sent + n).min(g.len())].to_vec();
        self.mot_sent += data.len();
        data.resize(n, 0);
        let mut x = Vec::new();
        if starting {
            let len = g.len() as u16 & 0x3fff;
            x.extend([0u8 << 5 | 1, code << 5 | 12, 0]);
            x.extend(len.to_be_bytes());
            x.extend(crc16_ccitt(&len.to_be_bytes()).to_be_bytes());
        } else {
            x.extend([code << 5 | 13, 0]);
        }
        x.extend(data);
        if self.mot_sent >= g.len() {
            self.mot.pop_front();
            self.mot_sent = 0;
        }
        Some(x)
    }

    /// Send `text` (EBU Latin, ASCII here) from the next segment on. True
    /// when it is a new label.
    pub fn set_label(&mut self, text: &str) -> bool {
        if text.as_bytes() == self.message.as_slice() {
            return false;
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
        true
    }

    /// Send DL Plus tags (TS 102 980 §7.3) with the label now set:
    /// (content type, first character, length) each, at most four.
    pub fn set_dl_plus(&mut self, tags: &[(u8, u8, u8)], item_toggle: bool, item_running: bool) {
        let n = tags.len().clamp(1, 4);
        let mut field = vec![(item_toggle as u8) << 3 | (item_running as u8) << 2 | (n as u8 - 1)];
        for &(ct, at, len) in tags.iter().take(4) {
            field.extend([ct & 0x7f, at & 0x7f, len.saturating_sub(1) & 0x7f]);
        }
        // Prefix: C set, command 2 (DL Plus); field 2's top bit links it to
        // the label's toggle, field 3 is the field's length less one.
        let mut g = vec![0x10 | 2, (self.toggle as u8) << 7 | (field.len() as u8 - 1)];
        g.extend(field);
        let crc = crc16_ccitt(&g);
        g.extend(crc.to_be_bytes());
        self.groups.retain(|g| g[0] & 0x10 == 0);
        self.groups.push(g);
    }

    /// This frame's X-PAD (in the order sent: reversed) and F-PAD.
    pub fn frame(&mut self) -> (Vec<u8>, [u8; 2]) {
        self.label_turn = !self.label_turn && !self.groups.is_empty();
        if !self.short && !self.label_turn {
            if let Some(x) = self.mot_frame() {
                return (x.into_iter().rev().collect(), [2 << 4, 2]);
            }
        }
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

    /// A DLS data group for `text` (one segment, first and last).
    fn dls_group(text: &[u8]) -> Vec<u8> {
        let mut g = vec![0x60 | (text.len() as u8 - 1), 0];
        g.extend_from_slice(text);
        let crc = crc16_ccitt(&g);
        g.extend(crc.to_be_bytes());
        g
    }

    #[test]
    fn x_pad_frames_without_contents_indicators_continue_the_group_whole() {
        // 9A's DAB+ X-PAD: one 10-byte X-PAD with a CI (8 data bytes), the
        // rest of the group in 10-byte X-PADs with none.
        let g = dls_group(b"Nation 60s - Ha"); // 2 + 15 + 2 = 19 bytes
        let mut frames = Vec::new();
        let mut first = vec![2 << 5 | 2, 0];
        first.extend(&g[..8]);
        frames.push((first, [0x20, 0x02]));
        for chunk in g[8..].chunks(10) {
            let mut c = chunk.to_vec();
            c.resize(10, 0);
            frames.push((c, [0x20, 0x00]));
        }
        let rev = |x: &[u8]| x.iter().rev().copied().collect::<Vec<u8>>();
        let mut dec = PadDecoder::new();
        for (x, f) in &frames {
            dec.push_xpad(&rev(x), *f);
        }
        assert_eq!(dec.label.as_deref(), Some("Nation 60s - Ha"));
        // Old DAB: audio bytes before the X-PAD, so the continuation's
        // length is the previous X-PAD's, its CI list included.
        let mut dec = PadDecoder::new();
        for (x, f) in &frames {
            let mut before = vec![0x77; 40];
            before.extend(rev(x));
            dec.push(&before, *f);
        }
        assert_eq!(dec.label.as_deref(), Some("Nation 60s - Ha"));
    }

    #[test]
    fn dl_plus_tags_name_the_title_and_artist_in_the_label() {
        let mut enc = PadEncoder::new(false);
        let mut dec = PadDecoder::new();
        let text = "Now: Blue Monday by New Order";
        enc.set_label(text);
        // ITEM.TITLE at 5 for 11, ITEM.ARTIST at 20 for 9; an item running.
        enc.set_dl_plus(&[(1, 5, 11), (4, 20, 9)], true, true);
        for _ in 0..40 {
            let (x, f) = enc.frame();
            dec.push(&x, f);
        }
        assert_eq!(dec.label.as_deref(), Some(text));
        let d = dec.dl_plus.clone().expect("DL Plus");
        assert_eq!((d.get(1), d.get(4)), (Some("Blue Monday"), Some("New Order")));
        assert!(d.item_running && d.item_toggle);
        // A new label with no tags of its own clears them: the old tags
        // name another label's characters.
        enc.set_label("Traffic and travel");
        for _ in 0..40 {
            let (x, f) = enc.frame();
            dec.push(&x, f);
        }
        assert_eq!(dec.label.as_deref(), Some("Traffic and travel"));
        assert_eq!(dec.dl_plus, None);
    }

    #[test]
    fn the_scf_crc_length_follows_the_bit_rate() {
        assert_eq!(scf_crc_len(false, 128, false), 4);
        assert_eq!(scf_crc_len(false, 96, false), 2);
        assert_eq!(scf_crc_len(false, 48, true), 2);
        assert_eq!(scf_crc_len(true, 64, true), 4);
    }
}
