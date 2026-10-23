//! The SSTV test transmitter, its channels and the score a decoded picture
//! gets (RADIO-SDR-PLAN §7.1): every mode in modes.rs from an RGB picture,
//! through AWGN, a tuning offset, the sending sound card's clock error and
//! Watterson fading, and the measures the decoders are compared on - PSNR
//! against the picture sent, and how many rows came out where they belong.
//!
//! Bit-identical on every platform, as testsig.rs is (the golden numbers in
//! testdata/sstv were captured from sstv.py on exactly these samples, and the
//! fixtures are pinned by sha256): only testsig's own sines, noise and
//! exponentials, and IEEE arithmetic.
//!
//! **The clock error is the sender's, modelled exactly, not resampled.** A
//! sound card fast by e plays its samples at fs(1 + e): every duration is
//! 1/(1 + e) as long and every tone (1 + e) as high. So the picture is
//! synthesised in the sender's own time, which runs (1 + e) / fs a received
//! sample; the line period comes out shorter and nothing is interpolated.
//! The tuning offset is the receiver's (an SSB dial off by that much), so it
//! is added after.

use super::modes::{rgb_to_yc, vis_segments, Chan, Colour, Mode, Seg, BLACK_HZ, SYNC_HZ, WHITE_HZ};
use crate::testsig::{db_to_ratio, sin_cos_turns, watterson, Rng, Watterson, SNR_BANDWIDTH};
use rustfft::num_complex::{Complex32, Complex64};

/// What happens to the picture on its way.
#[derive(Clone, Debug)]
pub struct Channel {
    /// In 2.5 kHz (testsig::SNR_BANDWIDTH), as RTTY's; None: no noise.
    pub snr_db: Option<f64>,
    /// The receiver's dial error: every tone moves this far.
    pub offset_hz: f64,
    /// The sender's sound card clock, parts per million fast.
    pub clock_ppm: f64,
    pub fading: Option<Watterson>,
    /// Noise alone before and after the transmission.
    pub lead_s: f64,
    pub tail_s: f64,
    /// The share of the transmission (header included) lost before the
    /// receiver tuned in: a picture joined part way through.
    pub cut: f64,
    pub seed: u64,
    /// Send the VIS header (a sender that leaves it out has no way to say
    /// its mode but its line syncs).
    pub vis: bool,
}

impl Default for Channel {
    fn default() -> Self {
        Channel {
            snr_db: None,
            offset_hz: 0.0,
            clock_ppm: 0.0,
            fading: None,
            lead_s: 0.5,
            tail_s: 1.0,
            cut: 0.0,
            seed: 1,
            vis: true,
        }
    }
}

/// One stretch of the transmission: a tone, or `n` pixels starting at
/// `px` in the pixel list, each a frequency.
#[derive(Clone, Copy, Debug)]
enum Payload {
    Tone(f64),
    Pixels { px: usize, n: usize },
}

fn px_hz(v: f64) -> f64 {
    BLACK_HZ + v.clamp(0.0, 255.0) / 255.0 * (WHITE_HZ - BLACK_HZ)
}

/// The whole transmission as (ms, payload), with the pixel frequencies.
fn plan(mode: &Mode, rgb: &[u8], with_vis: bool) -> (Vec<(f64, Payload)>, Vec<f64>) {
    let (w, h) = (mode.width as usize, mode.height as usize);
    assert_eq!(rgb.len(), w * h * 3, "picture size");
    let at = |x: usize, y: usize, c: usize| rgb[(y * w + x) * 3 + c] as f64;
    // Y, R-Y, B-Y as an encoder sends them: whole numbers, as 8-bit pixels.
    let yc: Vec<[f64; 3]> = (0..w * h)
        .map(|i| {
            let p = rgb_to_yc(rgb[i * 3] as f64, rgb[i * 3 + 1] as f64, rgb[i * 3 + 2] as f64);
            [p[0].round(), p[1].round(), p[2].round()]
        })
        .collect();
    let mut segs: Vec<(f64, Payload)> = Vec::new();
    let mut px: Vec<f64> = Vec::new();
    if with_vis {
        segs.extend(vis_segments(mode.vis).into_iter().map(|(ms, hz)| (ms, Payload::Tone(hz))));
    }
    if mode.head_ms > 0.0 {
        segs.push((mode.head_ms, Payload::Tone(SYNC_HZ)));
    }
    for line in 0..mode.lines() as usize {
        let y0 = line * mode.rows as usize;
        let y1 = (y0 + 1).min(h - 1);
        for s in mode.segs {
            match *s {
                Seg::Sync(ms) => segs.push((ms, Payload::Tone(SYNC_HZ))),
                Seg::Tone(ms, hz) => segs.push((ms, Payload::Tone(hz))),
                // "Even" (1500 Hz) before R-Y, "odd" (2300 Hz) before B-Y.
                Seg::Sep(ms) => segs.push((ms, Payload::Tone(if line % 2 == 0 { BLACK_HZ } else { WHITE_HZ }))),
                Seg::Scan(ms, ch) => {
                    let start = px.len();
                    for x in 0..w {
                        let v = match (mode.colour, ch) {
                            (Colour::Rgb, Chan::R) => at(x, y0, 0),
                            (Colour::Rgb, Chan::G) => at(x, y0, 1),
                            (Colour::Rgb, Chan::B) => at(x, y0, 2),
                            (_, Chan::Y0) => yc[y0 * w + x][0],
                            (_, Chan::Y1) => yc[y1 * w + x][0],
                            // PD's chroma is the pair's average (the paper's
                            // "averaged for two lines"); Robot 72's is the
                            // line's own (y1 == y0 + 1 is unused there:
                            // rows is 1, so y1 only matters for PD).
                            (_, Chan::Ry) if mode.rows == 2 => ((yc[y0 * w + x][1] + yc[y1 * w + x][1]) / 2.0).round(),
                            (_, Chan::By) if mode.rows == 2 => ((yc[y0 * w + x][2] + yc[y1 * w + x][2]) / 2.0).round(),
                            (_, Chan::Ry) => yc[y0 * w + x][1],
                            (_, Chan::By) => yc[y0 * w + x][2],
                            (_, Chan::Chroma) => yc[y0 * w + x][if line % 2 == 0 { 1 } else { 2 }],
                            _ => unreachable!("an RGB mode with a luminance scan"),
                        };
                        px.push(px_hz(v));
                    }
                    segs.push((ms, Payload::Pixels { px: start, n: w }));
                }
            }
        }
    }
    (segs, px)
}

/// The transmission as a complex tone of unit amplitude, its frequency
/// `shift` Hz lower (0 for audio's analytic signal; 1900 to centre the IQ
/// on the leader tone), at `rate`, with the sender's clock and the dial's
/// offset applied.
fn synthesise(mode: &Mode, rgb: &[u8], rate: f64, ch: &Channel, shift: f64) -> Vec<Complex64> {
    let (segs, px) = plan(mode, rgb, ch.vis);
    let k = 1.0 + ch.clock_ppm * 1e-6;
    let dt = k / rate; // sender seconds per received sample
    let total_s: f64 = segs.iter().map(|s| s.0).sum::<f64>() / 1000.0;
    let n = (total_s / dt).floor() as usize;
    let mut out = Vec::with_capacity(n);
    let mut phase = 0.0f64; // turns
    let mut seg = 0usize;
    let mut seg_start = 0.0f64; // sender seconds
    let mut seg_end = segs[0].0 / 1000.0;
    // Segment edges by their cumulative time, never by summed rounded
    // lengths (sstv.py's lesson: that drifts up to 520 ppm).
    let mut cum_ms = segs[0].0;
    for i in 0..n {
        // The middle of this sample's span in the sender's time.
        let t = (i as f64 + 0.5) * dt;
        while t >= seg_end && seg + 1 < segs.len() {
            seg += 1;
            seg_start = seg_end;
            cum_ms += segs[seg].0;
            seg_end = cum_ms / 1000.0;
        }
        let f = match segs[seg].1 {
            Payload::Tone(hz) => hz,
            Payload::Pixels { px: p, n } => {
                let j = (((t - seg_start) / (seg_end - seg_start)) * n as f64).floor() as usize;
                px[p + j.min(n - 1)]
            }
        };
        let (s, c) = sin_cos_turns(phase);
        out.push(Complex64::new(c, s));
        phase += (f * k + ch.offset_hz - shift) / rate;
        phase -= phase.floor();
    }
    out
}

/// The transmission with lead, tail, cut and fading, before noise.
fn on_air(mode: &Mode, rgb: &[u8], rate: f64, ch: &Channel, shift: f64) -> Vec<Complex64> {
    let tx = synthesise(mode, rgb, rate, ch, shift);
    let skip = ((tx.len() as f64) * ch.cut.clamp(0.0, 1.0)).floor() as usize;
    let mut x = vec![Complex64::new(0.0, 0.0); (ch.lead_s * rate).round() as usize];
    x.extend_from_slice(&tx[skip..]);
    x.resize(x.len() + (ch.tail_s * rate).round() as usize, Complex64::new(0.0, 0.0));
    match ch.fading {
        Some(f) => watterson(&x, rate, f, &mut Rng::new(ch.seed)),
        None => x,
    }
}

/// Amplitude of the signal, as testsig's RTTY: 0.5.
const AMP: f64 = 0.5;

/// `rgb` (width x height x 3, the mode's size) sent in `mode` as real audio
/// at `rate`: amplitude 0.5, white noise at the SNR in 2.5 kHz.
pub fn audio(mode: &Mode, rgb: &[u8], rate: f64, ch: &Channel) -> Vec<f32> {
    let x = on_air(mode, rgb, rate, ch, 0.0);
    let sigma = ch
        .snr_db
        .map(|snr| {
            let p_band = AMP * AMP / 2.0 / db_to_ratio(snr);
            (p_band * (rate / 2.0) / SNR_BANDWIDTH).sqrt()
        })
        .unwrap_or(0.0);
    let mut noise = Rng::new(ch.seed ^ 0x7373_7476_6e6f);
    x.iter().map(|z| (AMP * z.re + sigma * noise.gauss()) as f32).collect()
}

/// The same as complex baseband centred on the 1900 Hz leader tone (an SSB
/// signal's IQ with the session's frequency at the dial + 1900 Hz):
/// amplitude 0.5, complex noise at the SNR in 2.5 kHz.
pub fn iq(mode: &Mode, rgb: &[u8], rate: f64, ch: &Channel) -> Vec<Complex32> {
    let x = on_air(mode, rgb, rate, ch, IQ_CENTRE_HZ);
    let sigma = ch
        .snr_db
        .map(|snr| {
            let p_band = AMP * AMP / db_to_ratio(snr);
            (p_band * rate / SNR_BANDWIDTH).sqrt()
        })
        .unwrap_or(0.0);
    let mut noise = Rng::new(ch.seed ^ 0x7373_7476_6e6f);
    x.iter()
        .map(|z| {
            let n = noise.cgauss() * sigma;
            Complex32::new((AMP * z.re + n.re) as f32, (AMP * z.im + n.im) as f32)
        })
        .collect()
}

/// Where IQ is centred: the leader tone.
pub const IQ_CENTRE_HZ: f64 = 1900.0;

// ---------------------------------------------------------------- test card

/// Python's colour bars (sstv.py's tests): each channel on in a different
/// pattern, so a swapped or misplaced channel shows as the wrong colour.
const BARS: [[u8; 3]; 8] =
    [[255, 255, 0], [0, 255, 255], [0, 255, 0], [255, 0, 255], [255, 0, 0], [0, 0, 255], [0, 0, 0], [128, 128, 128]];

/// The picture every case sends, `w` x `h` RGB: the eight bars, each
/// darkening towards the top (so the rows differ, and a row in the wrong
/// place costs PSNR), inside a white border (a picture shifted sideways
/// shows at its edges); a grey ramp across one band (the level mapping);
/// a band of 3-pixel cells, black or white at random (resolution; random
/// rather than a checkerboard, whose period would let a row shifted by a
/// whole period pass as aligned). Every row has edges, so every row's
/// shift can be measured.
pub fn test_card(w: u32, h: u32) -> Vec<u8> {
    let (w, h) = (w as usize, h as usize);
    let b = (w / 40).max(1);
    let mut img = vec![0u8; w * h * 3];
    for y in 0..h {
        for x in 0..w {
            let bar = BARS[(x * 8 / w).min(7)];
            // 35 % at the top to 100 % at the bottom, in whole steps.
            let scale = 35 + 65 * y / (h - 1).max(1);
            let mut p = [0u8; 3];
            for c in 0..3 {
                p[c] = (bar[c] as usize * scale / 100) as u8;
            }
            if (h / 8..h / 4).contains(&y) {
                let g = (255 * x / (w - 1)) as u8;
                p = [g, g, g];
            }
            if (5 * h / 8..3 * h / 4).contains(&y) && (w / 8..7 * w / 8).contains(&x) {
                // A fixed hash of the cell and the row group.
                let cell = ((x / 3) as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15)
                    ^ ((y / 4) as u64).wrapping_mul(0xc2b2_ae3d_27d4_eb4f);
                let v = if (cell >> 29) & 1 == 0 { 230 } else { 25 };
                p = [v, v, v];
            }
            if x < b || x >= w - b || y < b || y >= h - b {
                p = [255, 255, 255];
            }
            img[(y * w + x) * 3..(y * w + x) * 3 + 3].copy_from_slice(&p);
        }
    }
    img
}

// ---------------------------------------------------------------- score

/// How a decoded picture compares with the one sent.
#[derive(Clone, Debug, PartialEq)]
pub struct Score {
    /// Peak signal-to-noise ratio over the rows compared, dB (99 for a
    /// perfect copy). A full picture is compared row for row with every
    /// row it lacks counted as black; a picture joined part way through is
    /// compared where its rows fit best (`row_offset`).
    pub psnr: f64,
    pub rows: usize,
    pub row_offset: usize,
    /// Rows within one pixel of where they belong, of those compared that
    /// have edges to measure.
    pub in_sync: f64,
    /// The largest and mean sideways shift of a row, pixels.
    pub max_shift: f64,
    pub mean_shift: f64,
}

fn luma_row(img: &[u8], w: usize, y: usize) -> Vec<f64> {
    (0..w)
        .map(|x| {
            let p = &img[(y * w + x) * 3..(y * w + x) * 3 + 3];
            0.299 * p[0] as f64 + 0.587 * p[1] as f64 + 0.114 * p[2] as f64
        })
        .collect()
}

/// How far `got` sits to the right of `want` (pixels, sub-pixel): the
/// peak of the cross-correlation of the two rows' gradients.
fn row_shift(want: &[f64], got: &[f64]) -> f64 {
    let gw: Vec<f64> = want.windows(2).map(|p| p[1] - p[0]).collect();
    let gg: Vec<f64> = got.windows(2).map(|p| p[1] - p[0]).collect();
    const R: i64 = 16;
    let n = gw.len() as i64;
    let corr = |s: i64| -> f64 {
        let mut acc = 0.0;
        for i in 0..n {
            let j = i + s;
            if j >= 0 && j < n {
                acc += gw[i as usize] * gg[j as usize];
            }
        }
        acc
    };
    let cs: Vec<f64> = (-R..=R).map(corr).collect();
    let k = (0..cs.len()).max_by(|a, b| cs[*a].total_cmp(&cs[*b])).unwrap();
    let mut frac = 0.0;
    if k > 0 && k + 1 < cs.len() {
        let den = cs[k - 1] - 2.0 * cs[k] + cs[k + 1];
        if den < 0.0 {
            frac = 0.5 * (cs[k - 1] - cs[k + 1]) / den;
        }
    }
    k as f64 - R as f64 + frac
}

/// Each row's sideways shift against `sent` (pixels, signed; None for a
/// row with no edges to measure), rows aligned from `off`.
pub fn row_shifts(sent: &[u8], w: u32, got: &[u8], off: usize) -> Vec<Option<f64>> {
    let w = w as usize;
    (0..got.len() / (3 * w))
        .map(|y| {
            let want = luma_row(sent, w, y + off);
            let edges: f64 = want.windows(2).map(|p| (p[1] - p[0]).abs()).sum();
            (edges > 100.0).then(|| row_shift(&want, &luma_row(got, w, y)))
        })
        .collect()
}

/// Score `got` (`w` wide, `got.len() / 3w` rows) against `sent` (`w` x `h`).
pub fn score(sent: &[u8], w: u32, h: u32, got: &[u8], partial: bool) -> Score {
    let (w, h) = (w as usize, h as usize);
    let rows = got.len() / (3 * w);
    let mse_rows = |off: usize, n: usize| -> f64 {
        let mut acc = 0.0;
        for y in 0..n {
            for i in 0..w * 3 {
                let a = sent[(y + off) * w * 3 + i] as f64;
                let b = if y < rows { got[y * w * 3 + i] as f64 } else { 0.0 };
                acc += (a - b) * (a - b);
            }
        }
        acc / (n * w * 3) as f64
    };
    let (mse, off, compared) = if !partial {
        (mse_rows(0, h), 0, rows.min(h))
    } else if rows == 0 || rows > h {
        (255.0 * 255.0, 0, 0)
    } else {
        (0..=h - rows).map(|off| (mse_rows(off, rows), off, rows)).min_by(|a, b| a.0.total_cmp(&b.0)).unwrap()
    };
    let psnr = if mse <= 0.0 { 99.0 } else { (10.0 * (255.0 * 255.0 / mse).log10()).min(99.0) };
    // Rows with no edges (the border's top and bottom) have no shift to
    // measure.
    let shifts: Vec<f64> = (0..compared)
        .filter_map(|y| {
            let want = luma_row(sent, w, y + off);
            let edges: f64 = want.windows(2).map(|p| (p[1] - p[0]).abs()).sum();
            (edges > 100.0).then(|| row_shift(&want, &luma_row(got, w, y)).abs())
        })
        .collect();
    let n = shifts.len().max(1) as f64;
    Score {
        psnr,
        rows,
        row_offset: off,
        in_sync: shifts.iter().filter(|s| **s <= 1.0).count() as f64 / n,
        max_shift: shifts.iter().cloned().fold(0.0, f64::max),
        mean_shift: shifts.iter().sum::<f64>() / n,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sstv::modes::by_id;

    #[test]
    fn a_perfect_copy_scores_99_and_in_sync() {
        let img = test_card(320, 256);
        let s = score(&img, 320, 256, &img, false);
        assert_eq!(s.psnr, 99.0);
        assert_eq!(s.in_sync, 1.0);
        assert!(s.max_shift < 0.01);
    }

    #[test]
    fn a_shifted_picture_is_measured_as_shifted() {
        let (w, h) = (320usize, 64usize);
        let img = test_card(w as u32, h as u32);
        let mut moved = vec![0u8; img.len()];
        for y in 0..h {
            for x in 3..w {
                for c in 0..3 {
                    moved[(y * w + x) * 3 + c] = img[(y * w + x - 3) * 3 + c];
                }
            }
        }
        let s = score(&img, w as u32, h as u32, &moved, false);
        assert!((s.mean_shift - 3.0).abs() < 0.2, "{s:?}");
        assert_eq!(s.in_sync, 0.0);
    }

    #[test]
    fn a_partial_picture_is_found_where_it_fits() {
        let img = test_card(320, 256);
        let tail = &img[100 * 320 * 3..];
        let s = score(&img, 320, 256, tail, true);
        assert_eq!((s.row_offset, s.rows), (100, 156));
        assert_eq!(s.psnr, 99.0);
    }

    #[test]
    fn the_line_period_is_the_specifications_and_the_clock_error_shortens_it() {
        // Where each sync pulse begins, from the analytic signal's
        // frequency: the encoder must keep to the line period with no
        // rounding drift (sstv.py's EncoderTimingTests), and a sender 200
        // ppm fast must send lines 200 ppm short.
        let m = by_id("martin1").unwrap();
        let img = vec![128u8; 320 * 256 * 3];
        for ppm in [0.0, 200.0] {
            let ch = Channel { lead_s: 0.0, tail_s: 0.0, clock_ppm: ppm, ..Default::default() };
            let x = synthesise(m, &img, 12_000.0, &ch, 0.0);
            let hz = 12_000.0 / (2.0 * std::f64::consts::PI);
            let f: Vec<f64> = x.windows(2).map(|p| (p[1] * p[0].conj()).arg() * hz).collect();
            let starts: Vec<usize> = (1..f.len()).filter(|&i| f[i] < 1350.0 && f[i - 1] >= 1350.0).collect();
            let vis_n = super::super::modes::VIS_MS / 1000.0 * 12_000.0;
            let syncs: Vec<usize> = starts.into_iter().filter(|&s| s as f64 > vis_n / (1.0 + ppm * 1e-6) + 10.0).collect();
            assert_eq!(syncs.len(), 255);
            let p = m.layout().line_ms / 1000.0 * 12_000.0 / (1.0 + ppm * 1e-6);
            let first = syncs[0] as f64;
            for (k, s) in syncs.iter().enumerate() {
                let want = first + p * k as f64;
                assert!((*s as f64 - want).abs() < 1.5, "ppm {ppm} line {k}: {s} vs {want}");
            }
        }
    }

    #[test]
    fn the_same_samples_every_time() {
        let m = by_id("robot36").unwrap();
        let img = test_card(320, 240);
        let ch = Channel { snr_db: Some(10.0), lead_s: 0.1, tail_s: 0.1, ..Default::default() };
        assert_eq!(audio(m, &img, 12_000.0, &ch), audio(m, &img, 12_000.0, &ch));
    }
}
