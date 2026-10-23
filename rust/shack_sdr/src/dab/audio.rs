//! A DAB service's sound from its sub-channel's logical frames.
//!
//! - **Old DAB** (EN 300 401 §7, ASCTy 0): MPEG-1/2 Layer II, one frame per
//!   logical frame at 48 kHz, one per two at 24 kHz, decoded by our own
//!   decoder (crate::mp2). A frame whose header CRC fails is muted, the
//!   usual concealment.
//! - **DAB+** (TS 102 563, ASCTy 63): five logical frames make an audio
//!   super frame; it is found by its header's Fire code, corrected by
//!   RS(120,110) across its virtual interleaver's rows, and split into AUs
//!   by the header's start positions, each checked by its CRC. The AUs are
//!   HE-AAC v2, which only the user's own faad2 decodes (RADIO-SDR-PLAN
//!   §9.2: never ported, linked or bundled), opened at run time through
//!   `Aac`. With no faad2 the super frames are still found and counted, so
//!   the service is known to be healthy and silent only for want of it.

use super::coding;
use super::pad::{self, PadDecoder};
use super::rs;
use crate::mp2;
use std::collections::VecDeque;

/// Sound out of a stream: (pcm interleaved, rate, channels).
pub type Pcm = (Vec<f32>, u32, u8);

/// An AAC decoder: faad2 at run time, or a test's stand-in.
pub trait Aac: Send {
    /// Decode one AU into PCM, or a code saying why not.
    fn decode(&mut self, au: &[u8]) -> Result<Pcm, &'static str>;
}

/// Makes an AAC decoder for a DAB+ stream's AudioSpecificConfig.
pub type AacFactory = Box<dyn Fn(&[u8]) -> Result<Box<dyn Aac>, &'static str> + Send>;

/// Old DAB's frames back together and decoded.
pub struct Mp2Stream {
    dec: mp2::Decoder,
    buf: Vec<u8>,
    pub frames: u64,
    pub errors: u64,
    /// The PAD at each frame's end: the dynamic label.
    pub pad: PadDecoder,
}

impl Default for Mp2Stream {
    fn default() -> Self {
        Self::new()
    }
}

impl Mp2Stream {
    pub fn new() -> Self {
        Mp2Stream { dec: mp2::Decoder::new(), buf: Vec::new(), frames: 0, errors: 0, pad: PadDecoder::new() }
    }

    /// One logical frame's bytes; the sound of any frame it completes.
    pub fn push(&mut self, lf: &[u8]) -> Vec<Pcm> {
        self.buf.extend_from_slice(lf);
        let mut out = Vec::new();
        while self.buf.len() >= 4 {
            match mp2::Header::parse(&self.buf) {
                Ok(h) => {
                    let len = h.length();
                    if self.buf.len() < len {
                        // A 24 kHz frame spans two logical frames; more than
                        // two means this was no frame.
                        if self.buf.len() > 2 * lf.len() {
                            self.buf.clear();
                        }
                        break;
                    }
                    // The PAD is not covered by the frame's CRC: read it
                    // anyway, its own CRCs guard the label.
                    let scf = pad::scf_crc_len(h.lsf, h.bitrate, h.channels() == 1);
                    if let Some((before, fpad)) = pad::mp2_pad(&self.buf[..len], scf) {
                        self.pad.push(before, fpad);
                    }
                    match self.dec.decode(&self.buf[..len]) {
                        Ok(f) => {
                            self.frames += 1;
                            out.push((f.pcm, f.rate, f.channels));
                        }
                        Err(_) => {
                            // Muted: a frame of silence keeps the timing.
                            self.errors += 1;
                            out.push((vec![0.0; 1152 * h.channels()], h.rate, h.channels() as u8));
                        }
                    }
                    self.buf.drain(..len);
                }
                Err(_) => {
                    // DAB frames start on logical frame boundaries: no
                    // header here, so drop this logical frame's worth.
                    self.errors += 1;
                    let n = lf.len().min(self.buf.len());
                    self.buf.drain(..n);
                }
            }
        }
        out
    }
}

/// DAB+'s audio parameters, from a super frame's header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DabPlusFormat {
    pub dac48: bool,
    pub sbr: bool,
    pub stereo: bool,
    pub ps: bool,
}

impl DabPlusFormat {
    fn from(b: u8) -> Self {
        DabPlusFormat { dac48: b & 0x40 != 0, sbr: b & 0x20 != 0, stereo: b & 0x10 != 0, ps: b & 0x08 != 0 }
    }

    pub fn aus(&self) -> usize {
        match (self.dac48, self.sbr) {
            (false, true) => 2,
            (true, true) => 3,
            (false, false) => 4,
            (true, false) => 6,
        }
    }

    /// The AudioSpecificConfig faad2 is opened with (ISO/IEC 14496-3
    /// §1.6.2.1): AAC LC with the 960-sample transform, and SBR and PS
    /// signalled explicitly and backward-compatibly, as DAB+ needs. As
    /// dablin's `AACDecoder` builds it (Stefan Pöschel, GPL-3.0-or-later;
    /// its comment notes faad2 does not take PS signalled as AOT 29). This
    /// is the container's signalling, not AAC decoding.
    pub fn asc(&self) -> Vec<u8> {
        // Sampling frequency indices: 48 k = 3, 32 k = 5, 24 k = 6, 16 k = 8.
        let core = match (self.dac48, self.sbr) {
            (true, false) => 3u8,
            (false, false) => 5,
            (true, true) => 6,
            (false, true) => 8,
        };
        let ext = if self.dac48 { 3u8 } else { 5 };
        let ch = if self.stereo { 2u8 } else { 1 };
        let mut asc = vec![0b00010 << 3 | core >> 1, (core & 1) << 7 | ch << 3 | 0b100];
        if self.sbr {
            asc.extend([0x56, 0xe5, 0x80 | ext << 3]);
            if self.ps {
                let n = asc.len();
                asc[n - 1] |= 0x05;
                asc.extend([0x48, 0x80]);
            }
        }
        asc
    }

    /// "he-aac v2", ...: what the stream is, for the feed.
    pub fn codec_code(&self) -> &'static str {
        match (self.sbr, self.ps) {
            (true, true) => "he-aac-v2",
            (true, false) => "he-aac",
            _ => "aac-lc",
        }
    }
}

/// What a super frame gave.
#[derive(Debug, Default)]
pub struct SuperframeOut {
    pub format: Option<DabPlusFormat>,
    /// The AUs whose CRC held, in order.
    pub aus: Vec<Vec<u8>>,
    pub au_errors: u64,
}

/// DAB+ super frames out of logical frames.
pub struct Superframes {
    s: usize,
    frames: VecDeque<Vec<u8>>,
    synced: bool,
    /// Logical frames since the last super frame, while synced.
    count: usize,
    pub found: u64,
    pub lost: u64,
    pub rs_corrected: u64,
    pub rs_failed: u64,
}

impl Superframes {
    /// For a sub-channel of `bitrate` kbit/s.
    pub fn new(bitrate: u16) -> Self {
        Superframes { s: bitrate as usize / 8, frames: VecDeque::new(), synced: false, count: 0, found: 0, lost: 0, rs_corrected: 0, rs_failed: 0 }
    }

    pub fn synced(&self) -> bool {
        self.synced
    }

    /// One logical frame; a super frame when it completes one.
    pub fn push(&mut self, lf: &[u8]) -> Option<SuperframeOut> {
        self.frames.push_back(lf.to_vec());
        if self.frames.len() > 5 {
            self.frames.pop_front();
        }
        self.count += 1;
        if self.frames.len() < 5 || (self.synced && self.count < 5) {
            return None;
        }
        let mut sf: Vec<u8> = self.frames.iter().flatten().copied().collect();
        if sf.len() != 120 * self.s {
            return None;
        }
        // RS first (it may mend the header), then the Fire code decides.
        let s = self.s;
        let mut fixed = 0usize;
        let mut failed = false;
        for i in 0..s {
            let mut w: Vec<u8> = (0..120).map(|j| sf[i + j * s]).collect();
            match rs::correct(&mut w) {
                Ok(n) => {
                    fixed += n;
                    for (j, v) in w.iter().enumerate() {
                        sf[i + j * s] = *v;
                    }
                }
                Err(()) => failed = true,
            }
        }
        if coding::firecode(&sf[2..11]) != u16::from_be_bytes([sf[0], sf[1]]) {
            if self.synced {
                self.lost += 1;
                // Keep the 5-frame rhythm a while; search again after two misses.
                if self.lost % 2 == 0 {
                    self.synced = false;
                }
            }
            self.count = 0;
            return None;
        }
        self.synced = true;
        self.count = 0;
        self.frames.clear();
        self.found += 1;
        self.rs_corrected += fixed as u64;
        self.rs_failed += failed as u64;
        let fmt = DabPlusFormat::from(sf[2]);
        let num = fmt.aus();
        let start0 = match num {
            2 => 5,
            3 => 6,
            4 => 8,
            _ => 11,
        };
        let bits = coding::unpack(&sf[..start0]);
        let mut starts = vec![start0];
        for n in 1..num {
            let v = bits[24 + (n - 1) * 12..24 + n * 12].iter().fold(0usize, |a, &b| a << 1 | b as usize);
            starts.push(v);
        }
        starts.push(110 * s);
        let mut out = SuperframeOut { format: Some(fmt), ..Default::default() };
        for n in 0..num {
            let (a, b) = (starts[n], starts[n + 1]);
            if a + 2 > b || b > 110 * s {
                out.au_errors += 1;
                continue;
            }
            if coding::crc16_ok(&sf[a..b]) {
                out.aus.push(sf[a..b - 2].to_vec());
            } else {
                out.au_errors += 1;
            }
        }
        Some(out)
    }
}

/// faad2, opened at run time from the file the codec loader's probe passed
/// (crate::codecs). Never linked: its entry points are looked up by name.
pub mod faad {
    use super::{Aac, Pcm};
    use std::ffi::{c_char, c_ulong, c_void};
    use std::sync::Arc;

    /// NeAACDecFrameInfo's leading members, then room for the rest.
    #[repr(C)]
    struct FrameInfo {
        bytesconsumed: c_ulong,
        samples: c_ulong,
        channels: u8,
        error: u8,
        samplerate: c_ulong,
        rest: [u8; 512],
    }

    type OpenFn = unsafe extern "C" fn() -> *mut c_void;
    type Init2 = unsafe extern "C" fn(*mut c_void, *mut u8, c_ulong, *mut c_ulong, *mut u8) -> c_char;
    type Decode = unsafe extern "C" fn(*mut c_void, *mut FrameInfo, *mut u8, c_ulong) -> *mut c_void;
    type Close = unsafe extern "C" fn(*mut c_void);

    pub struct Faad {
        // Kept so the library outlives the handle.
        _lib: Arc<libloading::Library>,
        h: *mut c_void,
        decode: Decode,
        close: Close,
        pub rate: u32,
        pub channels: u8,
    }

    // One session's decoder, used by one worker thread at a time.
    unsafe impl Send for Faad {}

    impl Faad {
        /// A decoder for `asc`, from the library `lib`.
        pub fn open(lib: Arc<libloading::Library>, asc: &[u8]) -> Result<Faad, &'static str> {
            // SAFETY: the signatures are faad2's public header's (neaacdec.h);
            // the loader's probe ran this library in a child process first.
            unsafe {
                let open = *lib.get::<OpenFn>(b"NeAACDecOpen\0").map_err(|_| "codec_missing_symbol")?;
                let init = *lib.get::<Init2>(b"NeAACDecInit2\0").map_err(|_| "codec_missing_symbol")?;
                let decode = *lib.get::<Decode>(b"NeAACDecDecode\0").map_err(|_| "codec_missing_symbol")?;
                let close = *lib.get::<Close>(b"NeAACDecClose\0").map_err(|_| "codec_missing_symbol")?;
                let h = open();
                if h.is_null() {
                    return Err("codec_open");
                }
                let mut a = asc.to_vec();
                let (mut rate, mut ch): (c_ulong, u8) = (0, 0);
                if init(h, a.as_mut_ptr(), a.len() as c_ulong, &mut rate, &mut ch) != 0 {
                    close(h);
                    return Err("codec_config");
                }
                Ok(Faad { _lib: lib, h, decode, close, rate: rate as u32, channels: ch })
            }
        }
    }

    impl Aac for Faad {
        fn decode(&mut self, au: &[u8]) -> Result<Pcm, &'static str> {
            let mut a = au.to_vec();
            // SAFETY: as open; the output is the library's own buffer of
            // info.samples 16-bit samples (faad2's default format), valid
            // until the next call.
            unsafe {
                let mut info: FrameInfo = std::mem::zeroed();
                let pcm = (self.decode)(self.h, &mut info, a.as_mut_ptr(), a.len() as c_ulong);
                if info.error != 0 || pcm.is_null() || info.samples == 0 || info.channels == 0 {
                    return Err("codec_frame");
                }
                let s = std::slice::from_raw_parts(pcm as *const i16, info.samples as usize);
                Ok((s.iter().map(|&v| v as f32 / 32768.0).collect(), info.samplerate as u32, info.channels))
            }
        }
    }

    impl Drop for Faad {
        fn drop(&mut self) {
            // SAFETY: the handle came from NeAACDecOpen and is closed once.
            unsafe { (self.close)(self.h) }
        }
    }
}
