//! Our own MPEG-1 Audio Layer II decoder (with MPEG-2's lower sampling
//! rates), for old DAB's audio, written from ISO/IEC 11172-3 §2.4.3.3 and
//! 13818-3. Layer II's patents expired years ago (Brian, 23/10/2026: "our
//! own Rust decoder ... so old-DAB stations play everywhere with nothing to
//! install"), unlike AAC, which is only ever the user's own faad2.
//!
//! DAB's use of it (EN 300 401 §7, ETSI TS 103 466) is plain Layer II at
//! 48 or 24 kHz with a CRC in every frame and the frame's last bytes
//! carrying the scale-factor CRC and PAD, which the audio decoding skips:
//! the frame's own length says where samples end. `dab_pad` hands the F-PAD
//! and X-PAD bytes to whoever wants them.
//!
//! `encode` is a plain Layer II encoder (no psychoacoustic model: it spends
//! bits where the signal is loudest) for the DAB transmitter model in the
//! tests, so the decoder is tested on frames it did not make up itself.

pub mod encode;
pub mod tables;

use tables::{AllocTable, QUANT_BITS, QUANT_STEPS, TABLES};

/// Why a frame was not decoded. Codes, never sentences.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mp2Error {
    /// No sync word, not Layer II, or a reserved field.
    BadHeader,
    /// The frame is longer than the bytes given.
    Short,
    /// The header CRC disagrees: the frame is muted.
    Crc,
}

impl Mp2Error {
    pub fn code(self) -> &'static str {
        match self {
            Mp2Error::BadHeader => "bad_header",
            Mp2Error::Short => "short",
            Mp2Error::Crc => "crc",
        }
    }
}

/// A Layer II frame header (ISO/IEC 11172-3 §2.4.2.3).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Header {
    /// MPEG-2's lower sampling rates (ID bit 0).
    pub lsf: bool,
    pub crc: bool,
    pub bitrate: u16,
    pub rate: u32,
    pub padding: bool,
    /// 0 stereo, 1 joint stereo, 2 dual channel, 3 mono.
    pub mode: u8,
    pub mode_ext: u8,
}

impl Header {
    pub fn parse(b: &[u8]) -> Result<Header, Mp2Error> {
        if b.len() < 4 || b[0] != 0xff || b[1] & 0xf0 != 0xf0 {
            return Err(Mp2Error::BadHeader);
        }
        let lsf = b[1] & 0x08 == 0;
        let layer = (b[1] >> 1) & 3;
        if layer != 2 {
            return Err(Mp2Error::BadHeader);
        }
        let crc = b[1] & 1 == 0;
        let bi = (b[2] >> 4) as usize;
        let ri = ((b[2] >> 2) & 3) as usize;
        if bi == 0 || bi == 15 || ri == 3 {
            return Err(Mp2Error::BadHeader);
        }
        let (bitrate, rate) = if lsf { (tables::BITRATE_LSF[bi], tables::RATE_LSF[ri]) } else { (tables::BITRATE_MPEG1[bi], tables::RATE_MPEG1[ri]) };
        Ok(Header { lsf, crc, bitrate, rate, padding: b[2] & 2 != 0, mode: b[3] >> 6, mode_ext: (b[3] >> 4) & 3 })
    }

    pub fn channels(&self) -> usize {
        if self.mode == 3 {
            1
        } else {
            2
        }
    }

    /// The frame's length in bytes (1152 samples of it).
    pub fn length(&self) -> usize {
        144_000 * self.bitrate as usize / self.rate as usize + self.padding as usize
    }

    /// The four header bytes.
    pub fn bytes(&self) -> [u8; 4] {
        let (brs, rts): (&[u16], &[u32]) =
            if self.lsf { (&tables::BITRATE_LSF, &tables::RATE_LSF) } else { (&tables::BITRATE_MPEG1, &tables::RATE_MPEG1) };
        let bi = brs.iter().position(|&b| b == self.bitrate).unwrap_or(0) as u8;
        let ri = rts.iter().position(|&r| r == self.rate).unwrap_or(0) as u8;
        [
            0xff,
            0xf0 | (!self.lsf as u8) << 3 | 2 << 1 | !self.crc as u8,
            bi << 4 | ri << 2 | (self.padding as u8) << 1,
            self.mode << 6 | self.mode_ext << 4 | 0x04, // original
        ]
    }
}

/// Reads bits, first from the high end, and counts them.
pub(crate) struct Bits<'a> {
    b: &'a [u8],
    pub pos: usize,
}

impl<'a> Bits<'a> {
    pub fn new(b: &'a [u8], pos: usize) -> Self {
        Bits { b, pos }
    }
    pub fn read(&mut self, n: u32) -> u32 {
        let mut v = 0u32;
        for _ in 0..n {
            let byte = self.b.get(self.pos / 8).copied().unwrap_or(0);
            v = v << 1 | (byte >> (7 - self.pos % 8)) as u32 & 1;
            self.pos += 1;
        }
        v
    }
}

/// CRC-16 x^16 + x^15 + x^2 + 1 from all ones over bits [from, to) of `b`
/// (ISO/IEC 11172-3 §2.4.3.1).
pub(crate) fn crc_bits(b: &[u8], ranges: &[(usize, usize)]) -> u16 {
    let mut c: u16 = 0xffff;
    for &(from, to) in ranges {
        for p in from..to {
            let bit = (b[p / 8] >> (7 - p % 8)) as u16 & 1;
            let fb = (c >> 15) ^ bit;
            c <<= 1;
            if fb != 0 {
                c ^= 0x8005;
            }
        }
    }
    c
}

/// Where the allocation ends, for the CRC: header, allocations and SCFSI.
pub(crate) struct Layout {
    pub table: &'static AllocTable,
    pub sblimit: usize,
    pub bound: usize,
    pub channels: usize,
}

pub(crate) fn layout(h: &Header) -> Layout {
    let channels = h.channels();
    let t = &TABLES[tables::select(h.bitrate, channels, h.rate, h.lsf)];
    let bound = if h.mode == 1 { 4 * (h.mode_ext as usize + 1) } else { t.sblimit }.min(t.sblimit);
    Layout { table: t, sblimit: t.sblimit, bound: if channels == 1 { t.sblimit } else { bound }, channels }
}

/// One decoded frame: 1152 frames of PCM, interleaved when stereo.
#[derive(Clone, Debug, PartialEq)]
pub struct Frame {
    pub pcm: Vec<f32>,
    pub rate: u32,
    pub channels: u8,
}

/// The polyphase synthesis filter bank of one channel (§2.4.3.3.4 and
/// Figure A.2): V is a ring of 1024.
#[derive(Clone)]
struct Synth {
    v: Vec<f32>,
    off: usize,
}

/// The matrixing coefficients N[i][k] = cos((16 + i)(2k + 1) pi / 64),
/// and the window: shared by every channel.
struct Bank {
    n: Vec<[f32; 32]>,
    d: [f32; 512],
}

fn bank() -> &'static Bank {
    static B: std::sync::OnceLock<Bank> = std::sync::OnceLock::new();
    B.get_or_init(|| Bank {
        n: (0..64)
            .map(|i| {
                let mut r = [0f32; 32];
                for (k, x) in r.iter_mut().enumerate() {
                    *x = ((16 + i) as f64 * (2 * k + 1) as f64 * std::f64::consts::PI / 64.0).cos() as f32;
                }
                r
            })
            .collect(),
        d: tables::synthesis_window(),
    })
}

impl Synth {
    fn new() -> Self {
        Synth { v: vec![0.0; 1024], off: 0 }
    }

    /// 32 subband samples in, 32 PCM samples out.
    fn run(&mut self, s: &[f32; 32], out: &mut [f32; 32]) {
        let b = bank();
        self.off = (self.off + 1024 - 64) & 1023;
        for i in 0..64 {
            let row = &b.n[i];
            let mut acc = 0f32;
            for k in 0..32 {
                acc += row[k] * s[k];
            }
            self.v[(self.off + i) & 1023] = acc;
        }
        for (j, o) in out.iter_mut().enumerate() {
            let mut acc = 0f32;
            for i in 0..8 {
                // U[64i + j] = V[128i + j], U[64i + 32 + j] = V[128i + 96 + j].
                acc += self.v[(self.off + 128 * i + j) & 1023] * b.d[64 * i + j];
                acc += self.v[(self.off + 128 * i + 96 + j) & 1023] * b.d[64 * i + 32 + j];
            }
            *o = acc;
        }
    }
}

/// A Layer II decoder for one stream; its filter bank keeps state from
/// frame to frame.
pub struct Decoder {
    synth: [Synth; 2],
    pub frames: u64,
    pub crc_errors: u64,
}

impl Default for Decoder {
    fn default() -> Self {
        Self::new()
    }
}

impl Decoder {
    pub fn new() -> Self {
        Decoder { synth: [Synth::new(), Synth::new()], frames: 0, crc_errors: 0 }
    }

    pub fn reset(&mut self) {
        self.synth = [Synth::new(), Synth::new()];
    }

    /// Decode the frame at the start of `b`.
    pub fn decode(&mut self, b: &[u8]) -> Result<Frame, Mp2Error> {
        let h = Header::parse(b)?;
        let len = h.length();
        if b.len() < len {
            return Err(Mp2Error::Short);
        }
        let b = &b[..len];
        let lay = layout(&h);
        let (nch, sbl, bound) = (lay.channels, lay.sblimit, lay.bound);
        let mut r = Bits::new(b, 32);
        let crc_stored = if h.crc { Some(r.read(16) as u16) } else { None };
        // Allocation (§2.4.1.6: below the bound per channel, above it shared).
        let mut alloc = [[0u8; 32]; 2];
        for sb in 0..sbl {
            let nbal = lay.table.bands[sb].0 as u32;
            if sb < bound {
                for a in alloc.iter_mut().take(nch) {
                    a[sb] = r.read(nbal) as u8;
                }
            } else {
                let v = r.read(nbal) as u8;
                alloc[0][sb] = v;
                alloc[1][sb] = v;
            }
        }
        let mut scfsi = [[0u8; 32]; 2];
        for sb in 0..sbl {
            for ch in 0..nch {
                if alloc[ch][sb] != 0 {
                    scfsi[ch][sb] = r.read(2) as u8;
                }
            }
        }
        if let Some(stored) = crc_stored {
            if crc_bits(b, &[(16, 32), (48, r.pos)]) != stored {
                self.crc_errors += 1;
                return Err(Mp2Error::Crc);
            }
        }
        let mut scf = [[[0f32; 3]; 32]; 2];
        for sb in 0..sbl {
            for ch in 0..nch {
                if alloc[ch][sb] == 0 {
                    continue;
                }
                let mut rd = || tables::scalefactor(r.read(6) as u8);
                scf[ch][sb] = match scfsi[ch][sb] {
                    0 => [rd(), rd(), rd()],
                    1 => {
                        let a = rd();
                        let c = rd();
                        [a, a, c]
                    }
                    2 => {
                        let a = rd();
                        [a, a, a]
                    }
                    _ => {
                        let a = rd();
                        let c = rd();
                        [a, c, c]
                    }
                };
            }
        }
        // Samples: 12 granules of 3 per subband, in three parts of four
        // granules, each part with its own scale factor.
        let mut pcm = vec![0f32; 1152 * nch];
        let mut sub = [[[0f32; 32]; 3]; 2];
        for gr in 0..12 {
            let part = gr / 4;
            for sb in 0..sbl {
                let shared = sb >= bound;
                for ch in 0..nch {
                    if shared && ch == 1 {
                        // Read once for both; the second channel scales the
                        // same values by its own scale factor.
                        let a = alloc[0][sb];
                        if a != 0 {
                            for s in 0..3 {
                                sub[1][s][sb] = sub[0][s][sb] / scf[0][sb][part] * scf[1][sb][part];
                            }
                        } else {
                            for s in sub[1].iter_mut() {
                                s[sb] = 0.0;
                            }
                        }
                        continue;
                    }
                    let a = alloc[ch][sb] as usize;
                    if a == 0 {
                        for s in sub[ch].iter_mut() {
                            s[sb] = 0.0;
                        }
                        continue;
                    }
                    let class = lay.table.bands[sb].1[a - 1] as usize;
                    let n = QUANT_STEPS[class];
                    let bits = QUANT_BITS[class];
                    let mut q = [0u32; 3];
                    if bits < 0 {
                        let mut c = r.read((-bits) as u32);
                        for v in q.iter_mut() {
                            *v = c % n;
                            c /= n;
                        }
                    } else {
                        for v in q.iter_mut() {
                            *v = r.read(bits as u32);
                        }
                    }
                    let f = scf[ch][sb][part];
                    for s in 0..3 {
                        // (2q - (n - 1)) / n: the standard's C (s'' + D).
                        sub[ch][s][sb] = (2.0 * q[s].min(n - 1) as f32 - (n - 1) as f32) / n as f32 * f;
                    }
                }
            }
            for s in 0..3 {
                for ch in 0..nch {
                    let mut out = [0f32; 32];
                    self.synth[ch].run(&sub[ch][s], &mut out);
                    let base = (gr * 3 + s) * 32;
                    for (j, v) in out.iter().enumerate() {
                        pcm[(base + j) * nch + ch] = *v;
                    }
                }
            }
        }
        self.frames += 1;
        Ok(Frame { pcm, rate: h.rate, channels: nch as u8 })
    }
}

/// DAB's F-PAD: the last two bytes of every DAB audio frame (EN 300 401
/// §7.4.1), whose indicator says how much X-PAD precedes the scale-factor
/// CRC before it. The audio decoding never reads them.
pub fn dab_fpad(frame: &[u8]) -> Option<[u8; 2]> {
    (frame.len() >= 2).then(|| [frame[frame.len() - 2], frame[frame.len() - 1]])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn headers_read_and_write_alike() {
        for (lsf, bitrate, rate, mode) in [(false, 128u16, 48_000u32, 0u8), (false, 192, 48_000, 1), (true, 64, 24_000, 3)] {
            let h = Header { lsf, crc: true, bitrate, rate, padding: false, mode, mode_ext: 2 };
            assert_eq!(Header::parse(&h.bytes()).unwrap(), h);
        }
        // DAB's sizes: 3 bytes per kbit/s at 48 kHz, 6 at 24 kHz.
        let h = Header::parse(&Header { lsf: false, crc: true, bitrate: 128, rate: 48_000, padding: false, mode: 0, mode_ext: 0 }.bytes()).unwrap();
        assert_eq!(h.length(), 384);
        let h = Header { lsf: true, crc: true, bitrate: 64, rate: 24_000, padding: false, mode: 3, mode_ext: 0 };
        assert_eq!(h.length(), 384);
        assert_eq!(Header::parse(&[0xff, 0xff, 0x90, 0x00]), Err(Mp2Error::BadHeader)); // layer bits 11: Layer I
    }

    #[test]
    fn the_window_is_the_standards() {
        let d = tables::synthesis_window();
        // Table B.3: D[1] = -0.000015259, D[256] = 1.144989014, D[64] =
        // 0.003250122, D[257] = 1.144287109, D[448] = 0.003250122.
        for (i, want) in [(1usize, -0.000015259f64), (256, 1.144989014), (64, 0.003250122), (257, 1.144287109), (448, 0.003250122)] {
            assert!((d[i] as f64 - want).abs() < 1e-9, "D[{i}] = {}", d[i]);
        }
    }
}
