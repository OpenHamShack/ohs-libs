//! Packet mode (EN 300 401 §5.3.2): a data sub-channel's logical frames cut
//! into packets of 24, 48, 72 or 96 bytes, each with its address and CRC,
//! and the packets of one address joined back into MSC data groups.
//!
//! A sub-channel's capacity is a whole number of 8 kbit/s units, 24 bytes
//! a logical frame each, so packets begin at every logical frame's start
//! (ODR-DabMux packs them that way, and so does every multiplexer seen);
//! each frame is read from its own start, so a damaged frame costs that
//! frame's packets and nothing after it. Packets whose CRC fails are
//! dropped and break their group (the continuity index says so too);
//! padding (address 0) and the packet FEC's RS packets (address 1022, EN
//! 300 401 §5.3.5) are skipped: the FEC is not applied.

use super::coding::crc16_ccitt;

/// The longest data group we join (the data group's own limit, 8 191 bytes
/// of data field, and its headers).
const MAX_GROUP: usize = 8_400;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct PacketCounts {
    pub packets: u64,
    pub crc_errors: u64,
    pub groups: u64,
    pub broken: u64,
}

/// Joins one address's packets into data groups.
pub struct PacketReader {
    pub address: u16,
    group: Vec<u8>,
    next_ci: Option<u8>,
    pub counts: PacketCounts,
}

impl PacketReader {
    pub fn new(address: u16) -> Self {
        PacketReader { address, group: Vec::new(), next_ci: None, counts: PacketCounts::default() }
    }

    /// One logical frame of the sub-channel: the data groups it completed.
    pub fn push_frame(&mut self, lf: &[u8]) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        let mut k = 0;
        while k + 3 <= lf.len() {
            let len = [24, 48, 72, 96][(lf[k] >> 6) as usize];
            if k + len > lf.len() {
                break;
            }
            let p = &lf[k..k + len];
            k += len;
            self.counts.packets += 1;
            if crc16_ccitt(&p[..len - 2]) != u16::from_be_bytes([p[len - 2], p[len - 1]]) {
                self.counts.crc_errors += 1;
                // Our address's packet may be the lost one: a group in
                // progress can no longer be trusted.
                if !self.group.is_empty() {
                    self.group.clear();
                    self.counts.broken += 1;
                }
                self.next_ci = None;
                continue;
            }
            let address = u16::from_be_bytes([p[0] & 3, p[1]]);
            if address != self.address {
                continue;
            }
            let ci = (p[0] >> 4) & 3;
            let (first, last) = (p[0] & 0x08 != 0, p[0] & 0x04 != 0);
            let useful = ((p[2] & 0x7f) as usize).min(len - 5);
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
                continue; // the middle of a group whose start we missed
            }
            if self.group.len() > MAX_GROUP {
                self.group.clear();
                self.counts.broken += 1;
                continue;
            }
            if last {
                self.counts.groups += 1;
                out.push(std::mem::take(&mut self.group));
            }
        }
        out
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
        assert_eq!(r.push_frame(&bad), vec![g2]);
        assert_eq!((r.counts.crc_errors, r.counts.broken), (1, 1));
    }
}
