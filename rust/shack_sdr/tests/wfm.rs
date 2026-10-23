//! Broadcast FM through the test transmitter (src/wfm/tx.rs), measured:
//! stereo separation, distortion and response; the pilot detector and its
//! hysteresis; the blend and the SNR estimate behind it; RDS at falling
//! carrier-to-noise ratios, through multipath, a tuning offset and clock
//! errors, with its block error rate; and that no published RDS field is
//! ever wrong, half-changed or stale after a retune.
//!
//! Carrier-to-noise ratios here are over the whole sampled band (240 kHz),
//! about 1.2 dB worse than the same signal in a 180 kHz channel.
//! `cargo test --release --test wfm -- --nocapture` prints the tables.

use rustfft::num_complex::Complex32;
use shack_sdr::wfm::rds::{date_to_mjd, Ct, Fields, Station};
use shack_sdr::wfm::tx::{self, Audio, Channel, Tx};
use shack_sdr::wfm::{Config, WfmReceiver};

const RATE: f64 = 240_000.0;
const AR: f64 = 48_000.0;

fn station() -> Station {
    let mut st = Station::new(0xC201, "TEST FM ", 10);
    st.rt = b"Hello from the test transmitter".to_vec();
    st.af = vec![89_100_000, 91_300_000, 93_500_000];
    st.tp = true;
    st.ct = Some(Ct { mjd: date_to_mjd(2026, 10, 23), hour: 14, minute: 5, offset_half_hours: 2 });
    st
}

fn iq(tx: &Tx, secs: f64, ch: &Channel) -> Vec<Complex32> {
    tx::channel(&tx::fm(&tx::mpx(tx, secs), tx.rate), tx.rate, ch)
}

/// Feed in 20 ms blocks; audio out, and every RDS field set as published.
fn receive(x: &[Complex32], cfg: Config) -> (Vec<f32>, WfmReceiver, Vec<Fields>) {
    let mut rx = WfmReceiver::new(RATE, cfg).unwrap();
    let mut out = Vec::new();
    let mut seen: Vec<Fields> = Vec::new();
    for c in x.chunks(4800) {
        rx.process_iq(c, &mut out);
        if let Some(f) = rx.rds() {
            if seen.last() != Some(f) {
                seen.push(f.clone());
            }
        }
    }
    (out, rx, seen)
}

fn chan(out: &[f32], ch: usize, skip_s: f64) -> Vec<f64> {
    out.iter().skip(ch).step_by(2).skip((skip_s * AR) as usize).map(|v| *v as f64).collect()
}

/// Amplitude of `hz` in `x` (a least-squares fit over whole cycles).
fn tone(x: &[f64], hz: f64) -> f64 {
    let (mut c, mut s) = (0.0, 0.0);
    for (i, v) in x.iter().enumerate() {
        let a = 2.0 * std::f64::consts::PI * hz * i as f64 / AR;
        c += v * a.cos();
        s += v * a.sin();
    }
    2.0 * (c * c + s * s).sqrt() / x.len() as f64
}

/// THD+N: what is left after the fundamental (and DC) is removed, against it.
fn thd_n(x: &[f64], hz: f64) -> f64 {
    let n = x.len() as f64;
    let (mut c, mut s) = (0.0, 0.0);
    let mean = x.iter().sum::<f64>() / n;
    for (i, v) in x.iter().enumerate() {
        let a = 2.0 * std::f64::consts::PI * hz * i as f64 / AR;
        c += (v - mean) * a.cos();
        s += (v - mean) * a.sin();
    }
    let (c, s) = (2.0 * c / n, 2.0 * s / n);
    let mut resid = 0.0;
    for (i, v) in x.iter().enumerate() {
        let a = 2.0 * std::f64::consts::PI * hz * i as f64 / AR;
        resid += (v - mean - c * a.cos() - s * a.sin()).powi(2);
    }
    let fund = (c * c + s * s) / 2.0;
    10.0 * (resid / n / fund).log10()
}

fn db(x: f64) -> f64 {
    20.0 * x.log10()
}

#[test]
fn stereo_separation_distortion_and_response_on_a_clean_signal() {
    let quiet = Channel { cnr_db: Some(50.0), seed: 1, ..Default::default() };
    println!("separation (L only, then R only), 50 dB CNR:");
    for hz in [100.0, 1_000.0, 5_000.0, 10_000.0, 15_000.0] {
        // Pre-emphasis lifts the top fivefold at 15 kHz: at a programme's
        // real level there (a tenth of its bass, or less) and not at a level
        // that sends L-R's 53 kHz sideband past what 240 kS/s can carry.
        let amp = if hz > 4_000.0 { 0.05 } else { 0.5 };
        let mut t = Tx::new(RATE);
        t.left = Audio::Tone { hz, amp };
        // The stereo indicator takes 0.15 s and the blend half a second more.
        let (out, rx, _) = receive(&iq(&t, 2.0, &quiet), Config::default());
        assert!(rx.status().stereo);
        let (l, r) = (tone(&chan(&out, 0, 1.0), hz), tone(&chan(&out, 1, 1.0), hz));
        let mut t = Tx::new(RATE);
        t.right = Audio::Tone { hz, amp };
        let (out, _, _) = receive(&iq(&t, 2.0, &quiet), Config::default());
        let (l2, r2) = (tone(&chan(&out, 0, 1.0), hz), tone(&chan(&out, 1, 1.0), hz));
        let (sep_l, sep_r) = (db(l / r), db(r2 / l2));
        // The level a tone went in at (0.5 x 0.87 x amp): flat response.
        let resp = db(l / (0.5 * 0.87 * amp));
        println!("  {hz:>6} Hz: {sep_l:5.1} dB, {sep_r:5.1} dB; response {resp:+.2} dB");
        assert!(sep_l > 45.0 && sep_r > 45.0, "{hz}: {sep_l} {sep_r}");
        assert!(resp.abs() < 0.5, "{hz}: {resp}");
    }
    // THD+N of a full-deviation 1 kHz mono tone, and of a stereo one.
    let mut t = Tx::new(RATE);
    t.left = Audio::Tone { hz: 1_000.0, amp: 1.0 };
    t.right = t.left.clone();
    let (out, _, _) = receive(&iq(&t, 1.5, &Channel::default()), Config::default());
    let mono = thd_n(&chan(&out, 0, 0.5), 1_000.0);
    let mut t = Tx::new(RATE);
    t.left = Audio::Tone { hz: 1_000.0, amp: 1.0 };
    let (out, _, _) = receive(&iq(&t, 2.0, &Channel::default()), Config::default());
    let stereo = thd_n(&chan(&out, 0, 1.0), 1_000.0);
    println!("THD+N at 1 kHz, full deviation, no noise: L=R {mono:.1} dB, L only {stereo:.1} dB");
    assert!(mono < -60.0 && stereo < -60.0, "{mono} {stereo}");
}

#[test]
fn no_pilot_is_mono_and_rds_still_decodes() {
    let mut t = Tx::new(RATE);
    t.pilot = 0.0;
    t.left = Audio::Tone { hz: 1_000.0, amp: 0.5 };
    t.rds = Some(station());
    let (out, rx, _) = receive(&iq(&t, 4.0, &Channel { cnr_db: Some(30.0), seed: 2, ..Default::default() }), Config::default());
    let st = rx.status();
    assert!(!st.stereo && st.blend == 0.0 && st.pilot < 0.01, "{st:?}");
    let (l, r) = (chan(&out, 0, 0.5), chan(&out, 1, 0.5));
    assert!(l.iter().zip(&r).all(|(a, b)| a == b));
    // A mono L-only programme is (L+R)/2 in both.
    assert!((tone(&l, 1_000.0) - 0.5 * 0.87 * 0.25).abs() < 0.002, "{}", tone(&l, 1_000.0));
    let f = rx.rds().unwrap();
    assert_eq!((f.pi, f.ps.as_deref()), (Some(0xC201), Some("TEST FM ")));
}

#[test]
fn the_stereo_indicator_follows_the_pilot_with_hysteresis() {
    // Pilot for 1 s, gone for 1 s, back for 1 s; and a burst of 80 ms
    // without it, which must not flick the indicator.
    let mut on = Tx::new(RATE);
    on.left = Audio::Noise { amp: 0.2, seed: 5 };
    let mut off = on.clone();
    off.pilot = 0.0;
    let a = tx::mpx(&on, 1.0);
    let b = tx::mpx(&off, 1.0);
    let mut m = Vec::new();
    m.extend_from_slice(&a);
    m.extend_from_slice(&b);
    m.extend_from_slice(&a);
    m.extend_from_slice(&b[..(0.08 * RATE) as usize]);
    m.extend_from_slice(&a[..(0.5 * RATE) as usize]);
    let x = tx::channel(&tx::fm(&m, RATE), RATE, &Channel { cnr_db: Some(25.0), seed: 4, ..Default::default() });
    let mut rx = WfmReceiver::new(RATE, Config::default()).unwrap();
    let mut out = Vec::new();
    let mut flips = Vec::new();
    let mut was = false;
    for (k, c) in x.chunks(2400).enumerate() {
        rx.process_iq(c, &mut out);
        let s = rx.status().stereo;
        if s != was {
            flips.push((k as f64 * 0.01, s));
            was = s;
        }
    }
    println!("stereo indicator: {flips:?}");
    assert_eq!(flips.len(), 3, "{flips:?}");
    for ((t, s), (base, ws)) in flips.iter().zip([(0.0, true), (1.0, false), (2.0, true)]) {
        assert_eq!(*s, ws);
        let lag = t - base;
        // On in 0.15 s plus the loop's settling, off in 0.3 s plus the
        // amplitude's fall: never as quick as the 80 ms gap.
        assert!((0.1..0.6).contains(&lag), "{t} {s}");
    }
}

#[test]
fn the_snr_estimate_tracks_the_measured_stereo_noise_and_the_blend_follows() {
    println!("CNR   stereo SNR  estimate  blend   mono SNR");
    for cnr in [40.0, 30.0, 25.0, 20.0, 15.0, 12.0, 10.0] {
        let t = Tx::new(RATE);
        let x = iq(&t, 2.5, &Channel { cnr_db: Some(cnr), seed: 3, ..Default::default() });
        let (out, rx, _) = receive(&x, Config { blend: false, ..Config::default() });
        let (l, r) = (chan(&out, 0, 1.0), chan(&out, 1, 1.0));
        let pr = r.iter().map(|v| v * v).sum::<f64>() / r.len() as f64;
        let pm = l.iter().zip(&r).map(|(a, b)| ((a + b) / 2.0).powi(2)).sum::<f64>() / r.len() as f64;
        // Full scale (0.87 of deviation) in one channel comes out at 0.5 x 0.87.
        let refp = (0.5f64 * 0.87).powi(2) / 2.0;
        let (snr, mono) = (10.0 * (refp / pr).log10(), 10.0 * (refp / pm).log10());
        let est = rx.status().snr_db.unwrap() as f64;
        let (_, rx2, _) = receive(&x, Config::default());
        let blend = rx2.status().blend;
        println!("{cnr:>4}  {snr:8.1}  {est:8.1}  {blend:6.2}  {mono:8.1}");
        // Above 50 dB the probe sees its own floor; the blend is full long before.
        if snr < 50.0 {
            assert!((est - snr).abs() < 1.5, "{cnr}: {est} vs {snr}");
        } else {
            assert!(est > 50.0, "{cnr}: {est}");
        }
        let want = ((snr - 26.0) / 14.0).clamp(0.0, 1.0) as f32;
        assert!((blend - want).abs() < 0.12, "{cnr}: blend {blend}, {want}");
    }
}

/// Each published field set must be the truth's, field by field, where set.
fn check_fields(seen: &[Fields], st: &Station) {
    let ps = String::from_utf8(st.ps.to_vec()).unwrap();
    let rt = String::from_utf8(st.rt.clone()).unwrap();
    for f in seen {
        assert!(f.pi.is_none() || f.pi == Some(st.pi), "{f:?}");
        assert!(f.ps.is_none() || f.ps.as_deref() == Some(ps.as_str()), "{f:?}");
        assert!(f.rt.is_none() || f.rt.as_deref() == Some(rt.as_str()), "{f:?}");
        assert!(f.pty.is_none() || f.pty == Some(st.pty), "{f:?}");
        assert!(f.ct.is_none() || f.ct == st.ct, "{f:?}");
        assert!(f.af.iter().all(|a| st.af.contains(a)), "{f:?}");
    }
}

#[test]
fn rds_through_offsets_clock_errors_quadrature_and_multipath() {
    let cases: Vec<(&str, Tx, Channel)> = {
        let mut v = Vec::new();
        let base = || {
            let mut t = Tx::new(RATE);
            t.left = Audio::Noise { amp: 0.25, seed: 7 };
            t.right = Audio::Noise { amp: 0.25, seed: 8 };
            t.rds = Some(station());
            t
        };
        let ch = |cnr: f64| Channel { cnr_db: Some(cnr), seed: 9, ..Default::default() };
        v.push(("plain, 20 dB", base(), ch(20.0)));
        v.push(("tuned 25 kHz off", base(), Channel { offset_hz: 25_000.0, ..ch(20.0) }));
        let mut t = base();
        t.clock_ppm = 100.0;
        v.push(("station clock +100 ppm", t, ch(20.0)));
        let mut t = base();
        t.clock_ppm = -100.0;
        v.push(("station clock -100 ppm", t, ch(20.0)));
        let mut t = base();
        t.rds_quadrature = true;
        v.push(("RDS in quadrature", t, ch(20.0)));
        let mut t = base();
        t.pilot = 0.0;
        v.push(("mono, no pilot", t, ch(20.0)));
        v.push(("two echoes (8 and 21 µs)", base(), Channel { echoes: vec![(2, 0.3, 0.2), (5, 0.15, 0.7)], ..ch(25.0) }));
        v
    };
    for (name, t, ch) in cases {
        let st = t.rds.clone().unwrap();
        let (_, rx, seen) = receive(&iq(&t, 8.0, &ch), Config::default());
        let f = rx.rds().unwrap();
        let s = rx.status();
        println!("{name}: {:?}, rds snr {:.1} dB, {:?}", f.ps, s.rds_snr_db, s.rds_counts);
        check_fields(&seen, &st);
        assert_eq!(f.pi, Some(0xC201), "{name}");
        assert_eq!(f.ps.as_deref(), Some("TEST FM "), "{name}");
        assert_eq!(f.rt.as_deref(), Some("Hello from the test transmitter"), "{name}");
        assert_eq!((f.pty, f.tp, f.ta), (Some(10), Some(true), Some(false)), "{name}");
        assert_eq!(f.ct.unwrap().iso(), "2026-10-23T15:05:00+01:00", "{name}");
        assert_eq!(f.af, vec![89_100_000, 91_300_000, 93_500_000], "{name}");
    }
}

#[test]
fn rds_block_error_rate_against_cnr_and_no_wrong_field() {
    let st = station();
    let sent: Vec<[u16; 4]> = (0..2000).map(|n| st.group(n)).collect();
    println!("CNR  RDS SNR  blocks good  corrected  BLER    PS after  wrong blocks");
    let mut last_bler = 0.0;
    let mut wrong_total = 0;
    for cnr in [20.0, 15.0, 12.0, 10.0, 9.0, 8.0, 7.0, 6.0] {
        let mut t = Tx::new(RATE);
        t.left = Audio::Noise { amp: 0.25, seed: 7 };
        t.right = Audio::Noise { amp: 0.25, seed: 8 };
        t.rds = Some(st.clone());
        let secs = 10.0;
        let x = iq(&t, secs, &Channel { cnr_db: Some(cnr), seed: 11, ..Default::default() });
        let mut rx = WfmReceiver::new(RATE, Config { audio: false, ..Config::default() }).unwrap();
        rx.rds_decoder().unwrap().keep_groups = true;
        let mut out = Vec::new();
        let mut ps_at = None;
        let mut seen = Vec::new();
        for (k, c) in x.chunks(4800).enumerate() {
            rx.process_iq(c, &mut out);
            let f = rx.rds().unwrap();
            if ps_at.is_none() && f.ps.is_some() {
                ps_at = Some(k as f64 * 0.02);
            }
            seen.push(f.clone());
        }
        // What a listener sees is never wrong, however bad the signal.
        check_fields(&seen, &st);
        let s = rx.status();
        let c = s.rds_counts;
        let expected = secs * 1187.5 / 26.0;
        let good = (c.ok + c.corrected) as f64;
        let bler = 1.0 - (good / expected).min(1.0);
        // Blocks accepted that were not sent in that place: a correction
        // must never make one; an undetected error (1 in 1024 of noise
        // blocks, the code's own limit) rarely does.
        let groups = std::mem::take(&mut rx.rds_decoder().unwrap().groups);
        let mut wrong = 0;
        for g in &groups {
            for i in 0..4 {
                let Some(b) = g.blocks[i] else { continue };
                let fits = sent.iter().any(|s| s[i] == b || (i == 2 && g.c_prime && b == s[0]));
                if !fits {
                    wrong += 1;
                    assert!(!g.corrected[i], "{cnr} dB: a correction made a wrong block: {g:?}");
                }
            }
        }
        wrong_total += wrong;
        println!(
            "{cnr:>4} {:7.1}  {:11}  {:9}  {:5.3}  {:>8}  {wrong}",
            s.rds_snr_db,
            c.ok + c.corrected,
            c.corrected,
            bler,
            ps_at.map(|t| format!("{t:.1} s")).unwrap_or("-".into())
        );
        if cnr >= 15.0 {
            assert!(bler < 0.03, "{cnr}: {bler}");
        } else if cnr >= 12.0 {
            assert!(bler < 0.1, "{cnr}: {bler}");
        }
        if cnr >= 9.0 {
            assert!(ps_at.is_some(), "{cnr}");
        }
        last_bler = bler;
    }
    assert!(wrong_total <= 3, "{wrong_total}");
    assert!(last_bler > 0.5, "the sweep should reach where RDS fails");
}

#[test]
fn a_retune_clears_rds_and_the_new_station_starts_clean() {
    let mut t = Tx::new(RATE);
    t.rds = Some(station());
    let x = iq(&t, 4.0, &Channel { cnr_db: Some(30.0), seed: 1, ..Default::default() });
    let (_, mut rx, _) = receive(&x, Config::default());
    assert_eq!(rx.rds().unwrap().ps.as_deref(), Some("TEST FM "));
    let (rev, _) = rx.rds_rev();
    rx.reset();
    assert_eq!(rx.rds().unwrap(), &Fields::default());
    assert!(!rx.status().stereo);
    assert!(rx.rds_rev().0 > rev);
    // The revision moves only on a change: a steady station leaves it still.
    let mut steady = WfmReceiver::new(RATE, Config::default()).unwrap();
    let mut out = Vec::new();
    for c in x.chunks(4800) {
        steady.process_iq(c, &mut out);
    }
    let before = steady.rds_rev().0;
    for c in iq(&t, 1.0, &Channel { cnr_db: Some(30.0), seed: 5, ..Default::default() }).chunks(4800) {
        steady.process_iq(c, &mut out);
    }
    assert!(steady.rds_rev().0 - before <= 2, "{} {}", before, steady.rds_rev().0);
    let mut other = Station::new(0xC555, "OTHER   ", 1);
    other.rt = b"Another station".to_vec();
    let mut t2 = Tx::new(RATE);
    t2.rds = Some(other.clone());
    let mut out = Vec::new();
    let mut seen = Vec::new();
    for c in iq(&t2, 4.0, &Channel { cnr_db: Some(30.0), seed: 2, ..Default::default() }).chunks(4800) {
        rx.process_iq(c, &mut out);
        seen.push(rx.rds().unwrap().clone());
    }
    check_fields(&seen, &other);
    assert_eq!(rx.rds().unwrap().ps.as_deref(), Some("OTHER   "));
}

#[test]
fn a_new_pi_on_the_same_frequency_forgets_the_old_station_without_a_reset() {
    // An app that does not call reset (a recording that cuts between
    // stations): the old station's PS must not stand beside the new PI.
    let mut a = Tx::new(RATE);
    a.rds = Some(station());
    let mut b = a.clone();
    let mut other = Station::new(0xC555, "OTHER   ", 1);
    other.rt = b"Another station".to_vec();
    b.rds = Some(other.clone());
    let mut m = tx::mpx(&a, 4.0);
    m.extend(tx::mpx(&b, 4.0));
    let x = tx::channel(&tx::fm(&m, RATE), RATE, &Channel { cnr_db: Some(30.0), seed: 3, ..Default::default() });
    let (_, rx, seen) = receive(&x, Config::default());
    for f in &seen {
        if f.pi == Some(0xC555) {
            assert!(f.ps.is_none() || f.ps.as_deref() == Some("OTHER   "), "{f:?}");
            assert!(f.rt.is_none() || f.rt.as_deref() == Some("Another station"), "{f:?}");
            assert!(f.ct.is_none() && f.af.is_empty(), "{f:?}");
        }
    }
    assert_eq!(rx.rds().unwrap().ps.as_deref(), Some("OTHER   "));
}

#[test]
fn deemphasis_75_us_and_the_lower_output_rates() {
    let mut t = Tx::new(RATE);
    t.preemph_us = 75.0;
    t.left = Audio::Tone { hz: 5_000.0, amp: 0.1 };
    t.right = t.left.clone();
    let x = iq(&t, 1.5, &Channel::default());
    let (out, _, _) = receive(&x, Config { deemph_us: 75.0, ..Config::default() });
    let got = tone(&chan(&out, 0, 0.5), 5_000.0);
    assert!(db(got / (0.5 * 0.87 * 0.1)).abs() < 0.3, "{got}");
    // 50 µs on a 75 µs station: the top is too bright, by the curves' ratio.
    let (out, _, _) = receive(&x, Config::default());
    let bright = db(tone(&chan(&out, 0, 0.5), 5_000.0) / got);
    let w = 2.0 * std::f64::consts::PI * 5_000.0;
    let want = 10.0 * ((1.0 + (w * 75e-6f64).powi(2)) / (1.0 + (w * 50e-6f64).powi(2))).log10();
    assert!((bright - want).abs() < 0.3, "{bright} {want}");
    // 12 kHz mono, Radio's audio today: a 1 kHz tone at its level, 5 kHz
    // still flat, and nothing above 6 kHz folded in.
    let mut t = Tx::new(RATE);
    t.left = Audio::Tone { hz: 1_000.0, amp: 0.5 };
    let (out, _, _) = receive(&iq(&t, 1.5, &Channel::default()), Config { audio_rate: 12_000, channels: 1, ..Config::default() });
    assert_eq!(out.len(), 18_000);
    let y: Vec<f64> = out[6_000..].iter().map(|v| *v as f64).collect();
    let (mut c, mut s) = (0.0, 0.0);
    for (i, v) in y.iter().enumerate() {
        let a = 2.0 * std::f64::consts::PI * 1_000.0 * i as f64 / 12_000.0;
        c += v * a.cos();
        s += v * a.sin();
    }
    let amp = 2.0 * (c * c + s * s).sqrt() / y.len() as f64;
    assert!((amp - 0.5 * 0.87 * 0.25).abs() < 0.002, "{amp}");
}

#[test]
fn the_wfm_host_session_plays_stereo_into_its_ring_and_reports_rds() {
    use shack_sdr::decoder::{InputKind, Output, Params, Value};
    use shack_sdr::host::{Body, Host, HostConfig};
    let h = Host::new(HostConfig::default());
    let sid = h.start("wfm", Params::new(), InputKind::Iq, RATE, 0.0, false).unwrap();
    let mut t = Tx::new(RATE);
    t.left = Audio::Tone { hz: 1_000.0, amp: 0.5 };
    t.rds = Some(station());
    let x = iq(&t, 4.0, &Channel { cnr_db: Some(30.0), seed: 6, ..Default::default() });
    let t0 = 1_792_663_200.0;
    let mut items = Vec::new();
    let mut pcm: Vec<f32> = Vec::new();
    for (k, c) in x.chunks(24_000).enumerate() {
        h.feed_iq(&sid, c, t0 + k as f64 * 0.1, (k * 24_000) as u64).unwrap();
        // Pull as an app would, at its own pace: each 0.1 s block makes
        // 4800 frames. (Fed faster than the 2 s input ring, it would drop.)
        let end = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while pcm.len() / 2 < (k + 1) * 4_800 && std::time::Instant::now() < end {
            items.extend(h.poll(500));
            for a in h.audio(&sid, 1.0, None, None).unwrap() {
                assert_eq!((a.rate, a.channels), (48_000, 2));
                pcm.extend(a.pcm);
            }
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
    }
    items.extend(h.poll(500));
    assert_eq!(pcm.len(), 2 * 48_000 * 4);
    let (l, r) = (tone(&chan(&pcm, 0, 2.0), 1_000.0), tone(&chan(&pcm, 1, 2.0), 1_000.0));
    assert!(db(l / r) > 40.0, "{l} {r}");
    // Never audio among the results: it has its ring.
    assert!(!items.iter().any(|i| matches!(&i.body, Body::Output(Output::Audio { .. }))));
    let metas: Vec<_> = items
        .iter()
        .filter_map(|i| match &i.body {
            Body::Output(Output::Meta { fields }) => Some((i.t, fields.clone())),
            _ => None,
        })
        .collect();
    let (tm, last) = metas.last().expect("meta");
    let get = |k: &str| last.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone()).unwrap();
    assert_eq!(get("stereo"), Value::Bool(true));
    assert_eq!(get("pi"), Value::Text("C201".into()));
    assert_eq!(get("ps"), Value::Text("TEST FM ".into()));
    assert_eq!(get("pty"), Value::Num(10.0));
    assert_eq!(get("ct"), Value::Text("2026-10-23T15:05:00+01:00".into()));
    assert_eq!(get("af"), Value::List(vec![Value::Num(89.1e6), Value::Num(91.3e6), Value::Num(93.5e6)]));
    // Times from the samples: within the 4 s fed, after t0.
    assert!(*tm > t0 && *tm <= t0 + 4.0, "{tm}");
    // Live: mono, and the next blocks say so.
    let p = Params::new().with("stereo", Value::Bool(false));
    h.configure(&sid, p).unwrap();
    assert!(h.configure(&sid, Params::new().with("deemphasis", Value::Num(60.0))).is_err());
    h.close();
}

#[test]
fn cpu_per_stream_on_this_host() {
    // One station's processing, timed as the thread's own CPU (not wall
    // time, which a loaded host inflates); printed with --nocapture and
    // held loosely, since hosts differ.
    let mut t = Tx::new(RATE);
    t.left = Audio::Noise { amp: 0.25, seed: 1 };
    t.right = Audio::Noise { amp: 0.25, seed: 2 };
    t.rds = Some(station());
    let secs = 10.0;
    let x = iq(&t, secs, &Channel { cnr_db: Some(25.0), seed: 4, ..Default::default() });
    for (name, cfg) in [
        ("stereo + RDS, 48 kHz", Config::default()),
        ("stereo, no RDS", Config { rds: false, ..Config::default() }),
        ("12 kHz mono + RDS", Config { audio_rate: 12_000, channels: 1, ..Config::default() }),
        ("RDS and pilot only", Config { audio: false, ..Config::default() }),
    ] {
        let mut rx = WfmReceiver::new(RATE, cfg).unwrap();
        let mut out = Vec::new();
        let c0 = shack_sdr::cpu::thread_time();
        for c in x.chunks(4800) {
            out.clear();
            rx.process_iq(c, &mut out);
        }
        let cpu = (shack_sdr::cpu::thread_time() - c0).as_secs_f64();
        eprintln!("{name:22}: {:.2} % of one core at 240 kS/s", 100.0 * cpu / secs);
        assert!(cpu / secs < 0.15, "{name}: {cpu}");
    }
}
