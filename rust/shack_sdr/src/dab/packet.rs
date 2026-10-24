//! Packet mode (EN 300 401 §5.3.2): a data sub-channel's bytes cut into
//! packets of 24, 48, 72 or 96 bytes, each with its address and CRC, and
//! the packets of one address joined back into MSC data groups.
//!
//! **A stream, not frames.** The sub-channel is read as one run of bytes
//! carried over from logical frame to logical frame: a packet need not
//! start at a frame's start. A 96-byte packet on an 8 kbit/s sub-channel
//! (24 bytes a frame) spans four frames, and the packet FEC's application
//! data tables (§5.3.5: 2 256 bytes of packets, then the RS packets) are
//! not frame-aligned either. A first version read each frame from its
//! start, as the transmitter model packs them; BBC National's SPI on 12B,
//! with FEC, showed why not.
//!
//! **Resynchronising.** Each packet's length is in its first byte, so one
//! damaged header would cut every packet after it wrongly; a packet whose
//! CRC fails is therefore dropped one byte at a time until a position
//! whose header and CRC agree is found (a CRC-16 holds by chance once in
//! 65 536 tries). A failure breaks the group in progress (the continuity
//! index says so too). Padding packets (address 0) are skipped. The FEC's
//! RS packets (address 1022, 24 bytes) carry parity in place of a packet
//! CRC, so they are recognised by address and length alone and stepped
//! over whole: taken as damaged packets they cost BBC National's SPI on
//! 12B every packet after each FEC run while the reader resynchronised
//! byte by byte (all 2 399 data packets of its 62 s capture are whole
//! without them). The FEC is not applied: a packet the RS code could
//! have mended is lost, and its group with it, until the carousel sends
//! it again.

use super::coding::crc16_ccitt;

/// The longest data group we join (the data group's own limit, 8 191 bytes
/// of data field, and its headers).
const MAX_GROUP: usize = 8_400;
/// The packet FEC's RS packets' address (§5.3.5).
const FEC_ADDRESS: u16 = 1022;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct PacketCounts {
    pub packets: u64,
    pub crc_errors: u64,
    pub groups: u64,
    pub broken: u64,
    /// Bytes stepped over looking for the next packet.
    pub skipped: u64,
}

/// Joins one address's packets into data groups.
pub struct PacketReader {
    pub address: u16,
    buf: Vec<u8>,
    /// Whether the last position tried failed: errors are counted once per
    /// loss of step, not once per byte stepped over.
    lost: bool,
    group: Vec<u8>,
    next_ci: Option<u8>,
    pub counts: PacketCounts,
}

impl PacketReader {
    pub fn new(address: u16) -> Self {
        PacketReader { address, buf: Vec::new(), lost: false, group: Vec::new(), next_ci: None, counts: PacketCounts::default() }
    }

    /// The sub-channel's bytes stopped (a gap in the samples): what is
    /// carried over can no longer be joined to what comes next.
    pub fn reset(&mut self) {
        self.buf.clear();
        self.group.clear();
        self.next_ci = None;
    }

    /// One logical frame of the sub-channel: the data groups it completed.
    pub fn push_frame(&mut self, lf: &[u8]) -> Vec<Vec<u8>> {
        self.buf.extend_from_slice(lf);
        let mut out = Vec::new();
        let mut k = 0;
        while k + 3 <= self.buf.len() {
            let len = [24, 48, 72, 96][(self.buf[k] >> 6) as usize];
            if k + len > self.buf.len() {
                break; // the rest of this packet comes with the next frame
            }
            if len == 24 && u16::from_be_bytes([self.buf[k] & 3, self.buf[k + 1]]) == FEC_ADDRESS {
                k += 24; // RS parity (module docs)
                continue;
            }
            let p = &self.buf[k..k + len];
            if crc16_ccitt(&p[..len - 2]) != u16::from_be_bytes([p[len - 2], p[len - 1]]) {
                if !self.lost {
                    self.lost = true;
                    self.counts.crc_errors += 1;
                    if !self.group.is_empty() {
                        self.group.clear();
                        self.counts.broken += 1;
                    }
                    self.next_ci = None;
                }
                self.counts.skipped += 1;
                k += 1;
                continue;
            }
            self.lost = false;
            self.counts.packets += 1;
            k += len;
            let p = p.to_vec();
            if let Some(g) = self.packet(&p) {
                out.push(g);
            }
        }
        self.buf.drain(..k);
        out
    }

    /// One packet whose CRC holds: the group it completes, if it does.
    fn packet(&mut self, p: &[u8]) -> Option<Vec<u8>> {
        let address = u16::from_be_bytes([p[0] & 3, p[1]]);
        if address != self.address || address == 0 {
            return None;
        }
        let ci = (p[0] >> 4) & 3;
        let (first, last) = (p[0] & 0x08 != 0, p[0] & 0x04 != 0);
        let useful = ((p[2] & 0x7f) as usize).min(p.len() - 5);
        let data = &p[3..3 + useful];
        if self.next_ci.is_some_and(|n| n != ci) && !self.group.is_empty() {
            self.group.clear();
            self.counts.broken += 1;
        }
        self.next_ci = Some((ci + 1) & 3);
        if first {
            self.group.clear();
            self.group.extend_from_slice(data);
        } else if !self.group.is_empty() {
            self.group.extend_from_slice(data);
        } else {
            return None; // the middle of a group whose start we missed
        }
        if self.group.len() > MAX_GROUP {
            self.group.clear();
            self.counts.broken += 1;
            return None;
        }
        if last {
            self.counts.groups += 1;
            return Some(std::mem::take(&mut self.group));
        }
        None
    }
}

/// The transmitter's half (for tests): a data group's packets for
/// `address`, `size` bytes each (24, 48, 72 or 96), continuity counted
/// on from `ci`.
pub fn packetise(group: &[u8], address: u16, size: usize, ci: &mut u8) -> Vec<Vec<u8>> {
    let code = [24, 48, 72, 96].iter().position(|&l| l == size).expect("a packet length") as u8;
    let room = size - 5;
    let chunks: Vec<&[u8]> = group.chunks(room).collect();
    let n = chunks.len();
    chunks
        .iter()
        .enumerate()
        .map(|(i, c)| {
            let (first, last) = (i == 0, i + 1 == n);
            let mut p = vec![code << 6 | (*ci & 3) << 4 | (first as u8) << 3 | (last as u8) << 2 | (address >> 8) as u8 & 3, address as u8, c.len() as u8];
            *ci = (*ci + 1) & 3;
            p.extend_from_slice(c);
            p.resize(size - 2, 0);
            let crc = crc16_ccitt(&p);
            p.extend(crc.to_be_bytes());
            p
        })
        .collect()
}

/// A padding packet (address 0, nothing useful) of `size` bytes.
pub fn padding_packet(size: usize) -> Vec<u8> {
    let code = [24, 48, 72, 96].iter().position(|&l| l == size).expect("a packet length") as u8;
    let mut p = vec![code << 6 | 0x0c, 0, 0];
    p.resize(size - 2, 0);
    let crc = crc16_ccitt(&p);
    p.extend(crc.to_be_bytes());
    p
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packets_join_into_data_groups_and_a_bad_packet_costs_only_its_group() {
        let g1: Vec<u8> = (0..100u8).collect();
        let g2: Vec<u8> = (0..30u8).map(|x| x ^ 0x55).collect();
        let mut ci = 0;
        let mut stream: Vec<Vec<u8>> = packetise(&g1, 5, 24, &mut ci);
        // Another address and padding between, as a multiplex sends.
        let mut other_ci = 0;
        stream.insert(2, packetise(&[9; 10], 6, 24, &mut other_ci).remove(0));
        stream.push(padding_packet(24));
        stream.extend(packetise(&g2, 5, 48, &mut ci));
        let lf: Vec<u8> = stream.concat();
        let mut r = PacketReader::new(5);
        assert_eq!(r.push_frame(&lf), vec![g1.clone(), g2.clone()]);
        // One bit of g1's second packet: g1 is lost, g2 still arrives.
        let mut bad = lf.clone();
        bad[24 + 10] ^= 1;
        let mut r = PacketReader::new(5);
        assert_eq!(r.push_frame(&bad), vec![g2.clone()]);
        assert_eq!((r.counts.crc_errors, r.counts.broken), (1, 1));
        // A damaged header: the reader steps byte by byte to the next good packet.
        let mut bad = lf.clone();
        bad[0] ^= 0xc0; // g1's first packet now says 96 bytes
        let mut r = PacketReader::new(5);
        assert_eq!(r.push_frame(&bad), vec![g2]);
        assert!(r.counts.skipped > 0);
    }

    #[test]
    fn the_fecs_rs_packets_are_stepped_over_without_a_crc() {
        // An application data table's packets, then RS packets as §5.3.5
        // sends them: address 1022, 24 bytes, parity where a CRC would be.
        let g: Vec<u8> = (0..120u8).collect();
        let mut ci = 0;
        let mut stream: Vec<u8> = packetise(&g, 1, 48, &mut ci).concat();
        for n in 0..9u8 {
            let mut fec = vec![(n & 3) << 4 | 0x03, 0xfe];
            fec.extend((0..22).map(|i| (i * 37 + n as usize * 11) as u8));
            stream.extend(fec);
        }
        stream.extend(packetise(&g, 1, 48, &mut ci).concat());
        let mut r = PacketReader::new(1);
        let got: Vec<Vec<u8>> = stream.chunks(48).flat_map(|f| r.push_frame(f)).collect();
        assert_eq!(got, vec![g.clone(), g]);
        assert_eq!((r.counts.crc_errors, r.counts.skipped), (0, 0));
    }

    #[test]
    fn packets_straddling_logical_frames_are_joined() {
        // 96-byte packets on an 8 kbit/s sub-channel: 24 bytes a frame, so
        // each packet spans four frames, and the stream starts mid-packet.
        let g: Vec<u8> = (0..400u32).map(|x| (x * 7) as u8).collect();
        let mut ci = 0;
        let mut stream: Vec<u8> = vec![0x5a; 37]; // the tail of a packet we never saw
        for _ in 0..2 {
            stream.extend(packetise(&g, 9, 96, &mut ci).concat());
            stream.extend(padding_packet(96));
        }
        let mut r = PacketReader::new(9);
        let mut got = Vec::new();
        for frame in stream.chunks(24) {
            got.extend(r.push_frame(frame));
        }
        assert_eq!(got, vec![g.clone(), g]);
    }
}
