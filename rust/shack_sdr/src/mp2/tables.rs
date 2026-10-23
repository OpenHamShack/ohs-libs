//! MPEG-1 Audio Layer II's tables (ISO/IEC 11172-3 Annex B; MPEG-2's lower
//! sampling rates, ISO/IEC 13818-3 Annex B). The numbers are the
//! standard's. They were read from FFmpeg's `libavcodec/mpegaudiodata.c`
//! and `mpegaudiodsp_data.c` (LGPL-2.1-or-later, (c) Fabrice Bellard),
//! the half synthesis window there being the standard's D[] times 65536,
//! exactly (the standard prints D to nine places, all multiples of 2^-16).
//! MPEG-1 Layer II's patents expired long ago; this is our own decoder.

/// Bit rates in kbit/s by index: MPEG-1 Layer II, then MPEG-2 LSF Layer II.
pub const BITRATE_MPEG1: [u16; 15] = [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384];
pub const BITRATE_LSF: [u16; 15] = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160];
/// Sampling rates by index: MPEG-1, then MPEG-2 LSF.
pub const RATE_MPEG1: [u32; 3] = [44_100, 48_000, 32_000];
pub const RATE_LSF: [u32; 3] = [22_050, 24_000, 16_000];

/// Quantisation classes (Table B.4): steps, and bits per code; a negative
/// count is a grouped code of three samples.
pub const QUANT_STEPS: [u32; 17] = [3, 5, 7, 9, 15, 31, 63, 127, 255, 511, 1023, 2047, 4095, 8191, 16383, 32767, 65535];
pub const QUANT_BITS: [i8; 17] = [-5, -7, 3, -10, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16];

/// One allocation table: for each subband, the bits of its allocation
/// field and the class of each allocation 1.. (0 is "nothing sent").
pub struct AllocTable {
    pub sblimit: usize,
    pub bands: &'static [(u8, &'static [u8])],
}

const A4_HI: &[u8] = &[0, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16];
const A4_MID: &[u8] = &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16];
const A3: &[u8] = &[0, 1, 2, 3, 4, 5, 16];
const A2: &[u8] = &[0, 1, 16];
const C4: &[u8] = &[0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];
const C3: &[u8] = &[0, 1, 3, 4, 5, 6, 7];
const L4: &[u8] = &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14];
const L3: &[u8] = &[0, 1, 3, 4, 5, 6, 7];
const L2: &[u8] = &[0, 1, 3];

/// Table B.2a/b: 27 or 30 subbands (high rates per channel).
const AB: [(u8, &[u8]); 30] = [
    (4, A4_HI), (4, A4_HI), (4, A4_HI),
    (4, A4_MID), (4, A4_MID), (4, A4_MID), (4, A4_MID), (4, A4_MID), (4, A4_MID), (4, A4_MID), (4, A4_MID),
    (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3), (3, A3),
    (2, A2), (2, A2), (2, A2), (2, A2), (2, A2), (2, A2), (2, A2),
];
/// Table B.2c/d: 8 or 12 subbands (low rates per channel).
const CD: [(u8, &[u8]); 12] =
    [(4, C4), (4, C4), (3, C3), (3, C3), (3, C3), (3, C3), (3, C3), (3, C3), (3, C3), (3, C3), (3, C3), (3, C3)];
/// ISO/IEC 13818-3 Table B.1: the lower sampling rates, 30 subbands.
const LSF: [(u8, &[u8]); 30] = [
    (4, L4), (4, L4), (4, L4), (4, L4),
    (3, L3), (3, L3), (3, L3), (3, L3), (3, L3), (3, L3), (3, L3),
    (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2),
    (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2), (2, L2),
];

pub static TABLES: [AllocTable; 5] = [
    AllocTable { sblimit: 27, bands: &AB },
    AllocTable { sblimit: 30, bands: &AB },
    AllocTable { sblimit: 8, bands: &CD },
    AllocTable { sblimit: 12, bands: &CD },
    AllocTable { sblimit: 30, bands: &LSF },
];

/// Which allocation table a frame uses (ISO/IEC 11172-3 Table B.2's notes):
/// by the bit rate per channel and the sampling rate; every LSF frame uses
/// 13818-3's one table.
pub fn select(bitrate_kbps: u16, channels: usize, rate: u32, lsf: bool) -> usize {
    if lsf {
        return 4;
    }
    let per = bitrate_kbps as usize / channels.max(1);
    if (rate == 48_000 && per >= 56) || (56..=80).contains(&per) {
        0
    } else if rate != 48_000 && per >= 96 {
        1
    } else if rate != 32_000 && per <= 48 {
        2
    } else {
        3
    }
}

/// The scale factors (Table B.1): 2^(1 - i/3), i = 0..62.
pub fn scalefactor(i: u8) -> f32 {
    (2.0f64).powf(1.0 - i.min(62) as f64 / 3.0) as f32
}

/// Half the synthesis window, D[0..=256] x 65536 (Table B.3).
const HALF_WINDOW: [i32; 257] = [
    0, -1, -1, -1, -1, -1, -1, -2, -2, -2, -2, -3, -3, -4, -4, -5, -5, -6, -7, -7, -8, -9, -10, -11, -13, -14, -16,
    -17, -19, -21, -24, -26, -29, -31, -35, -38, -41, -45, -49, -53, -58, -63, -68, -73, -79, -85, -91, -97, -104,
    -111, -117, -125, -132, -139, -147, -154, -161, -169, -176, -183, -190, -196, -202, -208, 213, 218, 222, 225,
    227, 228, 228, 227, 224, 221, 215, 208, 200, 189, 177, 163, 146, 127, 106, 83, 57, 29, -2, -36, -72, -111, -153,
    -197, -244, -294, -347, -401, -459, -519, -581, -645, -711, -779, -848, -919, -991, -1064, -1137, -1210, -1283,
    -1356, -1428, -1498, -1567, -1634, -1698, -1759, -1817, -1870, -1919, -1962, -2001, -2032, -2057, -2075, -2085,
    -2087, -2080, -2063, 2037, 2000, 1952, 1893, 1822, 1739, 1644, 1535, 1414, 1280, 1131, 970, 794, 605, 402, 185,
    -45, -288, -545, -814, -1095, -1388, -1692, -2006, -2330, -2663, -3004, -3351, -3705, -4063, -4425, -4788, -5153,
    -5517, -5879, -6237, -6589, -6935, -7271, -7597, -7910, -8209, -8491, -8755, -8998, -9219, -9416, -9585, -9727,
    -9838, -9916, -9959, -9966, -9935, -9863, -9750, -9592, -9389, -9139, -8840, -8492, -8092, -7640, -7134, 6574,
    5959, 5288, 4561, 3776, 2935, 2037, 1082, 70, -998, -2122, -3300, -4533, -5818, -7154, -8540, -9975, -11455,
    -12980, -14548, -16155, -17799, -19478, -21189, -22929, -24694, -26482, -28289, -30112, -31947, -33791, -35640,
    -37489, -39336, -41176, -43006, -44821, -46617, -48390, -50137, -51853, -53534, -55178, -56778, -58333, -59838,
    -61289, -62684, -64019, -65290, -66494, -67629, -68692, -69679, -70590, -71420, -72169, -72835, -73415, -73908,
    -74313, -74630, -74856, -74992, 75038,
];

/// The full synthesis window D[0..512]: D[512 - i] = -D[i], except at
/// multiples of 64, where it is D[i].
pub fn synthesis_window() -> [f32; 512] {
    let mut d = [0f32; 512];
    for (i, &v) in HALF_WINDOW.iter().enumerate() {
        let x = v as f32 / 65536.0;
        d[i] = x;
        if i != 0 {
            d[512 - i] = if i % 64 == 0 { x } else { -x };
        }
    }
    d
}
