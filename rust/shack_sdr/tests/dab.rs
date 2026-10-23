//! The `dab` decoder on the transmitter model's IQ (dab::tx), through the
//! Decoder interface as the host drives it: the feed's shape, old DAB's
//! sound, DAB+ AUs through a stand-in AAC decoder (we never encode or
//! decode AAC ourselves), faad2's absence, and decoding against noise,
//! frequency offset, multipath, fading, clock error and 2.4 MS/s input.
//! The sweep prints a table (run with --nocapture to see it).

use shack_sdr::dab::audio::{Aac, Pcm};
use shack_sdr::dab::tx::{dab_iq, dabplus_au, Channel, Path, TxEnsemble};
use shack_sdr::dab::Dab;
use shack_sdr::decoder::{CodecId, DecodeError, Decoder, Input, Output, Params, Sink, Value};
use rustfft::num_complex::Complex32;
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// A stand-in AAC decoder: keeps every AU it is given, and the
/// AudioSpecificConfig it was opened with; gives 960 x 2 frames of silence
/// per AU (HE-AAC's output at 48 kHz) so the sound path is exercised.
#[derive(Default)]
struct Seen {
    ascs: Vec<Vec<u8>>,
    aus: Vec<Vec<u8>>,
}

struct FakeAac(Arc<Mutex<Seen>>);

impl Aac for FakeAac {
    fn decode(&mut self, au: &[u8]) -> Result<Pcm, &'static str> {
        self.0.lock().unwrap().aus.push(au.to_vec());
        Ok((vec![0.0; 1920 * 2], 48_000, 2))
    }
}

fn with_fake() -> (Dab, Arc<Mutex<Seen>>) {
    let seen = Arc::new(Mutex::new(Seen::default()));
    let s2 = seen.clone();
    let d = Dab::with_aac(Box::new(move |asc: &[u8]| -> Result<Box<dyn Aac>, &'static str> {
        s2.lock().unwrap().ascs.push(asc.to_vec());
        Ok(Box::new(FakeAac(s2.clone())))
    }));
    (d, seen)
}

fn field<'a>(f: &'a [(String, Value)], k: &str) -> &'a Value {
    &f.iter().find(|(n, _)| n == k).unwrap_or_else(|| panic!("no field {k}")).1
}

fn map_field<'a>(v: &'a Value, k: &str) -> &'a Value {
    match v {
        Value::Map(m) => field(m, k),
        _ => panic!("not a map: {v:?}"),
    }
}

/// What a run of the decoder gave.
#[derive(Default)]
struct Run {
    metas: Vec<Vec<(String, Value)>>,
    audio: Vec<(Vec<f32>, u32, u8)>,
    needs_codec: bool,
    secs: f64,
    cpu_s: f64,
}

impl Run {
    fn last(&self) -> &[(String, Value)] {
        self.metas.last().expect("a meta result")
    }
    fn counter(d: &Dab, k: &str) -> u64 {
        d.status().counters.iter().find(|(n, _)| *n == k).map(|c| c.1).unwrap()
    }
}

/// Feed `iq` (at `rate`) in 100 ms blocks, as the host would.
fn run(d: &mut Dab, iq: &[Complex32], rate: f64) -> Run {
    let mut r = Run::default();
    let block = (rate / 10.0) as usize;
    let t = Instant::now();
    let c0 = shack_sdr::cpu::thread_time();
    for (i, chunk) in iq.chunks(block).enumerate() {
        let mut sink = Sink::new();
        let res = d.feed(Input::Iq { samples: chunk, rate, t0: 1000.0 + (i * block) as f64 / rate, index: (i * block) as u64 }, &mut sink);
        match res {
            Ok(()) => {}
            Err(DecodeError::NeedsCodec(CodecId::Faad2)) => r.needs_codec = true,
            Err(e) => panic!("{e:?}"),
        }
        for (_, o) in sink.take() {
            match o {
                Output::Meta { fields } => r.metas.push(fields),
                Output::Audio { pcm, rate, channels } => r.audio.push((pcm, rate, channels)),
                _ => {}
            }
        }
    }
    r.secs = t.elapsed().as_secs_f64();
    r.cpu_s = (shack_sdr::cpu::thread_time() - c0).as_secs_f64();
    r
}

fn choose(d: &mut Dab, sid: &str) {
    d.configure(&Params::new().with("service", Value::Text(sid.into()))).unwrap();
}

fn tone_level(x: &[f32], f: f64, rate: f64) -> f64 {
    let (mut c, mut s) = (0.0, 0.0);
    for (i, v) in x.iter().enumerate() {
        let p = 2.0 * std::f64::consts::PI * f * i as f64 / rate;
        c += *v as f64 * p.cos();
        s += *v as f64 * p.sin();
    }
    2.0 * (c * c + s * s).sqrt() / x.len() as f64
}

#[test]
fn the_feed_lists_the_ensemble_and_its_services() {
    let iq = dab_iq(&TxEnsemble::standard(), 14, &Channel { snr_db: Some(20.0), ..Default::default() });
    let mut d = Dab::new();
    let r = run(&mut d, &iq, 2_048_000.0);
    let m = r.last();
    assert_eq!(field(m, "sync"), &Value::Bool(true));
    let ens = field(m, "ensemble");
    assert_eq!(map_field(ens, "id"), &Value::Text("C1CE".into()));
    assert_eq!(map_field(ens, "label"), &Value::Text("Shack Test Mux".into()));
    assert_eq!(map_field(ens, "shortLabel"), &Value::Text("ShackTes".into()));
    let Value::List(svcs) = field(m, "services") else { panic!() };
    assert_eq!(svcs.len(), 2);
    let want = [("C221", "Shack MP2 Tone", 128.0, "dab", "3"), ("C3A7", "Shack DAB+ Data", 64.0, "dab+", "3-A")];
    for (s, (sid, label, rate, kind, prot)) in svcs.iter().zip(want) {
        assert_eq!(map_field(s, "sid"), &Value::Text(sid.into()));
        assert_eq!(map_field(s, "label"), &Value::Text(label.into()));
        assert_eq!(map_field(s, "bitrate"), &Value::Num(rate));
        assert_eq!(map_field(s, "kind"), &Value::Text(kind.into()));
        assert_eq!(map_field(s, "protection"), &Value::Text(prot.into()));
    }
    assert_eq!(field(m, "playing"), &Value::Null);
    assert_eq!(field(m, "why"), &Value::Null);
    // Nothing chosen: no sound, and only the FIC's symbols were decoded.
    assert!(r.audio.is_empty());
    // The first meta, before anything was heard, said so.
    assert_eq!(field(&r.metas[0], "sync"), &Value::Bool(false));
    assert_eq!(field(&r.metas[0], "services"), &Value::List(vec![]));
}

#[test]
fn an_old_dab_service_plays_its_tones_through_our_mp2_decoder() {
    let iq = dab_iq(&TxEnsemble::standard(), 20, &Channel { snr_db: Some(15.0), offset_hz: 3_210.0, ..Default::default() });
    let mut d = Dab::new();
    choose(&mut d, "C221");
    let r = run(&mut d, &iq, 2_048_000.0);
    assert_eq!(field(r.last(), "playing"), &Value::Text("C221".into()));
    assert_eq!(map_field(field(r.last(), "audio"), "codec"), &Value::Text("mp2".into()));
    assert!(!r.needs_codec);
    // 48 kHz stereo, a frame a logical frame from 16 CIFs in.
    assert!(r.audio.len() > 50, "{}", r.audio.len());
    assert!(r.audio.iter().all(|a| a.1 == 48_000 && a.2 == 2 && a.0.len() == 2304));
    let pcm: Vec<f32> = r.audio.iter().skip(4).flat_map(|a| a.0.clone()).collect();
    let left: Vec<f32> = pcm.iter().step_by(2).copied().collect();
    let right: Vec<f32> = pcm.iter().skip(1).step_by(2).copied().collect();
    // The transmitter's 1 kHz left and 1.5 kHz right at 0.5.
    assert!((tone_level(&left, 1000.0, 48_000.0) - 0.5).abs() < 0.02);
    assert!((tone_level(&right, 1500.0, 48_000.0) - 0.5).abs() < 0.02);
    assert!(tone_level(&left, 1500.0, 48_000.0) < 0.01);
    assert_eq!(Run::counter(&d, "audioErrors"), 0);
}

#[test]
fn dab_plus_aus_reach_the_aac_decoder_whole_and_in_order() {
    let iq = dab_iq(&TxEnsemble::standard(), 20, &Channel { snr_db: Some(15.0), ..Default::default() });
    let (mut d, seen) = with_fake();
    choose(&mut d, "c3a7");
    let r = run(&mut d, &iq, 2_048_000.0);
    let seen = seen.lock().unwrap();
    // HE-AAC, 48 kHz out, stereo: AAC LC at 24 kHz with the 960 transform,
    // then SBR signalled explicitly (TS 102 563; dablin's layout).
    assert_eq!(seen.ascs, vec![vec![0x13, 0x14, 0x56, 0xe5, 0x98]]);
    // Three AUs per super frame, each the transmitter's, none missing:
    // after its PAD (a data_stream_element, which faad2 skips), the test
    // bytes saying which super frame and AU it is.
    assert!(seen.aus.len() >= 30, "{}", seen.aus.len());
    let body = |au: &[u8]| -> Vec<u8> { au[2 + au[1] as usize..].to_vec() };
    assert!(seen.aus.iter().all(|au| au[0] >> 5 == 4), "every AU starts with its PAD");
    let b0 = body(&seen.aus[0]);
    let first = b0[0] as u64 * 256 + b0[1] as u64;
    for (i, au) in seen.aus.iter().enumerate() {
        let m = first + (i / 3) as u64;
        let b = body(au);
        assert_eq!(b, dabplus_au(m, i % 3, b.len()), "AU {i}");
    }
    assert_eq!(Run::counter(&d, "auErrors"), 0);
    assert_eq!(map_field(field(r.last(), "audio"), "codec"), &Value::Text("he-aac".into()));
    assert_eq!(field(r.last(), "why"), &Value::Null);
    assert_eq!(r.audio.len(), seen.aus.len());
}

#[test]
fn without_faad2_dab_plus_says_why_and_everything_else_still_works() {
    // No codec published to this process: crate::codecs has no faad2.
    let iq = dab_iq(&TxEnsemble::standard(), 20, &Channel { snr_db: Some(15.0), ..Default::default() });
    let mut d = Dab::new();
    choose(&mut d, "C3A7");
    let r = run(&mut d, &iq, 2_048_000.0);
    let m = r.last();
    assert!(r.needs_codec, "feed says NeedsCodec(faad2), so the host shows needs_codec:faad2");
    assert_eq!(field(m, "why"), &Value::Text("needs_codec:faad2".into()));
    assert_eq!(field(m, "playing"), &Value::Text("C3A7".into()));
    assert!(r.audio.is_empty());
    // The list is whole, each DAB+ service saying what it needs.
    let Value::List(svcs) = field(m, "services") else { panic!() };
    assert_eq!(svcs.len(), 2);
    assert_eq!(map_field(&svcs[1], "why"), &Value::Text("needs_codec:faad2".into()));
    assert_eq!(map_field(&svcs[1], "needs"), &Value::List(vec![Value::Text("faad2".into())]));
    assert_eq!(map_field(&svcs[0], "why"), &Value::Null);
    // The super frames were still found: healthy, silent only for faad2.
    assert!(Run::counter(&d, "superframes") > 5);
    // And old DAB, chosen live, plays.
    choose(&mut d, "C221");
    let iq2 = dab_iq(&TxEnsemble::standard(), 12, &Channel { snr_db: Some(15.0), seed: 9, ..Default::default() });
    d.reset();
    let r2 = run(&mut d, &iq2, 2_048_000.0);
    assert!(!r2.audio.is_empty() && !r2.needs_codec);
    assert_eq!(field(r2.last(), "why"), &Value::Null);
}

#[test]
fn noise_alone_is_no_signal_and_a_missing_service_is_said() {
    let mut rng = shack_sdr::testsig::Rng::new(5);
    let noise: Vec<Complex32> = (0..2_048_000).map(|_| Complex32::new(rng.gauss() as f32, rng.gauss() as f32)).collect();
    let mut d = Dab::new();
    let r = run(&mut d, &noise, 2_048_000.0);
    assert_eq!(field(r.last(), "sync"), &Value::Bool(false));
    assert_eq!(field(r.last(), "why"), &Value::Text("no_signal".into()));
    assert_eq!(field(r.last(), "ensemble"), &Value::Null);
    let iq = dab_iq(&TxEnsemble::standard(), 24, &Channel { snr_db: Some(20.0), ..Default::default() });
    let mut d = Dab::new();
    choose(&mut d, "BEEF");
    let r = run(&mut d, &iq, 2_048_000.0);
    assert_eq!(field(r.last(), "why"), &Value::Text("service_not_found".into()));
    assert_eq!(field(r.last(), "playing"), &Value::Null);
}

#[test]
fn parameters_are_checked() {
    let mut d = Dab::new();
    assert!(d.configure(&Params::new().with("service", Value::Text("C22G".into()))).is_err());
    assert!(d.configure(&Params::new().with("block", Value::Text("14A".into()))).is_err());
    assert!(d.configure(&Params::new().with("block", Value::Text("11d".into()))).is_ok());
    assert!(d.configure(&Params::new().with("nope", Value::Num(1.0))).is_err());
    let mut sink = Sink::new();
    let x = vec![Complex32::new(0.0, 0.0); 1000];
    assert_eq!(d.feed(Input::Iq { samples: &x, rate: 1_536_000.0, t0: 0.0, index: 0 }, &mut sink), Err(DecodeError::BadInput("rate")));
    assert_eq!(d.feed(Input::Audio { samples: &[0.0; 10], rate: 2_048_000.0, t0: 0.0, index: 0 }, &mut sink), Err(DecodeError::BadInput("input_kind")));
}

#[test]
fn a_gap_in_the_samples_is_bridged_by_finding_the_frame_again() {
    let iq = dab_iq(&TxEnsemble::standard(), 36, &Channel { snr_db: Some(15.0), ..Default::default() });
    let (mut d, seen) = with_fake();
    choose(&mut d, "C3A7");
    let half = iq.len() / 3;
    let mut sink = Sink::new();
    for (k, c) in iq[..half].chunks(204_800).enumerate() {
        d.feed(Input::Iq { samples: c, rate: 2_048_000.0, t0: 0.0, index: (k * 204_800) as u64 }, &mut sink).unwrap();
    }
    let before = seen.lock().unwrap().aus.len();
    // 0.3 s lost, then on.
    let skip = 614_400;
    for (k, c) in iq[half + skip..].chunks(204_800).enumerate() {
        d.feed(Input::Iq { samples: c, rate: 2_048_000.0, t0: 0.0, index: (half + skip + k * 204_800) as u64 }, &mut sink).unwrap();
    }
    assert!(seen.lock().unwrap().aus.len() > before + 6);
    assert!(Run::counter(&d, "resyncs") >= 2);
}

/// Does a decoded stereo frame hold just the transmitter's 1 kHz left and
/// 1.5 kHz right tones (at 0.5), to within 1 % of their power?
fn tones_clean(pcm: &[f32]) -> bool {
    [(0usize, 1000.0f64), (1, 1500.0)].iter().all(|&(ch, f)| {
        let x: Vec<f64> = pcm.iter().skip(ch).step_by(2).map(|v| *v as f64).collect();
        let n = x.len() as f64;
        let (mut c, mut s) = (0.0, 0.0);
        for (i, v) in x.iter().enumerate() {
            let p = 2.0 * std::f64::consts::PI * f * i as f64 / 48_000.0;
            c += v * p.cos();
            s += v * p.sin();
        }
        let (a, b) = (2.0 * c / n, 2.0 * s / n);
        let resid: f64 = x
            .iter()
            .enumerate()
            .map(|(i, v)| {
                let p = 2.0 * std::f64::consts::PI * f * i as f64 / 48_000.0;
                (v - a * p.cos() - b * p.sin()).powi(2)
            })
            .sum::<f64>()
            / n;
        (a * a + b * b).sqrt() > 0.45 && resid < 0.01 * 0.125
    })
}

/// One case of the sweep: fraction of FIBs, MP2 frames and DAB+ AUs good.
struct Score {
    fic: f64,
    mp2: f64,
    aus: f64,
    snr: f32,
    cpu_per_frame_ms: f64,
}

fn score(ch: &Channel, frames: usize) -> Score {
    let ens = TxEnsemble::standard();
    let iq = dab_iq(&ens, frames, ch);
    let after_lock = |d: &Dab| (Run::counter(d, "fibsOk"), Run::counter(d, "fibsBad"));
    // Old DAB.
    let mut d = Dab::new();
    choose(&mut d, "C221");
    let r = run(&mut d, &iq, ch.rate);
    let (ok, bad) = after_lock(&d);
    let fic = ok as f64 / (ok + bad).max(1) as f64;
    // An MP2 frame is good when its sound is the transmitter's tones: the
    // header CRC covers only the header and allocation, so passing it says
    // little about the samples.
    let mp2_ok = r.audio.iter().filter(|a| tones_clean(&a.0)).count() as u64;
    // Logical frames the sub-channel could have given: 4 per frame, less
    // the 15 CIFs of interleaving delay and the frame that locks.
    let possible = (4 * Run::counter(&d, "frames")).saturating_sub(16).max(1);
    let mp2 = mp2_ok as f64 / possible as f64;
    let snr = d.status().snr_db.unwrap_or(f32::NAN);
    let cpu_per_frame_ms = 1000.0 * r.cpu_s / Run::counter(&d, "frames").max(1) as f64;
    // DAB+ through the stand-in.
    let (mut d, seen) = with_fake();
    choose(&mut d, "C3A7");
    run(&mut d, &iq, ch.rate);
    let good = seen.lock().unwrap().aus.len() as f64;
    // Three AUs per 5 logical frames; the first super frame waits for 5
    // more after the interleaver fills.
    let possible = (3 * ((4 * Run::counter(&d, "frames")).saturating_sub(16 + 5) / 5)).max(1) as f64;
    Score { fic, mp2: mp2.min(1.0), aus: (good / possible).min(1.0), snr, cpu_per_frame_ms }
}

#[test]
fn decoding_holds_against_noise_offset_multipath_fading_and_clock_error() {
    let two_ray = vec![Path { delay: 0, gain: 1.0, doppler_hz: 0.0 }, Path { delay: 123, gain: 0.7, doppler_hz: 0.0 }];
    let weak_first = vec![Path { delay: 0, gain: 0.5, doppler_hz: 0.0 }, Path { delay: 200, gain: 1.0, doppler_hz: 0.0 }];
    let fading = vec![
        Path { delay: 0, gain: 0.8, doppler_hz: 10.0 },
        Path { delay: 40, gain: 0.5, doppler_hz: 10.0 },
        Path { delay: 300, gain: 0.3, doppler_hz: 10.0 },
    ];
    let mobile = vec![Path { delay: 0, gain: 0.8, doppler_hz: 40.0 }, Path { delay: 100, gain: 0.6, doppler_hz: 40.0 }];
    // (name, channel, (min FIC, min MP2, min AUs)).
    let cases: Vec<(&str, Channel, (f64, f64, f64))> = vec![
        ("AWGN 20 dB", Channel { snr_db: Some(20.0), ..Default::default() }, (1.0, 1.0, 1.0)),
        ("AWGN 12 dB", Channel { snr_db: Some(12.0), ..Default::default() }, (1.0, 1.0, 1.0)),
        ("AWGN 9 dB", Channel { snr_db: Some(9.0), ..Default::default() }, (1.0, 1.0, 1.0)),
        ("AWGN 7 dB", Channel { snr_db: Some(7.0), ..Default::default() }, (1.0, 0.95, 0.95)),
        // MP2 has no outer code, so one bit error spoils a frame: it falls
        // off at 5 dB, where DAB+'s RS still delivers most AUs.
        ("AWGN 5 dB", Channel { snr_db: Some(5.0), ..Default::default() }, (0.98, 0.0, 0.8)),
        ("AWGN 4 dB", Channel { snr_db: Some(4.0), ..Default::default() }, (0.9, 0.0, 0.0)),
        ("AWGN 3 dB", Channel { snr_db: Some(3.0), ..Default::default() }, (0.0, 0.0, 0.0)),
        ("offset +1234.5 Hz", Channel { snr_db: Some(12.0), offset_hz: 1234.5, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("offset -17.321 kHz", Channel { snr_db: Some(12.0), offset_hz: -17_321.0, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("offset +35 kHz", Channel { snr_db: Some(12.0), offset_hz: 35_000.0, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("echo 60 us -3 dB", Channel { snr_db: Some(12.0), paths: two_ray, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("first path 6 dB weak", Channel { snr_db: Some(12.0), paths: weak_first, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("3 paths fading 10 Hz", Channel { snr_db: Some(15.0), paths: fading, ..Default::default() }, (0.95, 0.9, 0.9)),
        ("2 paths fading 40 Hz", Channel { snr_db: Some(18.0), paths: mobile, seed: 3, ..Default::default() }, (0.95, 0.9, 0.9)),
        ("clock +40 ppm, +9 kHz", Channel { snr_db: Some(12.0), clock_ppm: 40.0, offset_hz: 9_000.0, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("clock -60 ppm", Channel { snr_db: Some(12.0), clock_ppm: -60.0, ..Default::default() }, (1.0, 1.0, 1.0)),
        ("2.4 MS/s input", Channel { snr_db: Some(12.0), rate: 2_400_000.0, ..Default::default() }, (1.0, 1.0, 1.0)),
    ];
    let results: Vec<(String, Score, (f64, f64, f64))> = std::thread::scope(|sc| {
        let hs: Vec<_> = cases.into_iter().map(|(n, ch, min)| sc.spawn(move || (n.to_string(), score(&ch, 24), min))).collect();
        hs.into_iter().map(|h| h.join().unwrap()).collect()
    });
    eprintln!("{:24} {:>7} {:>7} {:>7} {:>7} {:>9}", "channel", "SNR", "FIC", "MP2", "DAB+", "ms/frame");
    let mut bad = Vec::new();
    for (n, s, min) in &results {
        eprintln!("{:24} {:>7.1} {:>6.1}% {:>6.1}% {:>6.1}% {:>9.2}", n, s.snr, 100.0 * s.fic, 100.0 * s.mp2, 100.0 * s.aus, s.cpu_per_frame_ms);
        if s.fic + 1e-9 < min.0 || s.mp2 + 1e-9 < min.1 || s.aus + 1e-9 < min.2 {
            bad.push(n.clone());
        }
    }
    assert!(bad.is_empty(), "below their floors: {bad:?}");
}

#[test]
fn cpu_per_frame_on_this_host() {
    // One 96 ms frame's decoding, with each kind of service playing; the
    // figure is printed (--nocapture) and held loosely, since hosts differ.
    let iq = dab_iq(&TxEnsemble::standard(), 40, &Channel { snr_db: Some(15.0), ..Default::default() });
    for sid in ["", "C221", "C3A7"] {
        let (mut d, _seen) = with_fake();
        choose(&mut d, sid);
        let r = run(&mut d, &iq, 2_048_000.0);
        let frames = Run::counter(&d, "frames") as f64;
        let ms = 1000.0 * r.cpu_s / frames;
        eprintln!("service {:5}: {:.2} ms CPU per 96 ms frame ({:.1} % of one core), {:.2} s wall for {} frames",
                  if sid.is_empty() { "none" } else { sid }, ms, ms / 96.0 * 100.0, r.secs, frames);
        assert!(ms < 48.0, "{ms} ms per frame");
    }
}

fn dls_of(m: &[(String, Value)]) -> Option<String> {
    match field(m, "dls") {
        Value::Text(t) => Some(t.clone()),
        _ => None,
    }
}

#[test]
fn the_playing_services_programme_text_comes_through_and_changes() {
    // Old DAB's DLS in short X-PAD; its second label from audio frame 100.
    let iq = dab_iq(&TxEnsemble::standard(), 40, &Channel { snr_db: Some(12.0), ..Default::default() });
    let mut d = Dab::new();
    choose(&mut d, "C221");
    let r = run(&mut d, &iq, 2_048_000.0);
    let labels: Vec<String> = r.metas.iter().filter_map(|m| dls_of(m)).collect::<Vec<_>>();
    let mut seen: Vec<String> = Vec::new();
    for l in labels {
        if seen.last() != Some(&l) {
            seen.push(l);
        }
    }
    assert_eq!(seen, vec!["Shack MP2 Tone - now: 1 kHz left, 1.5 kHz right".to_string(), "Second label for C221".to_string()]);
}

#[test]
fn dab_plus_programme_text_needs_no_faad2_and_clears_when_the_service_changes() {
    let iq = dab_iq(&TxEnsemble::standard(), 24, &Channel { snr_db: Some(12.0), ..Default::default() });
    let mut d = Dab::new(); // no faad2 in this process
    choose(&mut d, "C3A7");
    let r = run(&mut d, &iq, 2_048_000.0);
    assert!(r.needs_codec);
    assert_eq!(dls_of(r.last()).as_deref(), Some("DAB+ test AUs from the Shack transmitter model"));
    // Another service: the label goes at once, not left from the last one.
    choose(&mut d, "C221");
    let mut sink = Sink::new();
    let more = dab_iq(&TxEnsemble::standard(), 1, &Channel { seed: 4, ..Default::default() });
    d.feed(Input::Iq { samples: &more[..20_480], rate: 2_048_000.0, t0: 0.0, index: iq.len() as u64 }, &mut sink).unwrap();
    let metas: Vec<_> = sink.take().into_iter().filter_map(|(_, o)| if let Output::Meta { fields } = o { Some(fields) } else { None }).collect();
    let m = metas.last().expect("a new meta for the change");
    assert_eq!(field(m, "playing"), &Value::Text("C221".into()));
    assert_eq!(dls_of(m), None);
}

#[test]
fn a_survey_says_quickly_whether_a_block_holds_an_ensemble() {
    use shack_sdr::dab::survey;
    // Lead-ins put the first frame at different points of the buffer.
    let mut rows = Vec::new();
    for (snr, lead, seed) in [(Some(20.0), 0.0123, 1u64), (Some(12.0), 0.071, 2), (Some(8.0), 0.0444, 3), (Some(12.0), 0.09, 4)] {
        let iq = dab_iq(&TxEnsemble::standard(), 24, &Channel { snr_db: snr, lead_s: lead, seed, ..Default::default() });
        let s = survey(&iq, 2_048_000.0, 1.0).unwrap();
        assert_eq!(field(&s.meta, "complete"), &Value::Bool(true));
        let Value::List(svcs) = field(&s.meta, "services") else { panic!() };
        assert_eq!(svcs.len(), 2);
        rows.push((snr.unwrap(), s.sync_s.unwrap(), s.complete_s.unwrap()));
        // Labels come once a second, so a complete list takes at most a
        // second and a half of signal; sync, a frame or two.
        assert!(s.sync_s.unwrap() < 0.45 && s.complete_s.unwrap() < 1.6, "{:?} {:?}", s.sync_s, s.complete_s);
    }
    // Nothing there: the verdict after give_up_s.
    let mut rng = shack_sdr::testsig::Rng::new(5);
    let noise: Vec<Complex32> = (0..4_096_000).map(|_| Complex32::new(rng.gauss() as f32, rng.gauss() as f32)).collect();
    let s = survey(&noise, 2_048_000.0, 1.0).unwrap();
    assert!(s.sync_s.is_none() && s.listened_s <= 1.06);
    assert_eq!(field(&s.meta, "why"), &Value::Text("no_signal".into()));
    for (snr, a, b) in rows {
        eprintln!("survey at {snr} dB: FIC read after {a:.2} s, ensemble complete after {b:.2} s");
    }
    eprintln!("survey of noise: no_signal after {:.2} s", s.listened_s);
}

#[test]
fn acquisition_holds_from_any_starting_point_and_never_locks_to_noise() {
    use shack_sdr::dab::survey;
    // The receiver starts anywhere in a frame, after any length of noise.
    let worst = std::thread::scope(|sc| {
        let hs: Vec<_> = (0..24u64)
            .map(|k| {
                sc.spawn(move || {
                    let lead = 0.003 + 0.0041 * k as f64; // 3 to 97 ms
                    let iq = dab_iq(&TxEnsemble::standard(), 10, &Channel { snr_db: Some(9.0), lead_s: lead, seed: 100 + k, offset_hz: 2_000.0 * (k as f64 - 12.0), ..Default::default() });
                    let s = survey(&iq, 2_048_000.0, 1.0).unwrap();
                    (k, s.sync_s.unwrap_or(f64::INFINITY) - lead)
                })
            })
            .collect();
        hs.into_iter().map(|h| h.join().unwrap()).fold((0, 0.0f64), |a, b| if b.1 > a.1 { b } else { a })
    });
    eprintln!("slowest of 24 starts: FIC read {:.2} s after the first frame began (case {})", worst.1, worst.0);
    assert!(worst.1 < 0.5, "case {} took {:.2} s", worst.0, worst.1);
    // Two seconds of noise each, eight times: never a sync.
    for seed in 0..8 {
        let mut rng = shack_sdr::testsig::Rng::new(seed);
        let noise: Vec<Complex32> = (0..4_096_000).map(|_| Complex32::new(rng.gauss() as f32, rng.gauss() as f32)).collect();
        let s = survey(&noise, 2_048_000.0, 2.0).unwrap();
        assert!(s.sync_s.is_none(), "noise seed {seed} synced");
        assert_eq!(field(&s.meta, "ensemble"), &Value::Null);
    }
}

/// What an RSP's samples look like to the decoder on a Band III block with
/// no ensemble when what comes in through the antenna fades for a
/// millisecond twice a second: noise at 2.4 MS/s in 50 ms blocks (an
/// SdrplaySource's), the receiver's DC residue already taken out by the
/// app's DC blocker, so each fade is deep. Before the search always moved
/// on, Dab::feed never returned from one of these blocks: the search
/// settled 9-16 blocks of 64 before a fade, found the "null" ending at the
/// first sample it held, and its NoPrs miss dropped nothing (ohs-box's
/// decoder thread, 23/10/2026; openhamshack-nb tests/test_ham_dab_rsp.py).
/// The feed runs on a thread of its own so a decoder that never comes back
/// fails this by its deadline instead of hanging the run.
#[test]
fn a_fading_empty_block_at_2_4_ms_never_holds_the_decoder() {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let rate = 2_400_000.0;
        let block = 120_000usize;
        let mut seed = 5u64;
        let mut u = move || {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1_442_695_040_888_963_407);
            ((seed >> 11) as f64 + 0.5) / (1u64 << 53) as f64
        };
        let mut d = Dab::new();
        let mut sink = Sink::new();
        for i in 0..200 {
            let x: Vec<Complex32> = (0..block)
                .map(|j| {
                    let t = i * block + j;
                    let (a, b) = (u(), u());
                    let r = 1e-3 * (-2.0 * a.ln()).sqrt();
                    let th = 2.0 * std::f64::consts::PI * b;
                    let on = if t % 1_200_000 >= 2_400 { 1.0 } else { 0.0 };
                    Complex32::new((on * r * th.cos()) as f32, (on * r * th.sin()) as f32)
                })
                .collect();
            d.feed(Input::Iq { samples: &x, rate, t0: i as f64 * 0.05, index: (i * block) as u64 }, &mut sink).unwrap();
            let _ = tx.send(i);
        }
    });
    let mut got = 0;
    while got < 199 {
        match rx.recv_timeout(std::time::Duration::from_secs(20)) {
            Ok(i) => got = i,
            Err(_) => panic!("Dab::feed never came back from block {} of 200", got + 1),
        }
    }
}
