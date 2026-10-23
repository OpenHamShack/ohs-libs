//! A plain Layer II encoder, for tests: the DAB transmitter model's old-DAB
//! service, and the decoder's round trip. ISO/IEC 11172-3's analysis
//! filter bank (§C.1.3, window C = D / 32), scale factors per part, and a
//! greedy bit allocation that raises whichever subband's signal-to-noise
//! is lowest until the frame is full: no psychoacoustic model, which only
//! decides what is audible, not whether the stream is valid.

use super::tables::{self, QUANT_BITS, QUANT_STEPS, TABLES};
use super::{crc_bits, layout, Header};

/// The analysis filter bank of one channel.
struct Analysis {
    x: Vec<f32>,
    c: [f32; 512],
    m: Vec<[f32; 64]>,
}

impl Analysis {
    fn new() -> Self {
        let d = tables::synthesis_window();
        let mut c = [0f32; 512];
        for i in 0..512 {
            c[i] = d[i] / 32.0;
        }
        let m = (0..32)
            .map(|k| {
                let mut r = [0f32; 64];
                for (i, v) in r.iter_mut().enumerate() {
                    *v = ((2 * k + 1) as f64 * (i as f64 - 16.0) * std::f64::consts::PI / 64.0).cos() as f32;
                }
                r
            })
            .collect();
        Analysis { x: vec![0.0; 512], c, m }
    }

    /// 32 PCM samples in, 32 subband samples out.
    fn run(&mut self, pcm: &[f32]) -> [f32; 32] {
        self.x.copy_within(0..480, 32);
        for i in 0..32 {
            self.x[31 - i] = pcm[i];
        }
        let mut y = [0f32; 64];
        for (i, yi) in y.iter_mut().enumerate() {
            let mut acc = 0f32;
            for j in 0..8 {
                acc += self.c[i + 64 * j] * self.x[i + 64 * j];
            }
            *yi = acc;
        }
        let mut s = [0f32; 32];
        for (k, sk) in s.iter_mut().enumerate() {
            *sk = self.m[k].iter().zip(&y).map(|(a, b)| a * b).sum();
        }
        s
    }
}

struct Writer {
    b: Vec<u8>,
    pos: usize,
}

impl Writer {
    fn put(&mut self, v: u32, n: u32) {
        for k in (0..n).rev() {
            if v >> k & 1 == 1 {
                self.b[self.pos / 8] |= 0x80 >> (self.pos % 8);
            }
            self.pos += 1;
        }
    }
}

/// An encoder for one stream. `mode`: 0 stereo, 1 joint stereo (with
/// `mode_ext`), 3 mono.
pub struct Encoder {
    pub header: Header,
    an: [Analysis; 2],
    /// Bytes left at each frame's end for DAB's scale-factor CRC and PAD.
    pub reserve: usize,
}

impl Encoder {
    pub fn new(bitrate: u16, rate: u32, mode: u8, mode_ext: u8) -> Self {
        let lsf = rate < 32_000;
        let header = Header { lsf, crc: true, bitrate, rate, padding: false, mode, mode_ext };
        Encoder { header, an: [Analysis::new(), Analysis::new()], reserve: if lsf { 4 } else { 6 } }
    }

    /// One frame from 1152 frames of PCM (interleaved when stereo).
    pub fn frame(&mut self, pcm: &[f32]) -> Vec<u8> {
        let h = self.header;
        let lay = layout(&h);
        let (nch, sbl, bound) = (lay.channels, lay.sblimit, lay.bound);
        // Subband samples [ch][sample 0..36][sb].
        let mut sub = vec![[[0f32; 32]; 36]; nch];
        for t in 0..36 {
            for (ch, s) in sub.iter_mut().enumerate() {
                let block: Vec<f32> = (0..32).map(|j| pcm[(t * 32 + j) * nch + ch]).collect();
                s[t] = self.an[ch].run(&block);
            }
        }
        // Scale factors per part: the smallest one at least the part's peak.
        let mut scfi = [[[62u8; 3]; 32]; 2];
        let mut peak_db = [[-200f32; 32]; 2];
        for ch in 0..nch {
            for sb in 0..sbl {
                for part in 0..3 {
                    let peak = (0..12).map(|t| sub[ch][part * 12 + t][sb].abs()).fold(0f32, f32::max);
                    let mut i = 62u8;
                    while i > 0 && tables::scalefactor(i) < peak {
                        i -= 1;
                    }
                    scfi[ch][sb][part] = i;
                    peak_db[ch][sb] = peak_db[ch][sb].max(20.0 * peak.max(1e-10).log10());
                }
            }
        }
        // Greedy allocation within the frame's bits.
        let len = h.length();
        let mut budget = (len - self.reserve) as i64 * 8 - 32 - 16;
        for sb in 0..sbl {
            let nbal = lay.table.bands[sb].0 as i64;
            budget -= if sb < bound { nbal * nch as i64 } else { nbal };
        }
        let mut alloc = [[0u8; 32]; 2];
        let class_snr = |c: usize| 20.0 * (QUANT_STEPS[c] as f32).log10();
        let bits_of = |c: usize| -> i64 {
            let b = QUANT_BITS[c] as i64;
            if b < 0 { 12 * -b } else { 36 * b }
        };
        loop {
            // The subband (and channel) whose noise is loudest now.
            let mut best: Option<(usize, usize, f32)> = None;
            for sb in 0..sbl {
                let chs = if sb < bound { nch } else { 1 };
                for ch in 0..chs {
                    let a = alloc[ch][sb] as usize;
                    let maxa = lay.table.bands[sb].1.len();
                    if a >= maxa || peak_db[ch][sb] < -100.0 {
                        continue;
                    }
                    let snr = if a == 0 { 0.0 } else { class_snr(lay.table.bands[sb].1[a - 1] as usize) };
                    let nmr = peak_db[ch][sb] - snr;
                    if best.map_or(true, |b| nmr > b.2) {
                        best = Some((sb, ch, nmr));
                    }
                }
            }
            let Some((sb, ch, _)) = best else { break };
            let a = alloc[ch][sb] as usize;
            let new_c = lay.table.bands[sb].1[a] as usize;
            let mut cost = bits_of(new_c);
            if a > 0 {
                cost -= bits_of(lay.table.bands[sb].1[a - 1] as usize);
            } else {
                // Scale factors and their selection, for each channel this
                // allocation brings in.
                cost += if sb < bound { 20 } else { 20 * nch as i64 };
            }
            if cost > budget {
                // Out of room for this one: stop raising it.
                peak_db[ch][sb] = -200.0;
                if sb >= bound && nch == 2 {
                    peak_db[1][sb] = -200.0;
                }
                continue;
            }
            budget -= cost;
            alloc[ch][sb] += 1;
            if sb >= bound {
                alloc[1][sb] = alloc[0][sb];
            }
        }
        // Write it.
        let mut w = Writer { b: vec![0u8; len], pos: 0 };
        for byte in h.bytes() {
            w.put(byte as u32, 8);
        }
        w.put(0, 16); // the CRC, filled in below
        for sb in 0..sbl {
            let nbal = lay.table.bands[sb].0 as u32;
            if sb < bound {
                for a in alloc.iter().take(nch) {
                    w.put(a[sb] as u32, nbal);
                }
            } else {
                w.put(alloc[0][sb] as u32, nbal);
            }
        }
        for sb in 0..sbl {
            for a in alloc.iter().take(nch) {
                if a[sb] != 0 {
                    w.put(0, 2); // three scale factors
                }
            }
        }
        let crc = crc_bits(&w.b, &[(16, 32), (48, w.pos)]);
        w.b[4] = (crc >> 8) as u8;
        w.b[5] = crc as u8;
        for sb in 0..sbl {
            for ch in 0..nch {
                if alloc[ch][sb] != 0 {
                    for part in 0..3 {
                        w.put(scfi[ch][sb][part] as u32, 6);
                    }
                }
            }
        }
        for gr in 0..12 {
            let part = gr / 4;
            for sb in 0..sbl {
                let chs = if sb < bound { nch } else { 1 };
                for ch in 0..chs {
                    let a = alloc[ch][sb] as usize;
                    if a == 0 {
                        continue;
                    }
                    let class = lay.table.bands[sb].1[a - 1] as usize;
                    let n = QUANT_STEPS[class];
                    let q: Vec<u32> = (0..3)
                        .map(|s| {
                            let t = gr * 3 + s;
                            // Above the bound both channels share one value,
                            // each scaled by its own factor on decoding.
                            let v = if sb >= bound && nch == 2 {
                                0.5 * (sub[0][t][sb] / tables::scalefactor(scfi[0][sb][part])
                                    + sub[1][t][sb] / tables::scalefactor(scfi[1][sb][part]))
                            } else {
                                sub[ch][t][sb] / tables::scalefactor(scfi[ch][sb][part])
                            };
                            let q = ((v as f64 * n as f64 + (n - 1) as f64) / 2.0).round();
                            q.clamp(0.0, (n - 1) as f64) as u32
                        })
                        .collect();
                    let bits = QUANT_BITS[class];
                    if bits < 0 {
                        w.put(q[0] + n * q[1] + n * n * q[2], (-bits) as u32);
                    } else {
                        for v in q {
                            w.put(v, bits as u32);
                        }
                    }
                }
            }
        }
        // Above the bound in joint stereo both channels' scale factors are
        // sent; the loop above wrote them for channel 1 too (alloc mirrored).
        debug_assert!(w.pos <= (len - self.reserve) * 8);
        w.b
    }
}
