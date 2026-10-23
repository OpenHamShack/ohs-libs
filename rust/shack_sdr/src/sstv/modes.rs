//! What each SSTV mode sends, in what order and for how long: one table that
//! the decoder (mod.rs) and the test transmitter (tx.rs) both read, so the
//! two can only disagree with the specification, never with each other in
//! a way the tests would miss for one mode alone.
//!
//! **Where the numbers come from.** JL Barber (N7CXI), "Proposal for SSTV
//! Mode Specifications", Dayton SSTV forum, 20 May 2000, as transcribed in
//! openhamshack-nb `ohscore/modes/sstv.py` (Robot 36, Martin M1, Scottie
//! S1, PD120, each checked there against the paper section by section) and
//! in OpenHamShack Icom's `app/sstv/modes.py` (the other ten, whose picture
//! times that file checks against QSSTV's parameter table: every mode
//! within 80 ppm, all on the same side, which is a convention for where a
//! line is counted from rather than an error). Both are ours. QSSTV's
//! figures are facts quoted, not code read.
//!
//! A line here starts where the transmitter's repeating unit starts: at the
//! sync pulse for Martin, Robot and PD, and at the separator before green
//! for Scottie, whose sync sits between blue and red (so a line cut at the
//! sync would put one line's red beside the next line's green). Scottie
//! alone also sends a lone 9 ms "starting" sync before its first line
//! (`head_ms`); some senders leave it out, which the decoder's first wide
//! sync search absorbs.
//!
//! Left out, as Icom's table leaves them out: Wraase SC2 (the references
//! disagree about its line structure), Pasokon, AVT, the MMSSTV MP/MR/ML
//! family (a 16-bit VIS) and the black-and-white Robots, which are hardly
//! ever sent. `UNSUPPORTED` names their VIS codes so the feed can say what
//! was heard rather than nothing.

/// The tones, Hz. A pixel is linear from black to white; the sync sits
/// below black, so no picture can make one.
pub const SYNC_HZ: f64 = 1200.0;
pub const BLACK_HZ: f64 = 1500.0;
pub const WHITE_HZ: f64 = 2300.0;

/// One scan's content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Chan {
    R,
    G,
    B,
    /// Luminance of the line's (first) picture row.
    Y0,
    /// Luminance of a PD line's second row.
    Y1,
    /// R-Y, B-Y: once per line (Robot 72), or once for a PD line's two rows.
    Ry,
    By,
    /// Robot 36's one chroma scan: R-Y on even lines, B-Y on odd, which the
    /// separator before it says (`Seg::Sep`).
    Chroma,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Seg {
    /// The line's sync pulse at 1200 Hz, ms.
    Sync(f64),
    /// A porch or separator: ms at a fixed tone.
    Tone(f64, f64),
    /// Robot 36's separator: 1500 Hz before R-Y ("even"), 2300 before B-Y.
    Sep(f64),
    /// Pixels, ms.
    Scan(f64, Chan),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Colour {
    Rgb,
    /// Y, R-Y, B-Y, scaled as the Dayton paper's Appendix B (BT.601).
    Yc,
}

#[derive(Debug)]
pub struct Mode {
    /// The id the decoder's feed and parameters use.
    pub id: &'static str,
    /// The usual name, for logs and tests (never shown: the app words ids).
    pub name: &'static str,
    /// Seven-bit VIS code, without its parity bit.
    pub vis: u8,
    pub width: u32,
    pub height: u32,
    /// Picture rows per transmitted line: 2 for PD, 1 for the rest.
    pub rows: u32,
    pub colour: Colour,
    pub segs: &'static [Seg],
    /// Sent once before the first line (Scottie's starting sync), ms.
    pub head_ms: f64,
}

const fn martin(id: &'static str, name: &'static str, vis: u8, segs: &'static [Seg]) -> Mode {
    Mode { id, name, vis, width: 320, height: 256, rows: 1, colour: Colour::Rgb, segs, head_ms: 0.0 }
}

const fn scottie(id: &'static str, name: &'static str, vis: u8, segs: &'static [Seg]) -> Mode {
    Mode { id, name, vis, width: 320, height: 256, rows: 1, colour: Colour::Rgb, segs, head_ms: 9.0 }
}

const fn pd(id: &'static str, name: &'static str, vis: u8, width: u32, height: u32, segs: &'static [Seg]) -> Mode {
    Mode { id, name, vis, width, height, rows: 2, colour: Colour::Yc, segs, head_ms: 0.0 }
}

macro_rules! martin_segs {
    ($scan:expr) => {
        &[
            Seg::Sync(4.862),
            Seg::Tone(0.572, BLACK_HZ),
            Seg::Scan($scan, Chan::G),
            Seg::Tone(0.572, BLACK_HZ),
            Seg::Scan($scan, Chan::B),
            Seg::Tone(0.572, BLACK_HZ),
            Seg::Scan($scan, Chan::R),
            // The fourth separator, before the next line's sync: step 8 of
            // the paper's sequence (sstv.py once left it out, 0.572 ms
            // short of the paper's own 446.446 ms line).
            Seg::Tone(0.572, BLACK_HZ),
        ]
    };
}

macro_rules! scottie_segs {
    ($scan:expr) => {
        &[
            Seg::Tone(1.5, BLACK_HZ),
            Seg::Scan($scan, Chan::G),
            Seg::Tone(1.5, BLACK_HZ),
            Seg::Scan($scan, Chan::B),
            Seg::Sync(9.0),
            Seg::Tone(1.5, BLACK_HZ),
            Seg::Scan($scan, Chan::R),
        ]
    };
}

/// PD: luminance of the first row, the pair's R-Y and B-Y (each averaged
/// over the two rows), luminance of the second; `$scan` is width x the
/// pixel time.
macro_rules! pd_segs {
    ($scan:expr) => {
        &[
            Seg::Sync(20.0),
            Seg::Tone(2.08, BLACK_HZ),
            Seg::Scan($scan, Chan::Y0),
            Seg::Scan($scan, Chan::Ry),
            Seg::Scan($scan, Chan::By),
            Seg::Scan($scan, Chan::Y1),
        ]
    };
}

pub static MODES: &[Mode] = &[
    martin("martin1", "Martin M1", 0x2C, martin_segs!(146.432)),
    martin("martin2", "Martin M2", 0x28, martin_segs!(73.216)),
    scottie("scottie1", "Scottie S1", 0x3C, scottie_segs!(138.240)),
    scottie("scottie2", "Scottie S2", 0x38, scottie_segs!(88.064)),
    scottie("scottiedx", "Scottie DX", 0x4C, scottie_segs!(345.600)),
    Mode {
        id: "robot36",
        name: "Robot 36",
        vis: 0x08,
        width: 320,
        height: 240,
        rows: 1,
        colour: Colour::Yc,
        segs: &[
            Seg::Sync(9.0),
            Seg::Tone(3.0, BLACK_HZ),
            Seg::Scan(88.0, Chan::Y0),
            Seg::Sep(4.5),
            // The 1.5 ms porch at 1900 Hz before the chroma (sstv.py once
            // left it out: 18 samples of every chroma scan misplaced).
            Seg::Tone(1.5, 1900.0),
            Seg::Scan(44.0, Chan::Chroma),
        ],
        head_ms: 0.0,
    },
    Mode {
        id: "robot72",
        name: "Robot 72",
        vis: 0x0C,
        width: 320,
        height: 240,
        rows: 1,
        colour: Colour::Yc,
        segs: &[
            Seg::Sync(9.0),
            Seg::Tone(3.0, BLACK_HZ),
            Seg::Scan(138.0, Chan::Y0),
            Seg::Tone(4.5, BLACK_HZ),
            Seg::Tone(1.5, 1900.0),
            Seg::Scan(69.0, Chan::Ry),
            Seg::Tone(4.5, WHITE_HZ),
            Seg::Tone(1.5, 1900.0),
            Seg::Scan(69.0, Chan::By),
        ],
        head_ms: 0.0,
    },
    pd("pd50", "PD50", 0x5D, 320, 256, pd_segs!(91.52)),
    pd("pd90", "PD90", 0x63, 320, 256, pd_segs!(170.24)),
    pd("pd120", "PD120", 0x5F, 640, 496, pd_segs!(121.6)),
    pd("pd160", "PD160", 0x62, 512, 400, pd_segs!(195.584)),
    pd("pd180", "PD180", 0x60, 640, 496, pd_segs!(183.04)),
    pd("pd240", "PD240", 0x61, 640, 496, pd_segs!(244.48)),
    pd("pd290", "PD290", 0x5E, 800, 616, pd_segs!(228.8)),
];

/// VIS codes of real modes not decoded here, so the feed can name them.
pub static UNSUPPORTED: &[(u8, &str)] = &[
    (0x00, "robot12bw"),
    (0x02, "robot8bw"),
    (0x04, "robot24"),
    (0x06, "robot12bw"),
    (0x0A, "robot24bw"),
    (0x0E, "robot36bw"),
    (0x20, "martin4"),
    (0x24, "martin3"),
    (0x30, "scottie4"),
    (0x34, "scottie3"),
    (0x37, "wraase_sc2_180"),
    (0x3B, "wraase_sc2_60"),
    (0x3F, "wraase_sc2_120"),
    (0x40, "avt24"),
    (0x44, "avt90"),
    (0x48, "avt94"),
    (0x71, "pasokon_p3"),
    (0x72, "pasokon_p5"),
    (0x73, "pasokon_p7"),
    // Not a mode: the escape for a 16-bit VIS (MMSSTV's MP, MR, ML).
    (0x23, "extended_vis"),
];

pub fn by_id(id: &str) -> Option<&'static Mode> {
    MODES.iter().find(|m| m.id == id)
}

pub fn by_vis(vis: u8) -> Option<&'static Mode> {
    MODES.iter().find(|m| m.vis == vis)
}

/// Where everything sits in one line, ms from the line's start.
#[derive(Clone, Debug)]
pub struct Layout {
    pub line_ms: f64,
    pub sync_off_ms: f64,
    pub sync_ms: f64,
    /// (what, start, duration), in the order sent.
    pub scans: Vec<(Chan, f64, f64)>,
    /// Robot 36's separator (start, duration).
    pub sep: Option<(f64, f64)>,
    /// Where the line's last pixel ends: a transmission that stops there
    /// still completes its last line.
    pub last_pixel_ms: f64,
}

impl Mode {
    pub fn layout(&self) -> Layout {
        let mut t = 0.0;
        let mut l = Layout { line_ms: 0.0, sync_off_ms: 0.0, sync_ms: 0.0, scans: Vec::new(), sep: None, last_pixel_ms: 0.0 };
        for s in self.segs {
            match *s {
                Seg::Sync(ms) => {
                    l.sync_off_ms = t;
                    l.sync_ms = ms;
                    t += ms;
                }
                Seg::Tone(ms, _) => t += ms,
                Seg::Sep(ms) => {
                    l.sep = Some((t, ms));
                    t += ms;
                }
                Seg::Scan(ms, ch) => {
                    l.scans.push((ch, t, ms));
                    t += ms;
                    l.last_pixel_ms = t;
                }
            }
        }
        l.line_ms = t;
        l
    }

    /// Transmitted lines (not rows: a PD line carries two).
    pub fn lines(&self) -> u32 {
        self.height / self.rows
    }

    /// The picture after its VIS header, seconds.
    pub fn seconds(&self) -> f64 {
        (self.head_ms + self.layout().line_ms * self.lines() as f64) / 1000.0
    }
}

// ---------------------------------------------------------------- VIS

/// The VIS header: leader 300 ms at 1900, break 10 ms at 1200, leader 300
/// ms, start bit 30 ms at 1200, seven data bits LSB first and even parity
/// (30 ms each; 1 = 1100 Hz, 0 = 1300, the paper's convention), stop bit
/// 30 ms at 1200.
pub const LEADER_MS: f64 = 300.0;
pub const BREAK_MS: f64 = 10.0;
pub const BIT_MS: f64 = 30.0;
pub const LEADER_HZ: f64 = 1900.0;
pub const VIS_ONE_HZ: f64 = 1100.0;
pub const VIS_ZERO_HZ: f64 = 1300.0;
/// From the first leader's start to the end of the stop bit.
pub const VIS_MS: f64 = 2.0 * LEADER_MS + BREAK_MS + 10.0 * BIT_MS;

/// (ms, Hz) of the whole header for `code`.
pub fn vis_segments(code: u8) -> Vec<(f64, f64)> {
    let mut v = vec![(LEADER_MS, LEADER_HZ), (BREAK_MS, SYNC_HZ), (LEADER_MS, LEADER_HZ), (BIT_MS, SYNC_HZ)];
    let mut parity = 0;
    for i in 0..7 {
        let b = (code >> i) & 1;
        parity ^= b;
        v.push((BIT_MS, if b == 1 { VIS_ONE_HZ } else { VIS_ZERO_HZ }));
    }
    v.push((BIT_MS, if parity == 1 { VIS_ONE_HZ } else { VIS_ZERO_HZ }));
    v.push((BIT_MS, SYNC_HZ));
    v
}

// ---------------------------------------------------------------- colour

/// RGB to Y, R-Y, B-Y, 0-255, the Dayton paper's Appendix B (as sstv.py).
pub fn rgb_to_yc(r: f64, g: f64, b: f64) -> [f64; 3] {
    let y = 16.0 + 0.003906 * (65.738 * r + 129.057 * g + 25.064 * b);
    let ry = 128.0 + 0.003906 * (112.439 * r - 94.154 * g - 18.285 * b);
    let by = 128.0 + 0.003906 * (-37.945 * r - 74.494 * g + 112.439 * b);
    [y.clamp(0.0, 255.0), ry.clamp(0.0, 255.0), by.clamp(0.0, 255.0)]
}

/// Its inverse, unclamped.
pub fn yc_to_rgb(y: f64, ry: f64, by: f64) -> [f64; 3] {
    let y = y - 16.0;
    let ry = ry - 128.0;
    let by = by - 128.0;
    [
        0.003906 * (298.082 * y + 408.583 * ry),
        0.003906 * (298.082 * y - 100.291 * by - 208.120 * ry),
        0.003906 * (298.082 * y + 516.411 * by),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_lengths_are_the_specifications() {
        // sstv.py's and its tests' figures for its four, the paper's for the rest.
        let want = [
            ("martin1", 446.446),
            ("martin2", 226.798),
            ("scottie1", 428.22),
            ("scottie2", 277.692),
            ("scottiedx", 1050.3),
            ("robot36", 150.0),
            ("robot72", 300.0),
            ("pd120", 508.48),
            ("pd290", 937.28),
        ];
        for (id, ms) in want {
            let l = by_id(id).unwrap().layout();
            assert!((l.line_ms - ms).abs() < 1e-6, "{id}: {}", l.line_ms);
        }
    }

    #[test]
    fn picture_times_agree_with_qsstvs_table_within_80_ppm() {
        // QSSTV's picture times as Icom's modes.py quotes them.
        let qsstv = [
            ("martin1", 114.29700),
            ("martin2", 58.06400),
            ("scottie1", 109.63250),
            ("scottie2", 71.09450),
            ("scottiedx", 268.89380),
            ("robot36", 36.00200),
            ("robot72", 72.00500),
            ("pd50", 49.68770),
            ("pd90", 89.99500),
            ("pd120", 126.11150),
            ("pd160", 160.89420),
            ("pd180", 187.06450),
            ("pd240", 248.01700),
            ("pd290", 288.70200),
        ];
        for (id, s) in qsstv {
            let m = by_id(id).unwrap();
            let ours = m.layout().line_ms * m.lines() as f64 / 1000.0;
            let ppm = (ours - s) / s * 1e6;
            assert!(ppm.abs() < 80.0, "{id}: {ppm} ppm");
        }
    }

    #[test]
    fn vis_codes_are_unique_and_have_even_parity() {
        for m in MODES {
            assert_eq!(MODES.iter().filter(|o| o.vis == m.vis).count(), 1, "{}", m.id);
            let segs = vis_segments(m.vis);
            let ones = segs[4..12].iter().filter(|s| s.1 == VIS_ONE_HZ).count();
            assert_eq!(ones % 2, 0, "{}", m.id);
            assert!(UNSUPPORTED.iter().all(|u| u.0 != m.vis));
        }
        // Robot 36, VIS 8: bits 0001000 LSB first, parity 1 (sstv.py's test).
        let bits: Vec<u8> = vis_segments(8)[4..12].iter().map(|s| u8::from(s.1 == VIS_ONE_HZ)).collect();
        assert_eq!(bits, vec![0, 0, 0, 1, 0, 0, 0, 1]);
    }

    #[test]
    fn colour_round_trips() {
        for &(r, g, b) in &[(0.0, 0.0, 0.0), (255.0, 255.0, 255.0), (200.0, 50.0, 220.0), (10.0, 200.0, 90.0)] {
            let [y, ry, by] = rgb_to_yc(r, g, b);
            let [r2, g2, b2] = yc_to_rgb(y, ry, by);
            assert!((r - r2).abs() < 1.5 && (g - g2).abs() < 1.5 && (b - b2).abs() < 1.5, "{r} {g} {b}");
        }
    }
}
