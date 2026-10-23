//! DAB's constants for transmission mode I, from ETSI EN 300 401 V2.1.1
//! (2017-01), the table or clause named at each. The specification is the
//! source; welle.io (GPL-2.0-or-later, Jan van Katwijk's SDR-J tables:
//! `phasetable.cpp`, `protTables.cpp`, `uep-protection.cpp`) was read
//! alongside and agrees everywhere except one row, noted at UEP below.
//! Tests (tests/dab.rs and this file's) check the tables against each
//! other and against the worked values the specification prints.

/// The elementary period is 1/2 048 000 s (Table 22): every time below is
/// in samples at this rate.
pub const RATE: f64 = 2_048_000.0;
/// Carriers (K), useful symbol (Tu), guard (Δ), symbol (Ts), null symbol
/// and frame (TF), and OFDM symbols per frame excluding the null (L).
pub const K: usize = 1536;
pub const TU: usize = 2048;
pub const GUARD: usize = 504;
pub const TS: usize = TU + GUARD;
pub const TNULL: usize = 2656;
pub const TF: usize = 196_608;
pub const L: usize = 76;
/// Symbols carrying the FIC (2-4) and the first MSC symbol (§14.4).
pub const FIC_SYMBOLS: usize = 3;
pub const MSC_FIRST: usize = 5;
/// Bits per OFDM symbol (2K), per CIF (§13), per capacity unit.
pub const SYMBOL_BITS: usize = 2 * K;
pub const CIF_BITS: usize = 55_296;
pub const CU_BITS: usize = 64;
pub const CIF_CUS: usize = 864;
/// Symbols per CIF, CIFs and FIC blocks per frame (mode I).
pub const CIF_SYMBOLS: usize = CIF_BITS / SYMBOL_BITS;
pub const CIFS: usize = 4;
/// A FIC block: 3 FIBs of 256 bits, convolutionally coded to 2304 (§11.2.1).
pub const FIB_BITS: usize = 256;
pub const FIC_INFO_BITS: usize = 3 * FIB_BITS;
pub const FIC_CODED_BITS: usize = 2304;

/// Table 13: the puncturing vectors PI = 1..24 (index 0 is PI 1), 32 bits
/// each, the first bit in the high end. PI keeps 8 + PI of every 32.
pub const PUNCTURE: [u32; 24] = [
    0b1100_1000_1000_1000_1000_1000_1000_1000,
    0b1100_1000_1000_1000_1100_1000_1000_1000,
    0b1100_1000_1100_1000_1100_1000_1000_1000,
    0b1100_1000_1100_1000_1100_1000_1100_1000,
    0b1100_1100_1100_1000_1100_1000_1100_1000,
    0b1100_1100_1100_1000_1100_1100_1100_1000,
    0b1100_1100_1100_1100_1100_1100_1100_1000,
    0b1100_1100_1100_1100_1100_1100_1100_1100,
    0b1110_1100_1100_1100_1100_1100_1100_1100,
    0b1110_1100_1100_1100_1110_1100_1100_1100,
    0b1110_1100_1110_1100_1110_1100_1100_1100,
    0b1110_1100_1110_1100_1110_1100_1110_1100,
    0b1110_1110_1110_1100_1110_1100_1110_1100,
    0b1110_1110_1110_1100_1110_1110_1110_1100,
    0b1110_1110_1110_1110_1110_1110_1110_1100,
    0b1110_1110_1110_1110_1110_1110_1110_1110,
    0b1111_1110_1110_1110_1110_1110_1110_1110,
    0b1111_1110_1110_1110_1111_1110_1110_1110,
    0b1111_1110_1111_1110_1111_1110_1110_1110,
    0b1111_1110_1111_1110_1111_1110_1111_1110,
    0b1111_1111_1111_1110_1111_1110_1111_1110,
    0b1111_1111_1111_1110_1111_1111_1111_1110,
    0b1111_1111_1111_1111_1111_1111_1111_1110,
    0b1111_1111_1111_1111_1111_1111_1111_1111,
];

/// §11.1.2: the 24 tail bits' puncturing vector VT, keeping 12.
pub const PUNCTURE_TAIL: u32 = 0b1100_1100_1100_1100_1100_1100;

/// Is bit `i` (0-31) of puncturing vector `pi` (1-24) kept?
pub fn kept(pi: usize, i: usize) -> bool {
    PUNCTURE[pi - 1] >> (31 - i) & 1 == 1
}

/// A protection profile: runs of (blocks, PI) over the mother codeword's
/// 128-bit blocks, then the 24 tail bits by VT, then zero padding bits
/// (§11.3). The FIC's is a profile too (§11.2.1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Profile {
    pub runs: Vec<(usize, usize)>,
    pub padding: usize,
}

impl Profile {
    /// Information bits per logical frame: 32 per 128-bit block.
    pub fn info_bits(&self) -> usize {
        32 * self.runs.iter().map(|r| r.0).sum::<usize>()
    }
    /// Bits after puncturing and padding: the sub-channel's size.
    pub fn coded_bits(&self) -> usize {
        self.runs.iter().map(|&(n, pi)| n * 4 * (8 + pi)).sum::<usize>() + 12 + self.padding
    }
}

/// §11.2.1: the FIC, 21 blocks at PI 16 and 3 at PI 15.
pub fn fic_profile() -> Profile {
    Profile { runs: vec![(21, 16), (3, 15)], padding: 0 }
}

/// Table 8 (short form): table index -> (sub-channel size in CUs,
/// protection level, bit rate in kbit/s).
pub const UEP_INDEX: [(u16, u8, u16); 64] = [
    (16, 5, 32), (21, 4, 32), (24, 3, 32), (29, 2, 32), (35, 1, 32),
    (24, 5, 48), (29, 4, 48), (35, 3, 48), (42, 2, 48), (52, 1, 48),
    (29, 5, 56), (35, 4, 56), (42, 3, 56), (52, 2, 56),
    (32, 5, 64), (42, 4, 64), (48, 3, 64), (58, 2, 64), (70, 1, 64),
    (40, 5, 80), (52, 4, 80), (58, 3, 80), (70, 2, 80), (84, 1, 80),
    (48, 5, 96), (58, 4, 96), (70, 3, 96), (84, 2, 96), (104, 1, 96),
    (58, 5, 112), (70, 4, 112), (84, 3, 112), (104, 2, 112),
    (64, 5, 128), (84, 4, 128), (96, 3, 128), (116, 2, 128), (140, 1, 128),
    (80, 5, 160), (104, 4, 160), (116, 3, 160), (140, 2, 160), (168, 1, 160),
    (96, 5, 192), (116, 4, 192), (140, 3, 192), (168, 2, 192), (208, 1, 192),
    (116, 5, 224), (140, 4, 224), (168, 3, 224), (208, 2, 224), (232, 1, 224),
    (128, 5, 256), (168, 4, 256), (192, 3, 256), (232, 2, 256), (280, 1, 256),
    (160, 5, 320), (208, 4, 320), (280, 2, 320),
    (192, 5, 384), (280, 3, 384), (416, 1, 384),
];

/// Table 15: UEP profiles, (bit rate, level, L1-L4, PI1-PI4, padding).
/// PI 0 means "-" (no fourth run). welle.io's copy has PI2 = 7 for 80 kbit/s
/// level 1; the specification says 17, and only 17 makes the size 84 CUs
/// that Table 8 gives (a test checks every row this way).
pub const UEP: [(u16, u8, [u8; 4], [u8; 4], u8); 64] = [
    (32, 5, [3, 4, 17, 0], [5, 3, 2, 0], 0),
    (32, 4, [3, 3, 18, 0], [11, 6, 5, 0], 0),
    (32, 3, [3, 4, 14, 3], [15, 9, 6, 8], 0),
    (32, 2, [3, 4, 14, 3], [22, 13, 8, 13], 0),
    (32, 1, [3, 5, 13, 3], [24, 17, 12, 17], 4),
    (48, 5, [4, 3, 26, 3], [5, 4, 2, 3], 0),
    (48, 4, [3, 4, 26, 3], [9, 6, 4, 6], 0),
    (48, 3, [3, 4, 26, 3], [15, 10, 6, 9], 4),
    (48, 2, [3, 4, 26, 3], [24, 14, 8, 15], 0),
    (48, 1, [3, 5, 25, 3], [24, 18, 13, 18], 0),
    (56, 5, [6, 10, 23, 3], [5, 4, 2, 3], 0),
    (56, 4, [6, 10, 23, 3], [9, 6, 4, 5], 0),
    (56, 3, [6, 12, 21, 3], [16, 7, 6, 9], 0),
    (56, 2, [6, 10, 23, 3], [23, 13, 8, 13], 8),
    (64, 5, [6, 9, 31, 2], [5, 3, 2, 3], 0),
    (64, 4, [6, 9, 33, 0], [11, 6, 5, 0], 0),
    (64, 3, [6, 12, 27, 3], [16, 8, 6, 9], 0),
    (64, 2, [6, 10, 29, 3], [23, 13, 8, 13], 8),
    (64, 1, [6, 11, 28, 3], [24, 18, 12, 18], 4),
    (80, 5, [6, 10, 41, 3], [6, 3, 2, 3], 0),
    (80, 4, [6, 10, 41, 3], [11, 6, 5, 6], 0),
    (80, 3, [6, 11, 40, 3], [16, 8, 6, 7], 0),
    (80, 2, [6, 10, 41, 3], [23, 13, 8, 13], 8),
    (80, 1, [6, 10, 41, 3], [24, 17, 12, 18], 4),
    (96, 5, [7, 9, 53, 3], [5, 4, 2, 4], 0),
    (96, 4, [7, 10, 52, 3], [9, 6, 4, 6], 0),
    (96, 3, [6, 12, 51, 3], [16, 9, 6, 10], 4),
    (96, 2, [6, 10, 53, 3], [22, 12, 9, 12], 0),
    (96, 1, [6, 13, 50, 3], [24, 18, 13, 19], 0),
    (112, 5, [14, 17, 50, 3], [5, 4, 2, 5], 0),
    (112, 4, [11, 21, 49, 3], [9, 6, 4, 8], 0),
    (112, 3, [11, 23, 47, 3], [16, 8, 6, 9], 0),
    (112, 2, [11, 21, 49, 3], [23, 12, 9, 14], 4),
    (128, 5, [12, 19, 62, 3], [5, 3, 2, 4], 0),
    (128, 4, [11, 21, 61, 3], [11, 6, 5, 7], 0),
    (128, 3, [11, 22, 60, 3], [16, 9, 6, 10], 4),
    (128, 2, [11, 21, 61, 3], [22, 12, 9, 14], 0),
    (128, 1, [11, 20, 62, 3], [24, 17, 13, 19], 8),
    (160, 5, [11, 19, 87, 3], [5, 4, 2, 4], 0),
    (160, 4, [11, 23, 83, 3], [11, 6, 5, 9], 0),
    (160, 3, [11, 24, 82, 3], [16, 8, 6, 11], 0),
    (160, 2, [11, 21, 85, 3], [22, 11, 9, 13], 0),
    (160, 1, [11, 22, 84, 3], [24, 18, 12, 19], 0),
    (192, 5, [11, 20, 110, 3], [6, 4, 2, 5], 0),
    (192, 4, [11, 22, 108, 3], [10, 6, 4, 9], 0),
    (192, 3, [11, 24, 106, 3], [16, 10, 6, 11], 0),
    (192, 2, [11, 20, 110, 3], [22, 13, 9, 13], 8),
    (192, 1, [11, 21, 109, 3], [24, 20, 13, 24], 0),
    (224, 5, [12, 22, 131, 3], [8, 6, 2, 6], 4),
    (224, 4, [12, 26, 127, 3], [12, 8, 4, 11], 0),
    (224, 3, [11, 20, 134, 3], [16, 10, 7, 9], 0),
    (224, 2, [11, 22, 132, 3], [24, 16, 10, 15], 0),
    (224, 1, [11, 24, 130, 3], [24, 20, 12, 20], 4),
    (256, 5, [11, 24, 154, 3], [6, 5, 2, 5], 0),
    (256, 4, [11, 24, 154, 3], [12, 9, 5, 10], 4),
    (256, 3, [11, 27, 151, 3], [16, 10, 7, 10], 0),
    (256, 2, [11, 22, 156, 3], [24, 14, 10, 13], 8),
    (256, 1, [11, 26, 152, 3], [24, 19, 14, 18], 4),
    (320, 5, [11, 26, 200, 3], [8, 5, 2, 6], 4),
    (320, 4, [11, 25, 201, 3], [13, 9, 5, 10], 8),
    (320, 2, [11, 26, 200, 3], [24, 17, 9, 17], 0),
    (384, 5, [11, 27, 247, 3], [8, 6, 2, 7], 0),
    (384, 3, [11, 24, 250, 3], [16, 9, 7, 10], 4),
    (384, 1, [12, 28, 245, 3], [24, 20, 14, 23], 8),
];

/// How a sub-channel is protected (FIG 0/1).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Protection {
    /// Short form: Table 8's index.
    Uep { index: u8 },
    /// Long form: option 0 (set A) or 1 (set B), level 1-4, size in CUs.
    Eep { option: u8, level: u8, size: u16 },
}

impl Protection {
    /// Sub-channel size in CUs.
    pub fn size(&self) -> usize {
        match *self {
            Protection::Uep { index } => UEP_INDEX[index as usize & 63].0 as usize,
            Protection::Eep { size, .. } => size as usize,
        }
    }

    /// Bit rate in kbit/s, or None for a size no profile fits.
    pub fn bitrate(&self) -> Option<u16> {
        match *self {
            Protection::Uep { index } => Some(UEP_INDEX[index as usize & 63].2),
            Protection::Eep { option: 0, level, size } => {
                // Table 18: sizes 12n, 8n, 6n, 4n CUs at 8n kbit/s for 1-A..4-A.
                let per = [12u16, 8, 6, 4][(level as usize).clamp(1, 4) - 1];
                (size % per == 0 && size > 0).then(|| 8 * (size / per))
            }
            Protection::Eep { option: 1, level, size } => {
                // Table 20: 27n, 21n, 18n, 15n CUs at 32n kbit/s for 1-B..4-B.
                let per = [27u16, 21, 18, 15][(level as usize).clamp(1, 4) - 1];
                (size % per == 0 && size > 0).then(|| 32 * (size / per))
            }
            Protection::Eep { .. } => None,
        }
    }

    /// "1-A", "3", ...: the level as broadcasters write it.
    pub fn level_code(&self) -> String {
        match *self {
            Protection::Uep { index } => format!("{}", UEP_INDEX[index as usize & 63].1),
            Protection::Eep { option, level, .. } => format!("{}-{}", level, if option == 0 { "A" } else { "B" }),
        }
    }

    /// The puncturing profile (§11.3.1, §11.3.2), or None when the
    /// signalling names none we know.
    pub fn profile(&self) -> Option<Profile> {
        match *self {
            Protection::Uep { index } => {
                let (_, level, rate) = UEP_INDEX[index as usize & 63];
                let (_, _, l, pi, pad) = *UEP.iter().find(|u| u.0 == rate && u.1 == level)?;
                let runs = (0..4).filter(|&k| l[k] > 0).map(|k| (l[k] as usize, pi[k] as usize)).collect();
                Some(Profile { runs, padding: pad as usize })
            }
            Protection::Eep { option, level, .. } => {
                let rate = self.bitrate()? as usize;
                let runs = match (option, level) {
                    (0, _) => {
                        let n = rate / 8;
                        match level {
                            1 => vec![(6 * n - 3, 24), (3, 23)],
                            2 if n == 1 => vec![(5, 13), (1, 12)],
                            2 => vec![(2 * n - 3, 14), (4 * n + 3, 13)],
                            3 => vec![(6 * n - 3, 8), (3, 7)],
                            4 => vec![(4 * n - 3, 3), (2 * n + 3, 2)],
                            _ => return None,
                        }
                    }
                    (1, 1..=4) => {
                        let n = rate / 32;
                        let pi = [10, 6, 4, 2][level as usize - 1];
                        vec![(24 * n - 3, pi), (3, pi - 1)]
                    }
                    _ => return None,
                };
                Some(Profile { runs: runs.into_iter().filter(|r| r.0 > 0).collect(), padding: 0 })
            }
        }
    }
}

/// Table 21: the time interleaver's delay index for bit i, by i mod 16.
/// Bit i of logical frame r is sent in CIF r + TIME_INTERLEAVE[i % 16].
pub const TIME_INTERLEAVE: [usize; 16] = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15];

/// §14.6.1: F(n), the carrier index k (-768..768, not 0) that carries QPSK
/// symbol n.
pub fn frequency_interleave() -> Vec<i32> {
    let mut out = Vec::with_capacity(K);
    let mut p: u32 = 0;
    for _ in 0..TU {
        if (256..=1792).contains(&p) && p != 1024 {
            out.push(p as i32 - 1024);
        }
        p = (13 * p + 511) % 2048;
    }
    out
}

/// Table 23: (first k of the run of 32, i, n) for transmission mode I.
const PRS_RUNS: [(i32, u8, u8); 48] = [
    (-768, 0, 1), (-736, 1, 2), (-704, 2, 0), (-672, 3, 1), (-640, 0, 3), (-608, 1, 2),
    (-576, 2, 2), (-544, 3, 3), (-512, 0, 2), (-480, 1, 1), (-448, 2, 2), (-416, 3, 3),
    (-384, 0, 1), (-352, 1, 2), (-320, 2, 3), (-288, 3, 3), (-256, 0, 2), (-224, 1, 2),
    (-192, 2, 2), (-160, 3, 1), (-128, 0, 1), (-96, 1, 3), (-64, 2, 1), (-32, 3, 2),
    (1, 0, 3), (33, 3, 1), (65, 2, 1), (97, 1, 1), (129, 0, 2), (161, 3, 2),
    (193, 2, 1), (225, 1, 0), (257, 0, 2), (289, 3, 2), (321, 2, 3), (353, 1, 3),
    (385, 0, 0), (417, 3, 2), (449, 2, 1), (481, 1, 3), (513, 0, 3), (545, 3, 3),
    (577, 2, 3), (609, 1, 0), (641, 0, 3), (673, 3, 0), (705, 2, 1), (737, 1, 1),
];

/// Table 24: h(i, j), j = 0..31 (its second half repeats the first).
const PRS_H: [[u8; 16]; 4] = [
    [0, 2, 0, 0, 0, 0, 1, 1, 2, 0, 0, 0, 2, 2, 1, 1],
    [0, 3, 2, 3, 0, 1, 3, 0, 2, 1, 2, 3, 2, 3, 3, 0],
    [0, 0, 0, 2, 0, 2, 1, 3, 2, 2, 0, 2, 2, 0, 1, 3],
    [0, 1, 2, 1, 0, 3, 3, 2, 2, 3, 2, 1, 2, 1, 3, 2],
];

/// §14.3.2: the phase reference symbol's phase on carrier k, in quarter
/// turns (0-3): φk = π/2 (h(i, k - k') + n).
pub fn prs_quarter_turns(k: i32) -> u8 {
    let &(k0, i, n) = PRS_RUNS.iter().rev().find(|r| r.0 <= k).expect("k in -768..768");
    (PRS_H[i as usize][((k - k0) % 16) as usize] + n) % 4
}

/// Band III blocks and their centre frequencies in Hz: the Wiesbaden
/// (1995) plan's 5A-13F, as every receiver lists them, and the three N
/// blocks it added. EN 300 401 §15.5 requires only multiples of 16 kHz;
/// the names come from the plan (welle.io's `channels.cpp` agrees).
pub const BAND_III: [(&str, u32); 41] = [
    ("5A", 174_928_000), ("5B", 176_640_000), ("5C", 178_352_000), ("5D", 180_064_000),
    ("6A", 181_936_000), ("6B", 183_648_000), ("6C", 185_360_000), ("6D", 187_072_000),
    ("7A", 188_928_000), ("7B", 190_640_000), ("7C", 192_352_000), ("7D", 194_064_000),
    ("8A", 195_936_000), ("8B", 197_648_000), ("8C", 199_360_000), ("8D", 201_072_000),
    ("9A", 202_928_000), ("9B", 204_640_000), ("9C", 206_352_000), ("9D", 208_064_000),
    ("10A", 209_936_000), ("10N", 210_096_000), ("10B", 211_648_000), ("10C", 213_360_000), ("10D", 215_072_000),
    ("11A", 216_928_000), ("11N", 217_088_000), ("11B", 218_640_000), ("11C", 220_352_000), ("11D", 222_064_000),
    ("12A", 223_936_000), ("12N", 224_096_000), ("12B", 225_648_000), ("12C", 227_360_000), ("12D", 229_072_000),
    ("13A", 230_784_000), ("13B", 232_496_000), ("13C", 234_208_000), ("13D", 235_776_000), ("13E", 237_488_000),
    ("13F", 239_200_000),
];

/// The centre of a Band III block by its name ("11D"), any case.
pub fn block_centre(name: &str) -> Option<u32> {
    BAND_III.iter().find(|b| b.0.eq_ignore_ascii_case(name.trim())).map(|b| b.1)
}

/// ETSI TS 101 756 Annex C: the complete EBU Latin character set (charset
/// 0) as Unicode. As welle.io's `charsets.cpp` (GPL-2.0-or-later, by
/// Przemyslaw Wegrzyn, generated from ODR-PadEnc's table).
pub const EBU_LATIN: [u16; 256] = [
    0x00, 0x118, 0x12e, 0x172, 0x102, 0x116, 0x10e, 0x218, 0x21a, 0x10a, 0xa, 0xb, 0x120, 0x139, 0x17b, 0x143,
    0x105, 0x119, 0x12f, 0x173, 0x103, 0x117, 0x10f, 0x219, 0x21b, 0x10b, 0x147, 0x11a, 0x121, 0x13a, 0x17c, 0x82,
    0x20, 0x21, 0x22, 0x23, 0x142, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f,
    0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f,
    0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x4f,
    0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x5b, 0x16e, 0x5d, 0x141, 0x5f,
    0x104, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x6b, 0x6c, 0x6d, 0x6e, 0x6f,
    0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0xab, 0x16f, 0xbb, 0x13d, 0x126,
    0xe1, 0xe0, 0xe9, 0xe8, 0xed, 0xec, 0xf3, 0xf2, 0xfa, 0xf9, 0xd1, 0xc7, 0x15e, 0xdf, 0xa1, 0x178,
    0xe2, 0xe4, 0xea, 0xeb, 0xee, 0xef, 0xf4, 0xf6, 0xfb, 0xfc, 0xf1, 0xe7, 0x15f, 0x11f, 0x131, 0xff,
    0x136, 0x145, 0xa9, 0x122, 0x11e, 0x11b, 0x148, 0x151, 0x150, 0x20ac, 0xa3, 0x24, 0x100, 0x112, 0x12a, 0x16a,
    0x137, 0x146, 0x13b, 0x123, 0x13c, 0x130, 0x144, 0x171, 0x170, 0xbf, 0x13e, 0xb0, 0x101, 0x113, 0x12b, 0x16b,
    0xc1, 0xc0, 0xc9, 0xc8, 0xcd, 0xcc, 0xd3, 0xd2, 0xda, 0xd9, 0x158, 0x10c, 0x160, 0x17d, 0xd0, 0x13f,
    0xc2, 0xc4, 0xca, 0xcb, 0xce, 0xcf, 0xd4, 0xd6, 0xdb, 0xdc, 0x159, 0x10d, 0x161, 0x17e, 0x111, 0x140,
    0xc3, 0xc5, 0xc6, 0x152, 0x177, 0xdd, 0xd5, 0xd8, 0xde, 0x14a, 0x154, 0x106, 0x15a, 0x179, 0x164, 0xf0,
    0xe3, 0xe5, 0xe6, 0x153, 0x175, 0xfd, 0xf5, 0xf8, 0xfe, 0x14b, 0x155, 0x107, 0x15b, 0x17a, 0x165, 0x127,
];

/// A label's bytes as text: charset 0 (EBU Latin), 15 (UTF-8) or 6 (UCS-2,
/// big-endian), ETSI TS 101 756 Table 1; trailing padding and spaces
/// dropped. Another charset reads as EBU Latin, the commonest.
pub fn label_text(bytes: &[u8], charset: u8) -> String {
    let s = match charset {
        15 => String::from_utf8_lossy(bytes).into_owned(),
        6 => {
            let u: Vec<u16> = bytes.chunks_exact(2).map(|c| u16::from_be_bytes([c[0], c[1]])).collect();
            String::from_utf16_lossy(&u)
        }
        _ => bytes.iter().map(|&b| char::from_u32(EBU_LATIN[b as usize] as u32).unwrap_or('?')).collect(),
    };
    s.trim_end_matches(['\0', ' ']).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_puncturing_vector_keeps_eight_plus_its_index() {
        for pi in 1..=24 {
            assert_eq!(PUNCTURE[pi - 1].count_ones() as usize, 8 + pi, "PI {pi}");
        }
        assert_eq!(PUNCTURE_TAIL.count_ones(), 12);
    }

    #[test]
    fn every_uep_profile_fills_the_size_table_8_gives() {
        // Table 15 against Table 8: L1-L4 and PI1-PI4 plus tail and padding
        // must come to the sub-channel's size, and carry 24 x bit rate bits.
        for index in 0..64u8 {
            let p = Protection::Uep { index };
            let prof = p.profile().unwrap_or_else(|| panic!("no profile for index {index}"));
            assert_eq!(prof.coded_bits(), p.size() * CU_BITS, "index {index}");
            assert_eq!(prof.info_bits(), 24 * p.bitrate().unwrap() as usize, "index {index}");
        }
    }

    #[test]
    fn eep_sizes_match_tables_18_and_20() {
        for n in 1..=24u16 {
            for (level, per) in [(1u8, 12u16), (2, 8), (3, 6), (4, 4)] {
                let p = Protection::Eep { option: 0, level, size: per * n };
                let prof = p.profile().unwrap();
                assert_eq!(p.bitrate(), Some(8 * n));
                assert_eq!(prof.coded_bits(), p.size() * CU_BITS, "{level}-A n {n}");
                assert_eq!(prof.info_bits(), 24 * 8 * n as usize);
            }
        }
        for n in 1..=6u16 {
            for (level, per) in [(1u8, 27u16), (2, 21), (3, 18), (4, 15)] {
                let p = Protection::Eep { option: 1, level, size: per * n };
                assert_eq!(p.profile().unwrap().coded_bits(), p.size() * CU_BITS, "{level}-B n {n}");
            }
        }
        assert_eq!(Protection::Eep { option: 0, level: 3, size: 25 }.bitrate(), None);
    }

    #[test]
    fn the_fic_codes_768_bits_into_2304() {
        let p = fic_profile();
        assert_eq!((p.info_bits(), p.coded_bits()), (FIC_INFO_BITS, FIC_CODED_BITS));
        assert_eq!(3 * SYMBOL_BITS, CIFS * FIC_CODED_BITS);
        assert_eq!(CIF_SYMBOLS * CIFS + MSC_FIRST - 1, L);
        assert_eq!(TNULL + L * TS, TF);
    }

    #[test]
    fn frequency_interleaving_starts_as_table_25_prints() {
        let f = frequency_interleave();
        assert_eq!(f.len(), K);
        // Table 25 prints Π(13) as 1 076 but its k, 43, is from 1 067, which
        // is what Π(13) = 13 x 988 + 511 mod 2048 gives.
        assert_eq!(&f[..13], &[-513, -14, 329, 692, -733, 13, 680, 273, -36, 43, 85, -432, -318]);
        assert_eq!(&f[K - 3..], &[652, 606, 197]);
        let mut all = f.clone();
        all.sort();
        all.dedup();
        assert_eq!(all.len(), K);
        assert!(!all.contains(&0) && all[0] == -768 && all[K - 1] == 768);
    }

    #[test]
    fn the_phase_reference_follows_tables_23_and_24() {
        // k = -768: i 0, n 1, h(0,0) = 0 -> 1; k = -767: h(0,1) = 2 -> 3;
        // k = 1: i 0, n 3, h(0,0) -> 3; k = 768: i 1, n 1, h(1,31) = 0 -> 1.
        assert_eq!(prs_quarter_turns(-768), 1);
        assert_eq!(prs_quarter_turns(-767), 3);
        assert_eq!(prs_quarter_turns(1), 3);
        assert_eq!(prs_quarter_turns(768), 1);
        // Jorgen Scott's 2014 correction to welle.io: 97..128 is i 1, not 2.
        assert_eq!(prs_quarter_turns(98), (PRS_H[1][1] + 1) % 4);
    }

    #[test]
    fn labels_read_in_their_charsets() {
        assert_eq!(label_text(b"BBC Radio 4     ", 0), "BBC Radio 4");
        assert_eq!(label_text(&[0x82, b'c', b'o', b'l', b'e'], 0), "\u{e9}cole");
        assert_eq!(label_text("Ünï".as_bytes(), 15), "Ünï");
        assert_eq!(block_centre("11d"), Some(222_064_000));
        assert!(BAND_III.iter().all(|b| b.1 % 16_000 == 0));
    }
}
