//! The runtime codec loader's native half (RADIO-SDR-PLAN §5): the table
//! of codecs, the file names each is known by on each OS, and the probe -
//! open one file by its full path, check its entry points and version, and
//! run a known-answer test.
//!
//! **Nothing here is ever linked.** faad2, fdk-aac and mbelib carry AAC
//! and AMBE/IMBE patents (and fdk-aac its own licence), so no build of
//! ours compiles, links, vendors or downloads them (§9.2): they are opened
//! at run time from where the user installed them, and only then.
//! tools/check-encumbered.py fails any build whose binaries import or
//! define their entry points; the names appear here only as strings for
//! the lookup. codec2 (LGPL-2.1, no patent problem) is the exception we may
//! ship, and the loader still treats it as optional.
//!
//! **The probe runs in a child process.** A library built for another ABI
//! can crash or hang the moment it is called, and a thread cannot contain
//! that. So the Python half (python/shack_sdr/codecs.py) runs
//! `python -m shack_sdr.codecs probe <id> <path>` the first time it sees a
//! file, and only a file that passed is ever loaded by the app itself. This
//! module is what that child calls.
//!
//! **Full paths only.** `probe` refuses a bare name: a bare name is a
//! search, and on Windows a search is DLL search-order hijacking. On
//! Windows the file is opened with LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
//! LOAD_LIBRARY_SEARCH_DEFAULT_DIRS, so a codec's own dependencies
//! (libgcc_s_seh-1.dll, libwinpthread-1.dll) are found beside it and
//! nowhere else.
//!
//! The version floors (faad2 2.8, mbelib 1.3) and the known-answer frames
//! are from the libraries' headers and documentation; until checked against
//! real builds (M1, §11) they are marked **[verify]** where they matter.

use crate::decoder::CodecId;
use std::ffi::{c_char, c_int, c_uint, c_ulong, c_void, CStr};
use std::path::Path;

pub struct Entry {
    pub id: CodecId,
    /// The decoders that use it.
    pub used_for: &'static [&'static str],
    /// May we ship it in our own bundles? Only codec2. An encumbered codec
    /// is never looked for in the app's bundle, so a stray copy that slipped
    /// into one could not be used.
    pub shippable: bool,
    /// Its name among a controller's host libraries (/opt/hostlibs/<name>),
    /// RADIO-SDR-PLAN §5.6.
    pub host_library: Option<&'static str>,
}

pub static ENTRIES: [Entry; 4] = [
    Entry { id: CodecId::Codec2, used_for: &["freedv", "m17"], shippable: true, host_library: None },
    Entry { id: CodecId::Faad2, used_for: &["dabplus", "drm"], shippable: false, host_library: Some("faad2") },
    Entry { id: CodecId::FdkAac, used_for: &["dabplus", "drm"], shippable: false, host_library: Some("fdk-aac") },
    Entry {
        id: CodecId::Mbelib,
        used_for: &["dmr", "dstar", "ysf", "p25", "nxdn"],
        shippable: false,
        host_library: Some("mbelib"),
    },
];

/// The file names a codec is known by on this OS, in the order tried.
pub fn names(id: CodecId) -> &'static [&'static str] {
    #[cfg(target_os = "windows")]
    match id {
        CodecId::Codec2 => &["libcodec2.dll", "codec2.dll"],
        CodecId::Faad2 => &["libfaad-2.dll", "libfaad2.dll", "libfaad.dll"],
        CodecId::FdkAac => &["libfdk-aac-2.dll", "libfdk-aac.dll"],
        CodecId::Mbelib => &["libmbe.dll", "mbelib.dll"],
    }
    #[cfg(target_os = "macos")]
    match id {
        CodecId::Codec2 => &["libcodec2.dylib", "libcodec2.1.2.dylib"],
        CodecId::Faad2 => &["libfaad.2.dylib", "libfaad.dylib"],
        CodecId::FdkAac => &["libfdk-aac.2.dylib"],
        CodecId::Mbelib => &["libmbe.dylib"],
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    match id {
        CodecId::Codec2 => &["libcodec2.so.1.2", "libcodec2.so.1.0"],
        CodecId::Faad2 => &["libfaad.so.2"],
        CodecId::FdkAac => &["libfdk-aac.so.2"],
        CodecId::Mbelib => &["libmbe.so.1", "libmbe.so"],
    }
}

/// Why a file was refused. Codes, never sentences.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProbeError {
    /// Not a full path: the loader never searches.
    NotAbsolute,
    /// The OS would not load it (wrong architecture, a missing dependency).
    NotLoadable(String),
    MissingSymbol(String),
    TooOld(String),
    /// The known-answer test gave the wrong answer.
    BadOutput(String),
}

impl ProbeError {
    pub fn code(&self) -> &'static str {
        match self {
            ProbeError::NotAbsolute => "not_absolute",
            ProbeError::NotLoadable(_) => "not_loadable",
            ProbeError::MissingSymbol(_) => "missing_symbol",
            ProbeError::TooOld(_) => "too_old",
            ProbeError::BadOutput(_) => "bad_output",
        }
    }
    /// The parameter of the code: a symbol's name, a version, the loader's words.
    pub fn detail(&self) -> Option<String> {
        match self {
            ProbeError::NotAbsolute => None,
            ProbeError::NotLoadable(s) | ProbeError::MissingSymbol(s) | ProbeError::TooOld(s) | ProbeError::BadOutput(s) => {
                Some(s.clone())
            }
        }
    }
}

/// Open `path` and nothing else: no search, no bare names.
pub fn open(path: &Path) -> Result<libloading::Library, ProbeError> {
    if !path.is_absolute() {
        return Err(ProbeError::NotAbsolute);
    }
    #[cfg(unix)]
    let lib = {
        use libloading::os::unix::{Library, RTLD_LOCAL, RTLD_NOW};
        // SAFETY: loading runs the library's initialisers; that is why only
        // the probe child does it with a file not yet seen to pass.
        unsafe { Library::open(Some(path), RTLD_NOW | RTLD_LOCAL) }.map(libloading::Library::from)
    };
    #[cfg(windows)]
    let lib = {
        use libloading::os::windows::{Library, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR};
        // SAFETY: as above.
        unsafe { Library::load_with_flags(path, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS) }
            .map(libloading::Library::from)
    };
    lib.map_err(|e| ProbeError::NotLoadable(e.to_string()))
}

/// A symbol of type T, or MissingSymbol naming it.
///
/// SAFETY: the caller states the symbol's C type correctly.
unsafe fn sym<'a, T>(lib: &'a libloading::Library, name: &str) -> Result<libloading::Symbol<'a, T>, ProbeError> {
    let mut z = name.as_bytes().to_vec();
    z.push(0);
    unsafe { lib.get::<T>(&z) }.map_err(|_| ProbeError::MissingSymbol(name.to_string()))
}

/// "2.11.2" -> (2, 11); anything unparsable is (0, 0).
fn major_minor(v: &str) -> (u32, u32) {
    let mut it = v.trim().split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty());
    let a = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let b = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    (a, b)
}

/// AAC-LC, 48 kHz, mono: the AudioSpecificConfig the AAC probes open with.
const ASC_LC_48K_MONO: [u8; 2] = [0x11, 0x88];
/// One raw AAC-LC frame of silence for that config: a single channel
/// element with max_sfb 0 (no spectral data), then END (ISO/IEC 14496-3
/// raw_data_block). Ours, written from the syntax; data, not codec code.
/// [verify] against a real faad2 and fdk-aac.
const AAC_SILENT_FRAME: [u8; 4] = [0x00, 0x00, 0x00, 0x07];

/// Check `path` as codec `id`: load it, find every entry point, check the
/// version, and decode a known input. The version string on success.
pub fn probe(id: CodecId, path: &Path) -> Result<Option<String>, ProbeError> {
    let lib = open(path)?;
    // SAFETY (each arm): the signatures are the libraries' public headers'.
    unsafe {
        match id {
            CodecId::Codec2 => probe_codec2(&lib),
            CodecId::Faad2 => probe_faad2(&lib),
            CodecId::FdkAac => probe_fdk_aac(&lib),
            CodecId::Mbelib => probe_mbelib(&lib),
        }
    }
}

unsafe fn probe_codec2(lib: &libloading::Library) -> Result<Option<String>, ProbeError> {
    type Create = unsafe extern "C" fn(c_int) -> *mut c_void;
    type Destroy = unsafe extern "C" fn(*mut c_void);
    type PerFrame = unsafe extern "C" fn(*mut c_void) -> c_int;
    type Decode = unsafe extern "C" fn(*mut c_void, *mut i16, *const u8);
    unsafe {
        let create = sym::<Create>(lib, "codec2_create")?;
        let destroy = sym::<Destroy>(lib, "codec2_destroy")?;
        let samples = sym::<PerFrame>(lib, "codec2_samples_per_frame")?;
        let bytes = sym::<PerFrame>(lib, "codec2_bytes_per_frame")?;
        let decode = sym::<Decode>(lib, "codec2_decode")?;
        // FreeDV is what uses it (ohscore/modes/freedv.py opens its modes).
        sym::<unsafe extern "C" fn()>(lib, "freedv_open")?;
        // Mode 0 is 3200 bit/s: 160 samples from 8 bytes, in every codec2 release.
        let c = create(0);
        if c.is_null() {
            return Err(ProbeError::BadOutput("codec2_create".into()));
        }
        let (n, b) = (samples(c), bytes(c));
        let ok = n == 160 && b == 8;
        if ok {
            let mut out = vec![i16::MIN; 160];
            decode(c, out.as_mut_ptr(), [0u8; 8].as_ptr());
            // Silence in, something sane out: not left at the fill value.
            if out.iter().all(|&s| s == i16::MIN) {
                destroy(c);
                return Err(ProbeError::BadOutput("codec2_decode".into()));
            }
        }
        destroy(c);
        if !ok {
            return Err(ProbeError::BadOutput(format!("frame {n} samples, {b} bytes")));
        }
        Ok(None)
    }
}

/// faad2's NeAACDecFrameInfo, the members read, then room for the rest
/// (the library writes the whole struct, about 100 bytes).
#[repr(C)]
struct FaadFrameInfo {
    bytesconsumed: c_ulong,
    samples: c_ulong,
    channels: u8,
    error: u8,
    samplerate: c_ulong,
    rest: [u8; 512],
}

unsafe fn probe_faad2(lib: &libloading::Library) -> Result<Option<String>, ProbeError> {
    type GetVersion = unsafe extern "C" fn(*mut *mut c_char, *mut *mut c_char) -> c_int;
    type OpenFn = unsafe extern "C" fn() -> *mut c_void;
    type Init2 = unsafe extern "C" fn(*mut c_void, *mut u8, c_ulong, *mut c_ulong, *mut u8) -> c_char;
    type Decode = unsafe extern "C" fn(*mut c_void, *mut FaadFrameInfo, *mut u8, c_ulong) -> *mut c_void;
    type Close = unsafe extern "C" fn(*mut c_void);
    type Caps = unsafe extern "C" fn() -> c_ulong;
    unsafe {
        let get_version = sym::<GetVersion>(lib, "NeAACDecGetVersion")?;
        let open = sym::<OpenFn>(lib, "NeAACDecOpen")?;
        let init2 = sym::<Init2>(lib, "NeAACDecInit2")?;
        let decode = sym::<Decode>(lib, "NeAACDecDecode")?;
        let close = sym::<Close>(lib, "NeAACDecClose")?;
        sym::<Caps>(lib, "NeAACDecGetCapabilities")?;
        let mut id: *mut c_char = std::ptr::null_mut();
        let mut copyright: *mut c_char = std::ptr::null_mut();
        get_version(&mut id, &mut copyright);
        let version = if id.is_null() { String::new() } else { CStr::from_ptr(id).to_string_lossy().into_owned() };
        // 2.8 is the first release with NeAACDecGetVersion and the DRM and
        // 960-sample fixes DAB+ needs. [verify]
        if major_minor(&version) < (2, 8) {
            return Err(ProbeError::TooOld(version));
        }
        let h = open();
        if h.is_null() {
            return Err(ProbeError::BadOutput("NeAACDecOpen".into()));
        }
        let mut asc = ASC_LC_48K_MONO;
        let mut rate: c_ulong = 0;
        let mut ch: u8 = 0;
        let r = init2(h, asc.as_mut_ptr(), asc.len() as c_ulong, &mut rate, &mut ch);
        if r != 0 || rate != 48_000 || ch == 0 {
            close(h);
            return Err(ProbeError::BadOutput(format!("init2 {r} rate {rate} channels {ch}")));
        }
        let mut good = false;
        for _ in 0..2 {
            let mut info: FaadFrameInfo = std::mem::zeroed();
            let mut frame = AAC_SILENT_FRAME;
            let pcm = decode(h, &mut info, frame.as_mut_ptr(), frame.len() as c_ulong);
            if info.error != 0 {
                close(h);
                return Err(ProbeError::BadOutput(format!("decode error {}", info.error)));
            }
            if !pcm.is_null() && info.samples > 0 && info.samples % 1024 == 0 {
                // The default output is 16-bit PCM: silence must decode to (near) silence.
                let s = std::slice::from_raw_parts(pcm as *const i16, info.samples as usize);
                if s.iter().any(|&x| x.unsigned_abs() > 16) {
                    close(h);
                    return Err(ProbeError::BadOutput("not silence".into()));
                }
                good = true;
            }
        }
        close(h);
        if !good {
            return Err(ProbeError::BadOutput("no samples".into()));
        }
        Ok(Some(version))
    }
}

/// fdk-aac's LIB_INFO.
#[repr(C)]
#[derive(Clone, Copy)]
struct FdkLibInfo {
    title: *const c_char,
    build_date: *const c_char,
    build_time: *const c_char,
    module_id: c_int,
    version: c_int,
    flags: c_uint,
    version_str: [c_char; 32],
}

/// FDK_MODULE_ID's AAC decoder.
const FDK_AACDEC: c_int = 3;
const TT_MP4_RAW: c_int = 0;

unsafe fn probe_fdk_aac(lib: &libloading::Library) -> Result<Option<String>, ProbeError> {
    type OpenFn = unsafe extern "C" fn(c_int, c_uint) -> *mut c_void;
    type ConfigRaw = unsafe extern "C" fn(*mut c_void, *mut *mut u8, *const c_uint) -> c_int;
    type Fill = unsafe extern "C" fn(*mut c_void, *mut *mut u8, *const c_uint, *mut c_uint) -> c_int;
    type DecodeFrame = unsafe extern "C" fn(*mut c_void, *mut i16, c_int, c_uint) -> c_int;
    type Close = unsafe extern "C" fn(*mut c_void);
    type GetLibInfo = unsafe extern "C" fn(*mut FdkLibInfo) -> c_int;
    unsafe {
        let open = sym::<OpenFn>(lib, "aacDecoder_Open")?;
        let config = sym::<ConfigRaw>(lib, "aacDecoder_ConfigRaw")?;
        let fill = sym::<Fill>(lib, "aacDecoder_Fill")?;
        let decode = sym::<DecodeFrame>(lib, "aacDecoder_DecodeFrame")?;
        let close = sym::<Close>(lib, "aacDecoder_Close")?;
        let get_info = sym::<GetLibInfo>(lib, "aacDecoder_GetLibInfo")?;
        sym::<unsafe extern "C" fn()>(lib, "aacDecoder_GetStreamInfo")?;
        // FDKinitLibInfo is "every module_id FDK_NONE (0)": zeroed. 64 is
        // more than FDK_MODULE_LAST in any release.
        let mut info = [std::mem::zeroed::<FdkLibInfo>(); 64];
        get_info(info.as_mut_ptr());
        let Some(aac) = info.iter().find(|i| i.module_id == FDK_AACDEC) else {
            return Err(ProbeError::BadOutput("no AAC decoder module".into()));
        };
        let version = CStr::from_ptr(aac.version_str.as_ptr()).to_string_lossy().into_owned();
        // LIB_VERSION(a, b, c) = a<<24 | b<<16 | c<<8. Version 2 and up of
        // the decoder module. [verify which module version carries xHE-AAC]
        if (aac.version >> 24) < 2 {
            return Err(ProbeError::TooOld(version));
        }
        let h = open(TT_MP4_RAW, 1);
        if h.is_null() {
            return Err(ProbeError::BadOutput("aacDecoder_Open".into()));
        }
        let mut asc = ASC_LC_48K_MONO;
        let mut p = asc.as_mut_ptr();
        let len = asc.len() as c_uint;
        if config(h, &mut p, &len) != 0 {
            close(h);
            return Err(ProbeError::BadOutput("ConfigRaw".into()));
        }
        let mut good = false;
        let mut pcm = vec![0i16; 2048 * 8];
        for _ in 0..2 {
            let mut frame = AAC_SILENT_FRAME;
            let mut fp = frame.as_mut_ptr();
            let size = frame.len() as c_uint;
            let mut valid = size;
            if fill(h, &mut fp, &size, &mut valid) != 0 {
                close(h);
                return Err(ProbeError::BadOutput("Fill".into()));
            }
            if decode(h, pcm.as_mut_ptr(), pcm.len() as c_int, 0) == 0 {
                if pcm.iter().any(|&x| x.unsigned_abs() > 16) {
                    close(h);
                    return Err(ProbeError::BadOutput("not silence".into()));
                }
                good = true;
            }
        }
        close(h);
        if !good {
            return Err(ProbeError::BadOutput("DecodeFrame".into()));
        }
        Ok(Some(version))
    }
}

unsafe fn probe_mbelib(lib: &libloading::Library) -> Result<Option<String>, ProbeError> {
    type PrintVersion = unsafe extern "C" fn(*mut c_char);
    type InitParms = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void);
    type Ambe2450 = unsafe extern "C" fn(
        *mut f32,
        *mut c_int,
        *mut c_int,
        *mut c_char,
        *mut c_char,
        *mut c_void,
        *mut c_void,
        *mut c_void,
        c_int,
    );
    unsafe {
        let print_version = sym::<PrintVersion>(lib, "mbe_printVersion")?;
        let init = sym::<InitParms>(lib, "mbe_initMbeParms")?;
        let ambe = sym::<Ambe2450>(lib, "mbe_processAmbe2450Dataf")?;
        // The three frame decoders the voice modes need.
        for f in ["mbe_processAmbe3600x2400Framef", "mbe_processAmbe3600x2450Framef", "mbe_processImbe7200x4400Framef"] {
            sym::<unsafe extern "C" fn()>(lib, f)?;
        }
        let mut v = [0 as c_char; 64];
        print_version(v.as_mut_ptr());
        v[63] = 0;
        let version = CStr::from_ptr(v.as_ptr()).to_string_lossy().into_owned();
        // [verify] mbelib 1.3.0 is the release every voice decoder builds on.
        if major_minor(&version) < (1, 3) {
            return Err(ProbeError::TooOld(version));
        }
        // mbe_parms is about 1.2 kB; three generous blocks.
        let mut parms = vec![0u8; 3 * 8192];
        let (cur, rest) = parms.split_at_mut(8192);
        let (prev, enh) = rest.split_at_mut(8192);
        let (cur, prev, enh) = (cur.as_mut_ptr() as *mut c_void, prev.as_mut_ptr() as *mut c_void, enh.as_mut_ptr() as *mut c_void);
        init(cur, prev, enh);
        let mut out = vec![f32::NAN; 160];
        let (mut e1, mut e2) = (0 as c_int, 0 as c_int);
        let mut err = [0 as c_char; 64];
        let mut data = [0 as c_char; 49];
        ambe(out.as_mut_ptr(), &mut e1, &mut e2, err.as_mut_ptr(), data.as_mut_ptr(), cur, prev, enh, 3);
        if !out.iter().all(|x| x.is_finite()) {
            return Err(ProbeError::BadOutput("non-finite audio".into()));
        }
        Ok(Some(version))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_compare_by_major_and_minor() {
        assert_eq!(major_minor("2.11.2"), (2, 11));
        assert_eq!(major_minor(" 1.3.0 (mbelib)"), (1, 3));
        assert_eq!(major_minor("garbage"), (0, 0));
        assert!(major_minor("2.10.1") >= (2, 8) && major_minor("2.7") < (2, 8));
    }

    #[test]
    fn a_bare_name_is_never_searched() {
        assert_eq!(probe(CodecId::Faad2, Path::new("libfaad.so.2")), Err(ProbeError::NotAbsolute));
    }

    #[test]
    fn only_codec2_is_ours_to_ship() {
        for e in &ENTRIES {
            assert_eq!(e.shippable, e.id == CodecId::Codec2);
            assert!(!names(e.id).is_empty());
        }
    }
}
