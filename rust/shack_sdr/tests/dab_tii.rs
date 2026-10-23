//! TII through the `dab` decoder, as the host drives it: the transmitter
//! model's single-frequency network (dab::tx::sfn_iq) with known
//! identifiers, levels and delays, plus noise, and what the feed's `tii`
//! list says about it. Run with --nocapture to see each case's list.

use rustfft::num_complex::Complex32;
use shack_sdr::dab::tx::{dab_iq, sfn_iq, Channel, Site, TxEnsemble};
use shack_sdr::dab::Dab;
use shack_sdr::decoder::{Decoder, Input, Output, Sink, Value};

/// One transmitter as the feed lists it.
#[derive(Debug, Clone, PartialEq)]
struct Seen {
    main: u8,
    sub: u8,
    level_db: f64,
    confidence: f64,
}

fn num(v: &Value) -> f64 {
    match v {
        Value::Num(x) => *x,
        _ => panic!("not a number: {v:?}"),
    }
}

fn get<'a>(m: &'a [(String, Value)], k: &str) -> &'a Value {
    &m.iter().find(|(n, _)| n == k).unwrap_or_else(|| panic!("no field {k} in {m:?}")).1
}

fn tii_of(meta: &[(String, Value)]) -> Vec<Seen> {
    let Value::List(items) = get(meta, "tii") else { panic!("tii is not a list") };
    items
        .iter()
        .map(|v| {
            let Value::Map(m) = v else { panic!("not a map: {v:?}") };
            Seen { main: num(get(m, "main")) as u8, sub: num(get(m, "sub")) as u8, level_db: num(get(m, "levelDb")), confidence: num(get(m, "confidence")) }
        })
        .collect()
}

/// Every meta the decoder gave for `iq`, fed in 100 ms blocks.
fn metas(iq: &[Complex32], rate: f64) -> (Dab, Vec<Vec<(String, Value)>>) {
    let mut d = Dab::new();
    let mut out = Vec::new();
    let block = (rate / 10.0) as usize;
    for (i, chunk) in iq.chunks(block).enumerate() {
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: chunk, rate, t0: 0.0, index: (i * block) as u64 }, &mut sink).unwrap();
        for (_, o) in sink.take() {
            if let Output::Meta { fields } = o {
                out.push(fields);
            }
        }
    }
    (d, out)
}

fn last_tii(iq: &[Complex32], rate: f64) -> Vec<Seen> {
    let (_, m) = metas(iq, rate);
    tii_of(m.last().expect("a meta"))
}

fn site(tii: (u8, u8), delay: usize, gain_db: f64) -> Site {
    Site { tii: Some(tii), delay, gain: 10f64.powf(gain_db / 20.0) }
}

#[test]
fn a_lone_transmitter_is_named_at_its_full_level() {
    // Ofcom writes these in hex: main 3E, sub 07 (62, 7).
    let iq = sfn_iq(&TxEnsemble::standard(), &[site((62, 7), 0, 0.0)], 50, &Channel { snr_db: Some(12.0), ..Default::default() });
    let seen = last_tii(&iq, 2_048_000.0);
    eprintln!("lone: {seen:?}");
    assert_eq!(seen.len(), 1, "{seen:?}");
    assert_eq!((seen[0].main, seen[0].sub), (62, 7));
    assert!(seen[0].level_db.abs() < 1.5, "{seen:?}");
    assert!(seen[0].confidence >= 0.8, "{seen:?}");
}

#[test]
fn two_transmitters_of_a_single_frequency_network_are_both_named_strongest_first() {
    // The second 6 dB down and 146 us later, through a Pluto's 2.4 MS/s
    // with a frequency offset and a clock error.
    let sites = [site((5, 12), 300, -6.0), site((62, 7), 0, 0.0)];
    let ch = Channel { snr_db: Some(15.0), offset_hz: 2_345.6, clock_ppm: 4.0, rate: 2_400_000.0, ..Default::default() };
    let seen = last_tii(&sfn_iq(&TxEnsemble::standard(), &sites, 60, &ch), 2_400_000.0);
    eprintln!("two: {seen:?}");
    assert_eq!(seen.iter().map(|s| (s.main, s.sub)).collect::<Vec<_>>(), vec![(62, 7), (5, 12)], "{seen:?}");
    // Their shares of the power: 1/1.25 and 0.25/1.25.
    assert!((seen[0].level_db - -0.97).abs() < 1.5, "{seen:?}");
    assert!((seen[1].level_db - -6.99).abs() < 1.5, "{seen:?}");
    assert!(seen.iter().all(|s| s.confidence >= 0.5), "{seen:?}");
}

#[test]
fn a_distant_transmitter_far_under_the_strongest_is_still_found() {
    // 20 dB under, 300 us later (past the guard: it is still TII, though
    // its data is interference), within ten seconds.
    let sites = [site((24, 18), 0, 0.0), site((24, 3), 614, -20.0)];
    let seen = last_tii(&sfn_iq(&TxEnsemble::standard(), &sites, 100, &Channel { snr_db: Some(20.0), ..Default::default() }), 2_048_000.0);
    eprintln!("distant: {seen:?}");
    assert_eq!(seen.iter().map(|s| (s.main, s.sub)).collect::<Vec<_>>(), vec![(24, 18), (24, 3)], "{seen:?}");
    assert!((seen[1].level_db - -20.0).abs() < 3.0, "{seen:?}");
}

#[test]
fn two_transmitters_sharing_a_comb_are_told_apart() {
    // One sub-identifier, two main ones whose patterns share three of
    // their groups (1: groups 3, 5, 6, 7; 40: 0, 3, 5, 7), 6 dB apart.
    let sites = [site((1, 5), 0, 0.0), site((40, 5), 200, -6.0)];
    let seen = last_tii(&sfn_iq(&TxEnsemble::standard(), &sites, 64, &Channel { snr_db: Some(15.0), ..Default::default() }), 2_048_000.0);
    eprintln!("one comb: {seen:?}");
    assert_eq!(seen.iter().map(|s| (s.main, s.sub)).collect::<Vec<_>>(), vec![(1, 5), (40, 5)], "{seen:?}");
    assert!((seen[0].level_db - -0.97).abs() < 2.0 && (seen[1].level_db - -6.99).abs() < 2.0, "{seen:?}");
}

#[test]
fn a_strong_transmitter_casts_no_ghost_into_its_neighbours() {
    // Very strong, through a Pluto with an offset and a clock error: the
    // carriers beside its pairs (the next combs) must not read as a
    // transmitter of their own.
    let ch = Channel { snr_db: Some(40.0), offset_hz: -12_345.6, clock_ppm: -7.0, rate: 2_400_000.0, ..Default::default() };
    let seen = last_tii(&sfn_iq(&TxEnsemble::standard(), &[site((33, 0), 0, 0.0)], 50, &ch), 2_400_000.0);
    eprintln!("strong: {seen:?}");
    assert_eq!(seen.iter().map(|s| (s.main, s.sub)).collect::<Vec<_>>(), vec![(33, 0)], "{seen:?}");
}

#[test]
fn no_transmitter_is_named_where_none_sends_tii() {
    // A DAB signal with silent nulls (weak and strong), and noise alone:
    // no meta along the way may name anyone.
    let mut cases: Vec<(String, Vec<Complex32>)> = (1..=5)
        .map(|seed| (format!("weak, no TII, seed {seed}"), dab_iq(&TxEnsemble::standard(), 70, &Channel { snr_db: Some(5.0), seed, ..Default::default() })))
        .collect();
    cases.extend([
        ("strong, no TII".to_string(), dab_iq(&TxEnsemble::standard(), 70, &Channel { snr_db: Some(40.0), seed: 8, ..Default::default() })),
        ("noise".to_string(), {
            let mut rng = shack_sdr::testsig::Rng::new(9);
            (0..70 * 196_608).map(|_| Complex32::new(rng.gauss() as f32, rng.gauss() as f32)).collect()
        }),
    ]);
    for (name, iq) in cases {
        let (_, m) = metas(&iq, 2_048_000.0);
        for meta in &m {
            let seen = tii_of(meta);
            assert!(seen.is_empty(), "{name}: {seen:?}");
        }
    }
}

#[test]
fn weak_signal_tii_names_only_what_is_sent() {
    // Many seeds of a weak two-site network: what is listed is always
    // among what was sent, never another identifier.
    let sites = [site((10, 2), 0, 0.0), site((47, 20), 120, -3.0)];
    let mut found = 0;
    for seed in 1..=6 {
        let iq = sfn_iq(&TxEnsemble::standard(), &sites, 40, &Channel { snr_db: Some(4.0), seed, ..Default::default() });
        let (_, m) = metas(&iq, 2_048_000.0);
        for meta in &m {
            for s in tii_of(meta) {
                assert!([(10, 2), (47, 20)].contains(&(s.main, s.sub)), "seed {seed}: {s:?}");
            }
        }
        found += tii_of(m.last().unwrap()).len();
    }
    eprintln!("weak: {found} of 12 found");
    assert!(found >= 8, "{found} of 12");
}

#[test]
fn the_list_changes_at_a_modest_rate() {
    // Ten seconds of a steady two-site network: the meta (which goes out
    // only when something in it changes) carries a new TII list at most
    // once every few seconds.
    let sites = [site((5, 12), 300, -6.0), site((62, 7), 0, 0.0)];
    let iq = sfn_iq(&TxEnsemble::standard(), &sites, 104, &Channel { snr_db: Some(15.0), ..Default::default() });
    let (_, m) = metas(&iq, 2_048_000.0);
    let mut lists: Vec<Vec<Seen>> = m.iter().map(|x| tii_of(x)).collect();
    lists.dedup();
    eprintln!("{} different lists in 10 s", lists.len());
    assert!(lists.len() <= 6, "{} lists: {lists:?}", lists.len());
}

#[test]
fn tii_in_the_null_does_not_pull_the_snr_reading_down() {
    // The SNR is read from the null's power against the symbols'; a null
    // carrying TII is not empty (its 32 carriers are -16.8 dB of the
    // signal), so read from TII nulls alone a 25 dB signal would read 17
    // every other frame.
    let ch = Channel { snr_db: Some(25.0), ..Default::default() };
    let readings = |iq: &[Complex32]| -> Vec<f32> {
        let mut d = Dab::new();
        let block = 204_800;
        let mut r = Vec::new();
        for (i, chunk) in iq.chunks(block).enumerate() {
            d.feed(Input::Iq { samples: chunk, rate: 2_048_000.0, t0: 0.0, index: (i * block) as u64 }, &mut Sink::new()).unwrap();
            r.extend(d.status().snr_db);
        }
        r
    };
    let plain = readings(&dab_iq(&TxEnsemble::standard(), 30, &ch));
    let tii = readings(&sfn_iq(&TxEnsemble::standard(), &[site((62, 7), 0, 0.0)], 30, &ch));
    let lo = |r: &[f32]| r[5..].iter().cloned().fold(f32::INFINITY, f32::min);
    eprintln!("lowest SNR reading: {:.1} dB without TII, {:.1} dB with", lo(&plain), lo(&tii));
    assert!(lo(&tii) > lo(&plain) - 1.5, "{} against {}", lo(&tii), lo(&plain));
}

#[test]
fn a_gap_keeps_the_list_and_a_retune_clears_it() {
    // A host that falls behind drops samples: the averages start again
    // (the frame parity may have changed), but the last list stands
    // until the next look replaces it. A retune is another block: nothing
    // heard before holds.
    let iq = sfn_iq(&TxEnsemble::standard(), &[site((62, 7), 0, 0.0)], 80, &Channel { snr_db: Some(15.0), ..Default::default() });
    let mut d = Dab::new();
    let block = 204_800;
    let mut lists = Vec::new();
    for (i, chunk) in iq.chunks(block).enumerate() {
        if i == 50 {
            continue; // 100 ms lost
        }
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: chunk, rate: 2_048_000.0, t0: 0.0, index: (i * block) as u64 }, &mut sink).unwrap();
        for (_, o) in sink.take() {
            if let Output::Meta { fields } = o {
                lists.push((i, tii_of(&fields)));
            }
        }
    }
    let first = lists.iter().position(|(_, l)| !l.is_empty()).expect("a list");
    assert!(lists[first].0 < 50, "first list at block {}", lists[first].0);
    assert!(lists[first..].iter().all(|(_, l)| l.len() == 1), "{lists:?}");
    d.reset();
    let mut sink = Sink::new();
    d.feed(Input::Iq { samples: &iq[..block], rate: 2_048_000.0, t0: 0.0, index: 0 }, &mut sink).unwrap();
    let after: Vec<Vec<Seen>> = sink.take().into_iter().filter_map(|(_, o)| if let Output::Meta { fields } = o { Some(tii_of(&fields)) } else { None }).collect();
    assert_eq!(after.last().map(|l| l.len()), Some(0), "{after:?}");
}

#[test]
fn the_first_list_comes_soon_after_the_lock_and_survives_a_looped_signal() {
    // About 1.5 s after the lock (eight frames of each kind), not the 3 s
    // of a full average. And a signal looped every 24 frames, as Radio's
    // simulator replays the model (its counters restart at the seam, and
    // the decoder may resync there), still gets its list.
    let iq = sfn_iq(&TxEnsemble::standard(), &[site((11, 1), 0, 0.0)], 40, &Channel { snr_db: Some(15.0), ..Default::default() });
    let (_, m) = metas(&iq, 2_048_000.0);
    let first = m.iter().position(|x| !tii_of(x).is_empty()).expect("a list");
    let mut d = Dab::new();
    let block = 204_800;
    let mut at = None;
    for (i, chunk) in iq.chunks(block).enumerate() {
        let mut sink = Sink::new();
        d.feed(Input::Iq { samples: chunk, rate: 2_048_000.0, t0: 0.0, index: (i * block) as u64 }, &mut sink).unwrap();
        if at.is_none() && sink.take().iter().any(|(_, o)| matches!(o, Output::Meta { fields } if !tii_of(fields).is_empty())) {
            at = Some(i);
        }
    }
    eprintln!("first list in meta {first}, after {:?} s", at.map(|i| (i + 1) as f64 * 0.1));
    assert!(at.unwrap() < 22, "first list after block {at:?}");

    let one = sfn_iq(&TxEnsemble::standard(), &[site((11, 1), 0, 0.0)], 24, &Channel { snr_db: None, ..Default::default() });
    let looped: Vec<Complex32> = one[one.len() - 24 * 196_608..].iter().cycle().take(5 * 24 * 196_608).copied().collect();
    let seen = last_tii(&looped, 2_048_000.0);
    assert_eq!(seen.iter().map(|s| (s.main, s.sub)).collect::<Vec<_>>(), vec![(11, 1)], "{seen:?}");
}
