//! Many-carrier RDS for the band scanner: a wide IQ capture (a few MHz, up
//! to the RSPdx's 10 MS/s) in; every FM station on the 100 kHz grid
//! between 87.5 and 108 MHz that it holds out, with its level, stereo flag
//! and RDS PI, PS, PTY (and RadioText if asked and there is time), decoded
//! in parallel within a time budget.
//!
//! - **One channeliser for every carrier.** Overlap-save fast convolution:
//!   the capture is cut into overlapping segments of N samples, each is
//!   transformed once, and every carrier takes its own ±rate/2D of bins
//!   from that spectrum (times the channel filter's), inverse-transformed
//!   at N/D points: the channel, mixed down and decimated by D in one step.
//!   D is the power of two that leaves 200-400 kS/s, which the receiver
//!   runs RDS at with no further resampling. So the forward FFT is paid
//!   once, not once a carrier, and memory is a round of segments (about
//!   0.1 s of spectra), never a copy of the capture. The segment's phase is
//!   put back on each carrier's output (`rot`), so the FM discriminator sees
//!   no step at the joins.
//! - **Levels** come from the first round's spectra: each grid point's
//!   power within ±75 kHz (dBFS, a full-scale complex tone is 0 dB), the
//!   noise floor the 30th-percentile bin's power over the same width. A station is a
//!   grid point `min_snr_db` over the floor that is no weaker than its
//!   neighbours (an FM station's skirts reach the next grid point, and a
//!   second station 100 kHz away is a rarity the scanner can live without).
//! - **Each carrier** is a `WfmReceiver` with audio off: the pilot detector
//!   and RDS only. A carrier is done once it has PI, PS and PTY (and
//!   RadioText, with `rt`), and stops costing anything; the scan ends when
//!   every carrier is done, the capture is used up, or the budget is spent
//!   (`timed_out`), returning what each carrier had by then.
//! - **Threads:** each round's segments are transformed across the
//!   threads, then the live carriers are shared out between them. Nothing
//!   is shared mutably; rounds are short enough (0.1 s of signal) that the
//!   budget is checked often.

use super::rds::Fields;
use super::receiver::{Config, WfmReceiver};
use crate::dsp::lowpass;
use rustfft::num_complex::Complex32;
use rustfft::{Fft, FftPlanner};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const BAND_LO: f64 = 87_500_000.0;
pub const BAND_HI: f64 = 108_000_000.0;
pub const GRID: f64 = 100_000.0;

#[derive(Clone, Debug)]
pub struct ScanOptions {
    /// Wall-clock seconds the scan may take, the channeliser included.
    pub budget_s: f64,
    /// Worker threads; 0 is one per core.
    pub threads: usize,
    /// How far over the noise floor (dB, in 150 kHz) a carrier must be.
    pub min_snr_db: f64,
    /// Keep decoding a carrier for RadioText after PI, PS and PTY.
    pub rt: bool,
}

impl Default for ScanOptions {
    fn default() -> Self {
        ScanOptions { budget_s: 3.0, threads: 0, min_snr_db: 10.0, rt: false }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Found {
    pub hz: f64,
    pub level_db: f32,
    pub stereo: bool,
    pub rds_sync: bool,
    pub fields: Fields,
    /// Everything asked for was decoded.
    pub complete: bool,
}

#[derive(Clone, Debug, Default)]
pub struct ScanReport {
    pub stations: Vec<Found>,
    /// Seconds of the capture actually used.
    pub used_s: f64,
    pub timed_out: bool,
    /// CPU time spent across every thread, seconds.
    pub cpu_s: f64,
    pub wall_s: f64,
    pub noise_db: f32,
}

struct Plan {
    n: usize,
    m: usize,
    d: usize,
    skip: usize,
    step: usize,
    h: Vec<Complex32>,
    fwd: Arc<dyn Fft<f32>>,
    inv: Arc<dyn Fft<f32>>,
}

impl Plan {
    fn new(rate: f64) -> Plan {
        let mut d = 1usize;
        while rate / (2 * d) as f64 >= 200_000.0 {
            d *= 2;
        }
        let out = rate / d as f64;
        // Pass ±100 kHz; stop where the output's alias would fold back
        // into it (out - 100 kHz).
        let stop = (out - 100_000.0).max(120_000.0);
        let fc = (100_000.0 + stop) / 2.0;
        let taps = ((5.5 * rate / (stop - 100_000.0)).ceil() as usize) | 1;
        let mut n = 1024usize;
        while n < 8 * taps || n < 4 * d {
            n *= 2;
        }
        let m = n / d;
        let skip = (taps - 1).div_ceil(d);
        let step = (m - skip) * d;
        let mut planner = FftPlanner::<f32>::new();
        let fwd = planner.plan_fft_forward(n);
        let inv = planner.plan_fft_inverse(m);
        let mut h: Vec<Complex32> = lowpass(taps, fc, rate).into_iter().map(|t| Complex32::new(t, 0.0)).collect();
        h.resize(n, Complex32::new(0.0, 0.0));
        fwd.process(&mut h);
        // The inverse transform's 1/N, folded into the filter.
        let k = 1.0 / n as f32;
        h.iter_mut().for_each(|z| *z *= k);
        Plan { n, m, d, skip, step, h, fwd, inv }
    }
}

struct Carrier {
    hz: f64,
    bin: usize,
    level_db: f32,
    rx: WfmReceiver,
    done: bool,
    want_rt: bool,
}

impl Carrier {
    fn check_done(&mut self) {
        let f = self.rx.rds().expect("RDS is on");
        self.done = f.pi.is_some() && f.ps.is_some() && f.pty.is_some() && (!self.want_rt || f.rt.is_some());
    }
}

fn spectrum(plan: &Plan, iq: &[Complex32], seg: usize, buf: &mut Vec<Complex32>, scratch: &mut [Complex32]) {
    let start = seg * plan.step;
    buf.clear();
    let end = (start + plan.n).min(iq.len());
    buf.extend_from_slice(&iq[start.min(end)..end]);
    buf.resize(plan.n, Complex32::new(0.0, 0.0));
    plan.fwd.process_with_scratch(buf, scratch);
}

/// Scan `iq` (at `rate`, centred on `centre_hz`) for FM stations and their RDS.
pub fn scan(iq: &[Complex32], rate: f64, centre_hz: f64, opts: &ScanOptions) -> ScanReport {
    let t_start = Instant::now();
    let deadline = t_start + Duration::from_secs_f64(opts.budget_s.max(0.0));
    let threads = if opts.threads == 0 {
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
    } else {
        opts.threads
    }
    .max(1);
    let mut report = ScanReport::default();
    if !(rate >= 250_000.0) || iq.len() < 4096 {
        report.wall_s = t_start.elapsed().as_secs_f64();
        return report;
    }
    let plan = Plan::new(rate);
    let n_segs = iq.len().saturating_sub(plan.skip * plan.d).div_ceil(plan.step).max(1);
    let round = ((0.1 * rate / plan.step as f64).ceil() as usize).max(1);
    let out_rate = rate / plan.d as f64;
    let mut carriers: Vec<Carrier> = Vec::new();
    let mut cpu = Duration::ZERO;
    let mut spectra: Vec<Vec<Complex32>> = Vec::new();
    let mut seg0 = 0;
    while seg0 < n_segs {
        let segs = round.min(n_segs - seg0);
        spectra.resize_with(segs, Vec::new);
        // The round's forward transforms, across the threads.
        let per = segs.div_ceil(threads);
        cpu += std::thread::scope(|sc| {
            let hs: Vec<_> = spectra
                .chunks_mut(per)
                .enumerate()
                .map(|(t, chunk)| {
                    let plan = &plan;
                    sc.spawn(move || {
                        let c0 = crate::cpu::thread_time();
                        let mut scratch = vec![Complex32::new(0.0, 0.0); plan.fwd.get_inplace_scratch_len()];
                        for (i, buf) in chunk.iter_mut().enumerate() {
                            spectrum(plan, iq, seg0 + t * per + i, buf, &mut scratch);
                        }
                        crate::cpu::thread_time().saturating_sub(c0)
                    })
                })
                .collect();
            hs.into_iter().map(|h| h.join().unwrap_or_default()).sum::<Duration>()
        });
        if seg0 == 0 {
            let c0 = crate::cpu::thread_time();
            report.noise_db = find_carriers(&plan, &spectra, rate, centre_hz, opts, out_rate, &mut carriers);
            cpu += crate::cpu::thread_time().saturating_sub(c0);
        }
        // Each live carrier through this round.
        let live: Vec<&mut Carrier> = carriers.iter_mut().filter(|c| !c.done).collect();
        if live.is_empty() {
            report.used_s = ((seg0 + segs) * plan.step) as f64 / rate;
            break;
        }
        let per = live.len().div_ceil(threads);
        let mut live = live;
        cpu += std::thread::scope(|sc| {
            let hs: Vec<_> = live
                .chunks_mut(per)
                .map(|group| {
                    let (plan, spectra) = (&plan, &spectra);
                    sc.spawn(move || {
                        let c0 = crate::cpu::thread_time();
                        let mut y = vec![Complex32::new(0.0, 0.0); plan.m];
                        let mut scratch = vec![Complex32::new(0.0, 0.0); plan.inv.get_inplace_scratch_len()];
                        let mut chan: Vec<Complex32> = Vec::new();
                        let mut nothing = Vec::new();
                        for c in group.iter_mut() {
                            chan.clear();
                            for (i, x) in spectra.iter().enumerate() {
                                let seg = seg0 + i;
                                channel(plan, x, c.bin, &mut y, &mut scratch);
                                // The segment's start, as a phase of the bin's frequency.
                                let turns = ((c.bin as u128 * (seg * plan.step) as u128) % plan.n as u128) as f64 / plan.n as f64;
                                let a = -2.0 * std::f64::consts::PI * turns;
                                let rot = Complex32::new(a.cos() as f32, a.sin() as f32);
                                chan.extend(y[plan.skip..].iter().map(|z| z * rot));
                            }
                            c.rx.process_iq(&chan, &mut nothing);
                            c.check_done();
                        }
                        crate::cpu::thread_time().saturating_sub(c0)
                    })
                })
                .collect();
            hs.into_iter().map(|h| h.join().unwrap_or_default()).sum::<Duration>()
        });
        seg0 += segs;
        report.used_s = (seg0 * plan.step).min(iq.len()) as f64 / rate;
        if Instant::now() >= deadline {
            report.timed_out = carriers.iter().any(|c| !c.done) && seg0 < n_segs;
            break;
        }
    }
    report.stations = carriers
        .into_iter()
        .map(|c| {
            let st = c.rx.status();
            Found {
                hz: c.hz,
                level_db: c.level_db,
                stereo: st.stereo,
                rds_sync: st.rds_sync,
                fields: c.rx.rds().cloned().unwrap_or_default(),
                complete: c.done,
            }
        })
        .collect();
    report.cpu_s = cpu.as_secs_f64();
    report.wall_s = t_start.elapsed().as_secs_f64();
    report
}

/// One carrier's decimated channel from a segment's spectrum, into `y`
/// (all M outputs; the first `skip` are the overlap's, not yet valid).
fn channel(plan: &Plan, x: &[Complex32], bin: usize, y: &mut [Complex32], scratch: &mut [Complex32]) {
    let (n, m) = (plan.n, plan.m);
    let half = m / 2;
    for k in 0..m {
        // Output bin k is offset kk from the carrier, -M/2..M/2.
        let kk = if k < half { k as isize } else { k as isize - m as isize };
        let src = (bin as isize + kk).rem_euclid(n as isize) as usize;
        let hk = kk.rem_euclid(n as isize) as usize;
        y[k] = x[src] * plan.h[hk];
    }
    plan.inv.process_with_scratch(y, scratch);
}

fn find_carriers(
    plan: &Plan,
    spectra: &[Vec<Complex32>],
    rate: f64,
    centre_hz: f64,
    opts: &ScanOptions,
    out_rate: f64,
    carriers: &mut Vec<Carrier>,
) -> f32 {
    let n = plan.n;
    let mut pw = vec![0.0f64; n];
    for x in spectra {
        for (p, z) in pw.iter_mut().zip(x) {
            *p += z.norm_sqr() as f64;
        }
    }
    let norm = 1.0 / (spectra.len() as f64 * (n as f64) * (n as f64));
    pw.iter_mut().for_each(|p| *p *= norm);
    let bin_hz = rate / n as f64;
    let half_bins = (75_000.0 / bin_hz).round() as isize;
    let bin_of = |f: f64| ((f - centre_hz) / bin_hz).round() as isize;
    let power = |f: f64| -> f64 {
        let c = bin_of(f);
        (c - half_bins..=c + half_bins).map(|k| pw[k.rem_euclid(n as isize) as usize]).sum()
    };
    // The floor from the middle 90 % of the band, a bin's median times the width.
    let edge = 0.45 * rate;
    let mut inside: Vec<f64> = (0..n)
        .filter(|&k| {
            let kk = if k < n / 2 { k as f64 } else { k as f64 - n as f64 };
            (kk * bin_hz).abs() < edge
        })
        .map(|k| pw[k])
        .collect();
    inside.sort_by(|a, b| a.total_cmp(b));
    // The 30th percentile, not the median: a busy band is half stations
    // or more, and their skirts would lift a median. Averaged over a
    // round's segments, a noise bin's power is within 0.3 dB of its mean
    // at that percentile.
    let floor = inside[inside.len() * 3 / 10] * (2 * half_bins + 1) as f64;
    let floor_db = 10.0 * floor.max(1e-30).log10();
    let lo = (BAND_LO.max(centre_hz - edge + GRID) / GRID).ceil() as i64;
    let hi = (BAND_HI.min(centre_hz + edge - GRID) / GRID).floor() as i64;
    let levels: Vec<(f64, f64)> = (lo - 1..=hi + 1).map(|g| (g as f64 * GRID, 10.0 * power(g as f64 * GRID).max(1e-30).log10())).collect();
    for i in 1..levels.len().saturating_sub(1) {
        let (hz, l) = levels[i];
        if l - floor_db < opts.min_snr_db || l < levels[i - 1].1 || l <= levels[i + 1].1 {
            continue;
        }
        let rx = WfmReceiver::new(out_rate, Config { audio: false, ..Config::default() }).expect("RDS-only at 200-400 kS/s is accepted");
        carriers.push(Carrier {
            hz,
            bin: bin_of(hz).rem_euclid(n as isize) as usize,
            level_db: l as f32,
            rx,
            done: false,
            want_rt: opts.rt,
        });
    }
    floor_db as f32
}
