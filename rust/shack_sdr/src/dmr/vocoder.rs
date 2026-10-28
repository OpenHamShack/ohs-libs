//! AMBE+2 to sound, only ever through the user's own mbelib (RADIO-SDR-PLAN
//! §5, §9.2). We never port, link or bundle a vocoder: the library is
//! opened at run time from the file the codec loader probed and approved
//! (codecs::library), and the decoder says `needs_codec:mbelib` and carries
//! on with everything else when there is none.
//!
//! mbelib keeps three parameter sets between frames (current, previous,
//! previous enhanced): each slot has its own, as each slot is its own
//! speaker. `mbe_processAmbe2450Dataf` takes the 49 bits our FEC produced
//! (ambe.rs) with its error counts, which it uses to repeat or mute a bad
//! frame, and gives 160 float samples at 8 kHz that mbelib's own
//! conversion to 16-bit PCM multiplies by 7 (`mbe_floattoshort`); so do we.
//!
//! mbelib draws unvoiced bands' phases from the C library's `rand()`, so
//! the same frames never give exactly the same samples twice: compare its
//! sound by its envelope, never sample for sample.

use crate::decoder::CodecId;
use std::ffi::{c_char, c_int, c_void};
use std::sync::Arc;

/// 20 ms at 8 kHz.
pub const FRAME_SAMPLES: usize = 160;
pub const RATE: u32 = 8_000;

/// Something that turns 49 AMBE+2 bits into 160 samples (-1..1). mbelib,
/// or in tests a stand-in.
pub trait Vocoder: Send {
    fn decode(&mut self, bits: &[u8; 49], errs_c0: u32, errs: u32) -> [f32; FRAME_SAMPLES];
}

/// Makes one vocoder per slot.
pub type VocoderFactory = Box<dyn Fn() -> Option<Box<dyn Vocoder>> + Send>;

type InitParms = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void);
type Ambe2450 =
    unsafe extern "C" fn(*mut f32, *mut c_int, *mut c_int, *mut c_char, *mut c_char, *mut c_void, *mut c_void, *mut c_void, c_int);

/// mbe_parms is about 1.2 kB in every mbelib release; each set gets 8 kB.
const PARMS_BYTES: usize = 8192;

pub struct Mbelib {
    // Held so the library outlives the function pointers taken from it.
    _lib: Arc<libloading::Library>,
    process: Ambe2450,
    parms: Vec<u8>,
}

// SAFETY: the parameter buffers are this struct's own, and mbelib's
// functions keep no global state; one Mbelib is used by one thread at a time.
unsafe impl Send for Mbelib {}

impl Mbelib {
    /// mbelib from the codec loader's approved file, if there is one.
    pub fn open() -> Option<Mbelib> {
        let lib = crate::codecs::library(CodecId::Mbelib)?;
        // SAFETY: the signatures are mbelib's public header's (mbelib.h);
        // the probe already found both and ran a frame through them.
        unsafe {
            let init: InitParms = *lib.get::<InitParms>(b"mbe_initMbeParms\0").ok()?;
            let process: Ambe2450 = *lib.get::<Ambe2450>(b"mbe_processAmbe2450Dataf\0").ok()?;
            let mut parms = vec![0u8; 3 * PARMS_BYTES];
            let p = parms.as_mut_ptr();
            init(p as *mut c_void, p.add(PARMS_BYTES) as *mut c_void, p.add(2 * PARMS_BYTES) as *mut c_void);
            Some(Mbelib { _lib: lib, process, parms })
        }
    }

    pub fn factory() -> VocoderFactory {
        Box::new(|| Mbelib::open().map(|m| Box::new(m) as Box<dyn Vocoder>))
    }
}

impl Vocoder for Mbelib {
    fn decode(&mut self, bits: &[u8; 49], errs_c0: u32, errs: u32) -> [f32; FRAME_SAMPLES] {
        let mut out = [0f32; FRAME_SAMPLES];
        let mut data: [c_char; 49] = std::array::from_fn(|i| bits[i] as c_char);
        let (mut e1, mut e2) = (errs_c0 as c_int, errs as c_int);
        // It writes one character for each error and one for the frame's
        // fate: room for any count.
        let mut err_str = [0 as c_char; 128];
        let p = self.parms.as_mut_ptr();
        // SAFETY: buffers sized as mbelib expects (160 floats, 49 bits, the
        // parameter sets above); uvquality 3 is DSD's default.
        unsafe {
            (self.process)(
                out.as_mut_ptr(),
                &mut e1,
                &mut e2,
                err_str.as_mut_ptr(),
                data.as_mut_ptr(),
                p as *mut c_void,
                p.add(PARMS_BYTES) as *mut c_void,
                p.add(2 * PARMS_BYTES) as *mut c_void,
                3,
            );
        }
        for x in out.iter_mut() {
            *x = if x.is_finite() { (*x * 7.0 / 32768.0).clamp(-1.0, 1.0) } else { 0.0 };
        }
        out
    }
}
