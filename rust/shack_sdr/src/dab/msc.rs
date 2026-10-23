//! One MSC sub-channel (EN 300 401 §11.3, §12, §13): its CUs cut from each
//! CIF's soft bits, time de-interleaved over 16 CIFs, depunctured by its
//! UEP or EEP profile, Viterbi-decoded and de-scrambled into the logical
//! frame's bytes. Only the chosen service's sub-channel is ever decoded.

use super::coding::{self, Soft, Viterbi};
use super::fic::Subchannel;
use super::tables::{CU_BITS, TIME_INTERLEAVE};
use std::collections::VecDeque;

pub struct SubchannelDecoder {
    pub sub: Subchannel,
    map: Vec<usize>,
    info_bits: usize,
    prbs: Vec<u8>,
    viterbi: Viterbi,
    /// The last 16 CIFs' soft bits of this sub-channel, oldest first.
    ring: VecDeque<Vec<Soft>>,
    pub frames: u64,
}

impl SubchannelDecoder {
    /// None when the sub-channel's protection names no profile we know.
    pub fn new(sub: Subchannel) -> Option<Self> {
        let p = sub.protection.profile()?;
        if p.coded_bits() != sub.protection.size() * CU_BITS {
            return None;
        }
        let info_bits = p.info_bits();
        Some(SubchannelDecoder {
            map: coding::puncture_map(&p),
            info_bits,
            prbs: coding::prbs(info_bits),
            viterbi: Viterbi::new(),
            ring: VecDeque::with_capacity(16),
            sub,
            frames: 0,
        })
    }

    /// Forget the interleaver's history (a gap in the CIFs).
    pub fn reset(&mut self) {
        self.ring.clear();
    }

    /// Bytes per logical frame (24 ms of the sub-channel).
    pub fn frame_bytes(&self) -> usize {
        self.info_bits / 8
    }

    /// One CIF's soft bits (all 55 296); the logical frame 15 CIFs back,
    /// once 16 have been seen.
    pub fn push_cif(&mut self, cif: &[Soft]) -> Option<Vec<u8>> {
        let size = self.sub.protection.size() * CU_BITS;
        let from = self.sub.start as usize * CU_BITS;
        if from + size > cif.len() {
            return None;
        }
        if self.ring.len() == 16 {
            let mut old = self.ring.pop_front().unwrap();
            old.clear();
            old.extend_from_slice(&cif[from..from + size]);
            self.ring.push_back(old);
        } else {
            self.ring.push_back(cif[from..from + size].to_vec());
        }
        if self.ring.len() < 16 {
            return None;
        }
        // Table 21: bit i was sent TIME_INTERLEAVE[i % 16] CIFs after its
        // logical frame; ring[0] is 15 CIFs back.
        let codeword: Vec<Soft> = (0..size).map(|i| self.ring[TIME_INTERLEAVE[i % 16]][i]).collect();
        let mut bits = self.viterbi.decode(&codeword, &self.map, self.info_bits);
        for (b, p) in bits.iter_mut().zip(&self.prbs) {
            *b ^= p;
        }
        self.frames += 1;
        Some(coding::pack(&bits))
    }
}
