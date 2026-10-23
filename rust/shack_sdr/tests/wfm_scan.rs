//! The band scanner's many-carrier RDS (src/wfm/scan.rs) on a synthetic
//! capture: stations at different levels, stereo and mono, on the 100 kHz
//! grid, in noise; each found at its frequency with its PI, PS and PTY,
//! and nothing reported where there is no station; twelve stations in 8
//! and 10 MHz. Also the CPU it takes per MHz of capture on this host,
//! printed with --nocapture.

use shack_sdr::wfm::rds::Station;
use shack_sdr::wfm::tx::{self, Audio, BandStation, Tx};
use shack_sdr::wfm::{scan, ScanOptions};

fn st(pi: u16, ps: &str, pty: u8, offset_hz: f64, level_db: f64, stereo: bool) -> BandStation {
    let mut t = Tx::new(250_000.0);
    t.left = Audio::Noise { amp: 0.25, seed: pi as u64 };
    t.right = Audio::Noise { amp: 0.25, seed: pi as u64 + 1 };
    if !stereo {
        t.pilot = 0.0;
    }
    let mut s = Station::new(pi, ps, pty);
    s.rt = format!("{ps} radiotext").into_bytes();
    t.rds = Some(s);
    BandStation { offset_hz, level_db, tx: t }
}

#[test]
fn a_synthetic_band_is_found_and_named() {
    let rate = 8_000_000.0;
    let centre = 97_000_000.0;
    // Levels from strong to weak, in -60 dBFS of noise over 8 MHz (about
    // -77 dBFS in a station's 150 kHz): the weakest is 17 dB over it.
    let stations = vec![
        st(0xC201, "RADIO 1 ", 10, -3_400_000.0, -20.0, true),
        st(0xC202, "RADIO 2 ", 13, -2_100_000.0, -35.0, true),
        st(0xC203, "RADIO 3 ", 14, -500_000.0, -45.0, true),
        st(0xC204, "RADIO 4 ", 1, 1_200_000.0, -50.0, false),
        st(0xC2D5, "LOCAL   ", 3, 2_300_000.0, -40.0, true),
        st(0xC2E6, "WEAK FM ", 11, 3_300_000.0, -60.0, true),
    ];
    let iq = tx::band(rate, 2.0, &stations, -60.0, 17);
    let report = scan(&iq, rate, centre, &ScanOptions { budget_s: 30.0, ..ScanOptions::default() });
    for f in &report.stations {
        println!(
            "{:.1} MHz {:6.1} dB stereo {} pi {:?} ps {:?} pty {:?} sync {} complete {}",
            f.hz / 1e6,
            f.level_db,
            f.stereo,
            f.fields.pi.map(|p| format!("{p:04X}")),
            f.fields.ps,
            f.fields.pty,
            f.rds_sync,
            f.complete
        );
    }
    println!(
        "noise {:.1} dBFS/150 kHz, used {:.2} s of 2, cpu {:.2} s, wall {:.2} s, timed out {}",
        report.noise_db, report.used_s, report.cpu_s, report.wall_s, report.timed_out
    );
    assert_eq!(report.stations.len(), stations.len(), "{:?}", report.stations.iter().map(|f| f.hz).collect::<Vec<_>>());
    for s in &stations {
        let hz = centre + s.offset_hz;
        let f = report.stations.iter().find(|f| (f.hz - hz).abs() < 1.0).expect("found");
        let want = s.tx.rds.as_ref().unwrap();
        assert!((f.level_db as f64 - s.level_db).abs() < 1.5, "{hz}: {} {}", f.level_db, s.level_db);
        assert_eq!(f.stereo, s.tx.pilot > 0.0, "{hz}");
        assert_eq!(f.fields.pi, Some(want.pi), "{hz}");
        assert_eq!(f.fields.ps.as_deref().map(str::as_bytes), Some(&want.ps[..]), "{hz}");
        assert_eq!(f.fields.pty, Some(want.pty), "{hz}");
        assert!(f.complete);
    }
    assert!(!report.timed_out);
}

#[test]
fn the_budget_is_kept_and_noise_alone_finds_nothing() {
    let rate = 4_000_000.0;
    let iq = tx::band(rate, 1.0, &[], -50.0, 3);
    let report = scan(&iq, rate, 98_000_000.0, &ScanOptions::default());
    assert!(report.stations.is_empty(), "{:?}", report.stations);
    // The floor in noise alone: -50 dBFS over 4 MHz is -64.3 in 150 kHz.
    let want = -50.0 - 10.0 * (4_000_000.0f64 / 150_000.0).log10();
    assert!((report.noise_db as f64 - want).abs() < 1.0, "{} {want}", report.noise_db);
    // A budget too small for the work: it stops at a round's end, with
    // what each carrier had.
    let stations = vec![st(0xC201, "RADIO 1 ", 10, 0.0, -20.0, true), st(0xC202, "RADIO 2 ", 13, 1_000_000.0, -30.0, true)];
    let iq = tx::band(rate, 2.0, &stations, -60.0, 5);
    let report = scan(&iq, rate, 98_000_000.0, &ScanOptions { budget_s: 0.05, threads: 1, ..ScanOptions::default() });
    assert_eq!(report.stations.len(), 2);
    assert!(report.timed_out && report.used_s < 1.0, "{report:?}");
    assert!(report.wall_s < 0.5, "{}", report.wall_s);
}

#[test]
fn cpu_per_mhz_of_capture_on_this_host() {
    // A full 8 and 10 MHz capture with twelve stations, scanned to the end
    // of the capture (RadioText asked for, which none sends, so no carrier
    // stops early): the worst case, per MHz of capture and per second of it.
    for rate in [8_000_000.0, 10_000_000.0] {
        let n = 12;
        let stations: Vec<BandStation> = (0..n)
            .map(|k| {
                let off = -0.42 * rate + (k as f64 + 0.5) * 0.84 * rate / n as f64;
                let off = (off / 100_000.0).round() * 100_000.0;
                st(0xC300 + k as u16, &format!("STN {k:02} "), (k % 31) as u8 + 1, off, -25.0 - 3.0 * k as f64, k % 3 != 0)
            })
            .map(|mut s| {
                s.tx.rds.as_mut().unwrap().rt.clear();
                s
            })
            .collect();
        let secs = 1.5;
        let iq = tx::band(rate, secs, &stations, -60.0, 9);
        for threads in [1, 4] {
            let r = scan(&iq, rate, 98_000_000.0, &ScanOptions { budget_s: 60.0, threads, rt: true, ..ScanOptions::default() });
            let mhz = rate / 1e6;
            eprintln!(
                "{mhz:4.0} MHz, {threads} thread(s), {} stations: {:.1} ms CPU per MHz per second of capture ({:.2} s CPU, {:.2} s wall for {:.1} s); all named: {}",
                r.stations.len(),
                1000.0 * r.cpu_s / mhz / r.used_s,
                r.cpu_s,
                r.wall_s,
                r.used_s,
                r.stations.iter().all(|f| f.fields.ps.is_some())
            );
            eprintln!("noise floor {:.1} dBFS in 150 kHz", r.noise_db);
            let found: Vec<f64> = r.stations.iter().map(|f| f.hz).collect();
            let want: Vec<f64> = stations.iter().map(|s| 98_000_000.0 + s.offset_hz).collect();
            assert_eq!(found, want, "{:?}", r.stations.iter().map(|f| (f.hz, f.level_db)).collect::<Vec<_>>());
            assert!(r.cpu_s / mhz / r.used_s < 0.05, "{}", r.cpu_s);
        }
    }
}
