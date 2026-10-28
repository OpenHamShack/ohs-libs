//! Packet data on a DMR slot (TS 102 361-1 §9.3.2-9.3.6, TS 102 361-3):
//! a data header, then blocks at rate 1/2 (BPTC(196,96)) or rate 3/4 (a
//! trellis code), the last ending in a CRC-32. What a listener wants from
//! it is short messages: an SMS is an IP datagram (service access point 4)
//! carrying UDP, whose payload holds the text, in UTF-16 from Motorola's
//! text messaging (port 4007) and in UTF-16 or 8-bit text from others.
//! The formats after UDP are vendors' own, so the text is found rather
//! than parsed: the longest run of plain characters in either coding.
//!
//! Rate 1 data, confirmed data's per-block CRC-9 and response packets are
//! not decoded (a confirmed message is still read, its blocks' serial
//! numbers stripped, and the CRC-32 decides).

use super::bptc::{decode196, encode196};
use super::fec::{crc32_dmr, crc_ccitt};

pub const MASK_DATA_HEADER: u16 = 0xCCCC;
pub const MASK_CSBK: u16 = 0xA5A5;
pub const MASK_PI_HEADER: u16 = 0x6969;

/// Data packet formats (DPF).
pub const DPF_UNCONFIRMED: u8 = 2;
pub const DPF_CONFIRMED: u8 = 3;
pub const DPF_SHORT_DEFINED: u8 = 0xD;
/// Service access point: IP-based packet data.
pub const SAP_IP: u8 = 4;

fn bits(bytes: &[u8]) -> Vec<u8> {
    bytes.iter().flat_map(|&b| (0..8).rev().map(move |i| (b >> i) & 1)).collect()
}

fn bytes(bits: &[u8]) -> Vec<u8> {
    bits.chunks(8).map(|c| c.iter().fold(0, |a, &b| (a << 1) | b)).collect()
}

/// A 12-octet burst payload (data header, CSBK, PI header, rate 1/2 block)
/// from its info bits, with BPTC's corrections.
pub fn decode_block12(info: &[u8; 196]) -> ([u8; 12], u32) {
    let (d, r) = decode196(info);
    (bytes(&d).try_into().expect("12"), r.corrected)
}

pub fn encode_block12(b: &[u8; 12]) -> [u8; 196] {
    encode196(&bits(b).try_into().expect("96"))
}

/// Check a header-like block's CRC-CCITT under its mask.
pub fn crc_ok(b: &[u8; 12], mask: u16) -> bool {
    let want = crc_ccitt(&b[..10]) ^ mask;
    u16::from_be_bytes([b[10], b[11]]) == want
}

pub fn put_crc(b: &mut [u8; 12], mask: u16) {
    let c = crc_ccitt(&b[..10]) ^ mask;
    b[10..].copy_from_slice(&c.to_be_bytes());
}

/// A data header's fields.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Header {
    pub group: bool,
    pub dpf: u8,
    pub sap: u8,
    pub pad: u8,
    pub dst: u32,
    pub src: u32,
    /// Blocks that follow the header.
    pub blocks: u8,
}

impl Header {
    pub fn parse(b: &[u8; 12]) -> Option<Header> {
        if !crc_ok(b, MASK_DATA_HEADER) {
            return None;
        }
        let dpf = b[0] & 0xF;
        let blocks = match dpf {
            DPF_UNCONFIRMED | DPF_CONFIRMED => b[8] & 0x7F,
            DPF_SHORT_DEFINED => (b[0] & 0x30) | (b[1] & 0xF),
            _ => 0,
        };
        let pad = if matches!(dpf, DPF_UNCONFIRMED | DPF_CONFIRMED) { ((b[0] & 0x10) | (b[1] & 0xF)) as u8 } else { 0 };
        Some(Header {
            group: b[0] & 0x80 != 0,
            dpf,
            sap: b[1] >> 4,
            pad,
            dst: u32::from_be_bytes([0, b[2], b[3], b[4]]),
            src: u32::from_be_bytes([0, b[5], b[6], b[7]]),
            blocks,
        })
    }

    /// An unconfirmed header, for the test transmitter.
    pub fn build(&self) -> [u8; 12] {
        let mut b = [0u8; 12];
        b[0] = (self.group as u8) << 7 | (self.pad & 0x10) | (self.dpf & 0xF);
        b[1] = (self.sap << 4) | (self.pad & 0xF);
        b[2..5].copy_from_slice(&self.dst.to_be_bytes()[1..]);
        b[5..8].copy_from_slice(&self.src.to_be_bytes()[1..]);
        b[8] = 0x80 | (self.blocks & 0x7F); // F: the full message
        put_crc(&mut b, MASK_DATA_HEADER);
        b
    }
}

// ---------------------------------------------------------------- rate 3/4

/// Where each of the 98 dibits goes after deinterleaving.
const INTERLEAVE: [usize; 98] = [
    0, 1, 8, 9, 16, 17, 24, 25, 32, 33, 40, 41, 48, 49, 56, 57, 64, 65, 72, 73, 80, 81, 88, 89, 96, 97, 2, 3, 10, 11, 18, 19,
    26, 27, 34, 35, 42, 43, 50, 51, 58, 59, 66, 67, 74, 75, 82, 83, 90, 91, 4, 5, 12, 13, 20, 21, 28, 29, 36, 37, 44, 45, 52,
    53, 60, 61, 68, 69, 76, 77, 84, 85, 92, 93, 6, 7, 14, 15, 22, 23, 30, 31, 38, 39, 46, 47, 54, 55, 62, 63, 70, 71, 78, 79,
    86, 87, 94, 95,
];

/// The constellation point for (state, tribit): the trellis's next output.
const ENCODE: [u8; 64] = [
    0, 8, 4, 12, 2, 10, 6, 14, 4, 12, 2, 10, 6, 14, 0, 8, 1, 9, 5, 13, 3, 11, 7, 15, 5, 13, 3, 11, 7, 15, 1, 9, 3, 11, 7, 15,
    1, 9, 5, 13, 7, 15, 1, 9, 5, 13, 3, 11, 2, 10, 6, 14, 0, 8, 4, 12, 6, 14, 0, 8, 4, 12, 2, 10,
];

/// Each point's two symbols.
const POINTS: [(i8, i8); 16] = [
    (1, -1),
    (-1, -1),
    (3, -3),
    (-3, -3),
    (-3, -1),
    (3, -1),
    (-1, -3),
    (1, -3),
    (-3, 3),
    (3, 3),
    (-1, 1),
    (1, 1),
    (1, 3),
    (-1, 3),
    (3, 1),
    (-3, 1),
];

fn sym(d: (u8, u8)) -> i8 {
    match d {
        (0, 1) => 3,
        (0, 0) => 1,
        (1, 0) => -1,
        _ => -3,
    }
}

fn dibit(s: i8) -> (u8, u8) {
    match s {
        3 => (0, 1),
        1 => (0, 0),
        -1 => (1, 0),
        _ => (1, 1),
    }
}

/// Rate 3/4: 196 info bits to 18 octets, by Viterbi over the 8-state
/// trellis with the symbols' squared distance; with the symbols that
/// disagreed with the path chosen (an error count).
pub fn decode_rate34(info: &[u8; 196]) -> ([u8; 18], u32) {
    let mut ds = [0i8; 98];
    for i in 0..98 {
        ds[INTERLEAVE[i]] = sym((info[2 * i], info[2 * i + 1]));
    }
    const INF: u32 = u32::MAX / 2;
    let mut cost = [INF; 8];
    cost[0] = 0;
    let mut back = vec![[0u8; 8]; 49];
    for (t, pair) in ds.chunks(2).enumerate() {
        let mut next = [INF; 8];
        for s in 0..8 {
            if cost[s] >= INF {
                continue;
            }
            for tri in 0..8 {
                let (a, b) = POINTS[ENCODE[s * 8 + tri] as usize];
                let d = ((a - pair[0]) as i32).pow(2) + ((b - pair[1]) as i32).pow(2);
                let c = cost[s] + d as u32;
                if c < next[tri] {
                    next[tri] = c;
                    back[t][tri] = s as u8;
                }
            }
        }
        cost = next;
    }
    // The tail tribit is 0: the path ends in state 0.
    let mut path = [0u8; 49];
    let mut s = 0u8;
    for t in (0..49).rev() {
        path[t] = s;
        s = back[t][s as usize];
    }
    let mut out = Vec::with_capacity(144);
    let mut errs = 0;
    let mut state = 0usize;
    for t in 0..49 {
        let tri = path[t] as usize;
        let (a, b) = POINTS[ENCODE[state * 8 + tri] as usize];
        errs += (a != ds[2 * t]) as u32 + (b != ds[2 * t + 1]) as u32;
        state = tri;
        if t < 48 {
            out.extend_from_slice(&[(tri >> 2) as u8 & 1, (tri >> 1) as u8 & 1, tri as u8 & 1]);
        }
    }
    (bytes(&out).try_into().expect("18"), errs)
}

pub fn encode_rate34(b: &[u8; 18]) -> [u8; 196] {
    let bs = bits(b);
    let mut ds = [0i8; 98];
    let mut state = 0usize;
    for t in 0..49 {
        let tri = if t < 48 { (bs[3 * t] << 2 | bs[3 * t + 1] << 1 | bs[3 * t + 2]) as usize } else { 0 };
        let (a, b) = POINTS[ENCODE[state * 8 + tri] as usize];
        ds[2 * t] = a;
        ds[2 * t + 1] = b;
        state = tri;
    }
    let mut info = [0u8; 196];
    for i in 0..98 {
        let (x, y) = dibit(ds[INTERLEAVE[i]]);
        info[2 * i] = x;
        info[2 * i + 1] = y;
    }
    info
}

// ---------------------------------------------------------------- messages

/// A data message being gathered, block by block.
#[derive(Clone, Debug)]
pub struct Message {
    pub header: Header,
    pub octets: Vec<u8>,
    pub got: u8,
}

impl Message {
    pub fn new(header: Header) -> Self {
        Message { header, octets: Vec::new(), got: 0 }
    }

    /// Add a block's octets (12 at rate 1/2, 18 at rate 3/4); true when the
    /// header's count has arrived.
    pub fn push(&mut self, block: &[u8]) -> bool {
        // A confirmed block opens with its serial number and CRC-9.
        let skip = if self.header.dpf == DPF_CONFIRMED { 2 } else { 0 };
        self.octets.extend_from_slice(&block[skip.min(block.len())..]);
        self.got += 1;
        self.got >= self.header.blocks
    }

    /// The user data, if the CRC-32 holds.
    pub fn payload(&self) -> Option<Vec<u8>> {
        let n = self.octets.len();
        if n < 4 + self.header.pad as usize || n % 2 != 0 {
            return None;
        }
        let crc = u32::from_le_bytes(self.octets[n - 4..].try_into().expect("4"));
        if crc32_dmr(&self.octets[..n - 4]) != crc {
            return None;
        }
        Some(self.octets[..n - 4 - self.header.pad as usize].to_vec())
    }
}

/// A finished message, as the decoder reports it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Received {
    pub text: Option<String>,
    pub ports: Option<(u16, u16)>,
    pub octets: usize,
}

/// What a message carries: an IPv4/UDP datagram's ports and payload where
/// it is one, and the text in it.
pub fn read(header: &Header, payload: &[u8]) -> Received {
    let mut body = payload;
    let mut ports = None;
    if header.sap == SAP_IP && payload.len() >= 28 && payload[0] >> 4 == 4 {
        let ihl = (payload[0] & 0xF) as usize * 4;
        if payload[9] == 17 && payload.len() >= ihl + 8 {
            let u = &payload[ihl..];
            ports = Some((u16::from_be_bytes([u[0], u[1]]), u16::from_be_bytes([u[2], u[3]])));
            body = &u[8..];
        }
    }
    Received { text: find_text(body), ports, octets: payload.len() }
}

/// A UTF-16 code unit that is plainly text: Latin, Greek, Cyrillic. (Not
/// any printable BMP character: two ASCII letters read as one unit are a
/// CJK ideograph, so 8-bit text would pass as UTF-16 rubbish.)
fn plain16(u: u16) -> bool {
    (0x20..0x7F).contains(&u) || (0xA0..0x250).contains(&u) || (0x370..0x530).contains(&u)
}

fn plain8(b: u8) -> bool {
    (0x20..0x7F).contains(&b)
}

/// The longest run of plain text, read as UTF-16 (either byte order, either
/// alignment) and as 8-bit; at least two characters.
pub fn find_text(b: &[u8]) -> Option<String> {
    let mut best = String::new();
    for le in [true, false] {
        for start in 0..2 {
            let mut run = String::new();
            for p in b[start.min(b.len())..].chunks_exact(2) {
                let u = if le { u16::from_le_bytes([p[0], p[1]]) } else { u16::from_be_bytes([p[0], p[1]]) };
                if plain16(u) {
                    run.push(char::from_u32(u as u32).unwrap_or('?'));
                } else {
                    if run.chars().count() > best.chars().count() {
                        best = std::mem::take(&mut run);
                    }
                    run.clear();
                }
            }
            if run.chars().count() > best.chars().count() {
                best = run;
            }
        }
    }
    let mut run = String::new();
    for &c in b {
        if plain8(c) {
            run.push(c as char);
        } else {
            if run.len() > best.chars().count() {
                best = std::mem::take(&mut run);
            }
            run.clear();
        }
    }
    if run.len() > best.chars().count() {
        best = run;
    }
    let best = best.trim().to_string();
    (best.chars().count() >= 2).then_some(best)
}

/// An SMS as a Motorola radio sends one, for the test transmitter: IPv4
/// and UDP to port 4007, then the text messaging header and UTF-16LE text.
pub fn sms_datagram(src_ip: [u8; 4], dst_ip: [u8; 4], text: &str) -> Vec<u8> {
    let utf16: Vec<u8> = text.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
    let mut tms = vec![0u8, 0, 0xA0, 0x00, 0x01, 0x04, 0x0D, 0x00, 0x0A, 0x00];
    tms.extend_from_slice(&utf16);
    let n = tms.len() as u16 - 2;
    tms[..2].copy_from_slice(&n.to_be_bytes());
    let udp_len = 8 + tms.len() as u16;
    let total = 20 + udp_len;
    let mut ip = vec![0x45, 0, 0, 0, 0, 1, 0, 0, 64, 17, 0, 0];
    ip[2..4].copy_from_slice(&total.to_be_bytes());
    ip.extend_from_slice(&src_ip);
    ip.extend_from_slice(&dst_ip);
    let sum = ip.chunks(2).fold(0u32, |a, w| a + u16::from_be_bytes([w[0], w[1]]) as u32);
    let sum = !(((sum & 0xFFFF) + (sum >> 16)) as u16);
    ip[10..12].copy_from_slice(&sum.to_be_bytes());
    ip.extend_from_slice(&4007u16.to_be_bytes());
    ip.extend_from_slice(&4007u16.to_be_bytes());
    ip.extend_from_slice(&udp_len.to_be_bytes());
    ip.extend_from_slice(&[0, 0]);
    ip.extend_from_slice(&tms);
    ip
}

/// A whole unconfirmed message at rate 1/2 or 3/4: its header (with the
/// block count and pad filled in) and blocks, the last ending in the
/// CRC-32.
pub fn build_message(mut header: Header, payload: &[u8], rate34: bool) -> ([u8; 12], Vec<Vec<u8>>) {
    let per = if rate34 { 18 } else { 12 };
    let mut n = payload.len() + 4;
    let blocks = n.div_ceil(per);
    let pad = blocks * per - n;
    n += pad;
    let mut octets = payload.to_vec();
    octets.resize(n - 4, 0);
    let crc = crc32_dmr(&octets);
    octets.extend_from_slice(&crc.to_le_bytes());
    header.dpf = DPF_UNCONFIRMED;
    header.pad = pad as u8;
    header.blocks = blocks as u8;
    (header.build(), octets.chunks(per).map(|c| c.to_vec()).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rate34_round_trips_and_corrects_symbol_errors() {
        let b: [u8; 18] = std::array::from_fn(|i| (i as u8).wrapping_mul(37) ^ 0x5A);
        let info = encode_rate34(&b);
        assert_eq!(decode_rate34(&info), (b, 0));
        let mut rx = info;
        // A noisy slicer's errors: a symbol read as its neighbour, in three
        // places far apart.
        for i in [5usize, 45, 85] {
            let s = sym((rx[2 * i], rx[2 * i + 1]));
            let (a, b) = dibit(match s {
                3 => 1,
                -3 => -1,
                x => -x,
            });
            rx[2 * i] = a;
            rx[2 * i + 1] = b;
        }
        let (got, errs) = decode_rate34(&rx);
        assert_eq!(got, b);
        assert_eq!(errs, 3);
    }

    #[test]
    fn rate34_puts_its_first_point_where_the_firmware_does() {
        // MMDVMHost: tribit 1 from state 0 is point 8, symbols (-3, +3),
        // and dibit 0 is the interleave table's first entry.
        let mut b = [0u8; 18];
        b[0] = 0b0010_0000;
        let info = encode_rate34(&b);
        assert_eq!((info[0], info[1], info[2], info[3]), (1, 1, 0, 1));
    }

    #[test]
    fn an_sms_survives_header_blocks_and_crc_and_its_text_is_found() {
        let dg = sms_datagram([12, 0, 0, 1], [12, 0, 0, 2], "Hello from GM4BRI");
        let h = Header { group: false, dpf: 0, sap: SAP_IP, pad: 0, dst: 2, src: 1, blocks: 0 };
        for rate34 in [false, true] {
            let (hb, blocks) = build_message(h, &dg, rate34);
            let h2 = Header::parse(&hb).unwrap();
            let mut m = Message::new(h2);
            let mut done = false;
            for b in &blocks {
                done = m.push(b);
            }
            assert!(done);
            let p = m.payload().expect("crc");
            assert_eq!(p, dg);
            let r = read(&h2, &p);
            assert_eq!(r.text.as_deref(), Some("Hello from GM4BRI"));
            assert_eq!(r.ports, Some((4007, 4007)));
        }
    }

    #[test]
    fn text_is_found_in_8_bit_and_not_invented_from_noise() {
        assert_eq!(find_text(b"\x01\x02CQ CQ de GM4BRI\x00\x7f").as_deref(), Some("CQ CQ de GM4BRI"));
        assert_eq!(find_text(&[0xFF, 0x01, 0xFE, 0x02]), None);
    }
}
