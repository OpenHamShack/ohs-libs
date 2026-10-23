//! The band scanner's many-carrier RDS (src/wfm/scan.rs) on a synthetic
//! capture: stations at different levels, stereo and mono, on the 100 kHz
//! grid, in noise; each found at its frequency with its PI, PS and PTY,
//! and nothing reported where there is no station. Also the CPU it takes
//! per MHz of capture on this host, printed with --nocapture.

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
    // A budget too small for the work: it stops at a round's end, with
    // what each carrier had.
    let stations = vec![st(0xC201, "RADIO 1 ", 10, 0.0, -20.0, true), st(0xC202, "RADIO 2 ", 13, 1_000_000.0, -30.0, true)];
    let iq = tx::band(rate, 2.0, &stations, -60.0, 5);
    let report = scan(&iq, rate, 98_000_000.0, &ScanOptions { budget_s: 0.05, threads: 1, ..ScanOptions::default() });
    assert_eq!(report.stations.len(), 2);
    assert!(report.timed_out && report.used_s < 1.0, "{report:?}");
    assert!(report.wall_s < 0.5, "{}", report.wall_s);
}
