//! The SSTV decoder against the golden numbers captured from
//! openhamshack-nb's Python decoder (testdata/sstv, tools/sstv_golden.py),
//! and on its own for the ten modes sstv.py never had.
//!
//! Every fixture is regenerated from its recipe by the test transmitter
//! (shack_sdr::sstv::tx) and checked byte for byte (sha256) before it is
//! decoded, so the numbers compared are on exactly the samples sstv.py saw.
//! Each is decoded as Radio's engine feeds a mode decoder: 12 kHz audio in
//! 0.1 s blocks. The picture scored is the PNG the decoder hands the host,
//! read back, against the picture sent: PSNR, and the share of its rows
//! within a pixel of where they belong (tx::score).
//!
//! `cargo test --release --test sstv -- --ignored --nocapture sweep` prints
//! the whole table, Rust against Python, every mode and channel.

use serde_json::Value as J;
use sha2::{Digest, Sha256};
use shack_sdr::decoder::{Decoder, ImageEvent, Input, InputKind, Output, Params, Sink, Value};
use shack_sdr::host::{Body, Host, HostConfig, HostError, Item};
use shack_sdr::sstv::modes::{self, Mode};
use shack_sdr::sstv::{png, tx, Sstv};
use shack_sdr::testsig::{fsk, Watterson};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

const DATA: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/sstv");
const RATE: f64 = 12_000.0;
/// UTC of the first sample fed: a picture's `started` and its file's name
/// come from here (2027-01-15 08:00:00).
const T0: f64 = 1.8e9;

/// One picture as the decoder gave it.
#[derive(Debug, Default, Clone)]
struct Pic {
    width: u32,
    /// The finished picture, read back from its PNG (empty: no file).
    rgb: Vec<u8>,
    file: String,
    meta: Vec<(String, Value)>,
    /// Rows as last sent in `Lines` events, live and re-sent.
    live: Vec<Option<Vec<u8>>>,
    lines_events: usize,
}

#[derive(Debug, Default)]
struct Run {
    pics: Vec<Pic>,
    starts: usize,
    metas: Vec<Vec<(String, Value)>>,
    cpu_s: f64,
}

fn field<'a>(m: &'a [(String, Value)], k: &str) -> Option<&'a Value> {
    m.iter().find(|f| f.0 == k).map(|f| &f.1)
}

fn num(m: &[(String, Value)], k: &str) -> Option<f64> {
    match field(m, k) {
        Some(Value::Num(x)) => Some(*x),
        _ => None,
    }
}

fn text<'a>(m: &'a [(String, Value)], k: &str) -> Option<&'a str> {
    match field(m, k) {
        Some(Value::Text(t)) => Some(t),
        _ => None,
    }
}

fn collect(items: Vec<(f64, Output)>, run: &mut Run, cur: &mut Option<Pic>) {
    for (_, o) in items {
        match o {
            Output::Image { event: ImageEvent::Start { width, height, format } } => {
                assert_eq!(format, "rgb");
                run.starts += 1;
                *cur = Some(Pic { width, live: vec![None; height as usize], ..Default::default() });
            }
            Output::Image { event: ImageEvent::Lines { from, rgb } } => {
                let p = cur.as_mut().expect("lines before start");
                let w3 = p.width as usize * 3;
                assert_eq!(rgb.len() % w3, 0);
                p.lines_events += 1;
                for (i, row) in rgb.chunks(w3).enumerate() {
                    let r = from as usize + i;
                    assert!(r < p.live.len(), "row {r} beyond the picture");
                    p.live[r] = Some(row.to_vec());
                }
            }
            Output::Image { event: ImageEvent::Done { file, png: bytes, .. } } => {
                let p = cur.as_mut().expect("done before start");
                let (w, _h, rgb) = png::decode(&bytes).expect("a PNG");
                assert_eq!(w, p.width);
                p.rgb = rgb;
                p.file = file;
            }
            Output::Meta { fields } => {
                let state = text(&fields, "state").map(str::to_string);
                run.metas.push(fields.clone());
                if matches!(state.as_deref(), Some("done") | Some("lost")) {
                    let mut p = cur.take().expect("a picture to end");
                    p.meta = fields;
                    run.pics.push(p);
                }
            }
            _ => {}
        }
    }
}

/// Feed `audio` in blocks of `block` samples at `rate`, as the engine does.
fn decode_at(audio: &[f32], rate: f64, block: usize, params: Params) -> Run {
    let mut d = Sstv::new();
    d.configure(&params).unwrap();
    let mut sink = Sink::new();
    let mut run = Run::default();
    let mut cur = None;
    let t = shack_sdr::cpu::thread_time();
    for (k, c) in audio.chunks(block).enumerate() {
        let input = Input::Audio { samples: c, rate, t0: T0 + (k * block) as f64 / rate, index: (k * block) as u64 };
        d.feed(input, &mut sink).unwrap();
        collect(sink.take(), &mut run, &mut cur);
    }
    run.cpu_s = (shack_sdr::cpu::thread_time() - t).as_secs_f64();
    run
}

/// As the engine feeds a mode decoder: 12 kHz audio in 0.1 s blocks.
fn decode(audio: &[f32], params: Params) -> Run {
    decode_at(audio, RATE, 1200, params)
}

/// The picture this run got that scores best (a run may, rightly, give
/// none).
fn best_score(run: &Run, sent: &[u8], m: &Mode, partial: bool) -> Option<tx::Score> {
    run.pics
        .iter()
        .filter(|p| !p.rgb.is_empty() && p.width == m.width)
        .map(|p| tx::score(sent, m.width, m.height, &p.rgb, partial))
        .max_by(|a, b| a.psnr.total_cmp(&b.psnr))
}

/// The test card sent in `mode` through `ch` and decoded: (its score, the
/// run, seconds of audio).
fn run_case(mode: &'static Mode, ch: &tx::Channel) -> (Option<tx::Score>, Run, f64) {
    let img = tx::test_card(mode.width, mode.height);
    let audio = tx::audio(mode, &img, RATE, ch);
    let run = decode(&audio, Params::new());
    (best_score(&run, &img, mode, ch.cut > 0.0 || !ch.vis), run, audio.len() as f64 / RATE)
}

/// `f` over `items` on a few threads, results in order.
fn par<T: Sync, R: Send>(items: &[T], f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(2).clamp(1, 6);
    let next = std::sync::atomic::AtomicUsize::new(0);
    let out = std::sync::Mutex::new(Vec::new());
    std::thread::scope(|sc| {
        for _ in 0..threads {
            sc.spawn(|| loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let Some(x) = items.get(i) else { break };
                let r = f(x);
                out.lock().unwrap().push((i, r));
            });
        }
    });
    let mut v = out.into_inner().unwrap();
    v.sort_by_key(|x| x.0);
    v.into_iter().map(|x| x.1).collect()
}

fn all_modes() -> Vec<&'static Mode> {
    modes::MODES.iter().collect()
}

fn ch(snr: Option<f64>) -> tx::Channel {
    tx::Channel { snr_db: snr, tail_s: 3.0, ..Default::default() }
}

// ---------------------------------------------------------------- golden

fn load(name: &str) -> J {
    serde_json::from_str(&std::fs::read_to_string(format!("{DATA}/{name}")).unwrap()).unwrap()
}

#[derive(Clone, Debug)]
struct Case {
    id: String,
    group: String,
    mode: &'static Mode,
    ch: tx::Channel,
    sha256: String,
}

fn cases() -> Vec<Case> {
    load("cases.json")["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| Case {
            id: c["id"].as_str().unwrap().into(),
            group: c["group"].as_str().unwrap().into(),
            mode: modes::by_id(c["mode"].as_str().unwrap()).unwrap(),
            ch: tx::Channel {
                snr_db: c["snr_db"].as_f64(),
                offset_hz: c["offset_hz"].as_f64().unwrap(),
                clock_ppm: c["clock_ppm"].as_f64().unwrap(),
                fading: match c["fading"].as_str() {
                    Some("moderate") => Some(Watterson::MODERATE),
                    Some("good") => Some(Watterson::GOOD),
                    _ => None,
                },
                lead_s: c["lead_s"].as_f64().unwrap(),
                tail_s: c["tail_s"].as_f64().unwrap(),
                cut: c["cut"].as_f64().unwrap(),
                seed: c["seed"].as_u64().unwrap(),
                vis: c["vis"].as_bool().unwrap(),
            },
            sha256: c["sha256"].as_str().unwrap().into(),
        })
        .collect()
}

fn sha(x: &[f32]) -> String {
    let mut h = Sha256::new();
    for v in x {
        h.update(v.to_le_bytes());
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// Every golden case decoded (its fixture checked first): (case, sstv.py's
/// result, ours, Rust's CPU as a share of a core).
fn golden_runs() -> Vec<(Case, J, Option<tx::Score>, f64)> {
    let golden = load("golden.json");
    let want: BTreeMap<String, J> =
        golden["results"].as_array().unwrap().iter().map(|r| (r["id"].as_str().unwrap().to_string(), r.clone())).collect();
    let all = cases();
    par(&all, |case| {
        let img = tx::test_card(case.mode.width, case.mode.height);
        let audio = tx::audio(case.mode, &img, RATE, &case.ch);
        assert_eq!(sha(&audio), case.sha256, "{}: the fixture changed", case.id);
        let run = decode(&audio, Params::new());
        let s = best_score(&run, &img, case.mode, case.ch.cut > 0.0 || !case.ch.vis);
        (case.clone(), want[&case.id].clone(), s, run.cpu_s / (audio.len() as f64 / RATE))
    })
}

/// Never worse than sstv.py: PSNR, and the share of rows in their place,
/// case by case; through fading, on the mean of each condition's fades
/// (one fade can favour either decoder).
#[test]
fn never_worse_than_sstv_py() {
    let mut bad = Vec::new();
    let mut groups: BTreeMap<(String, String), Vec<[f64; 4]>> = BTreeMap::new();
    for (case, py, s, _) in golden_runs() {
        let (pp, ps) = (py["psnr"].as_f64().unwrap(), py["inSync"].as_f64().unwrap());
        let (rp, rs) = s.as_ref().map(|s| (s.psnr, s.in_sync)).unwrap_or((0.0, 0.0));
        if case.group.starts_with("fading") {
            groups.entry((case.mode.id.to_string(), case.group.clone())).or_default().push([pp, ps, rp, rs]);
        } else if rp < pp || rs + 1e-9 < ps {
            bad.push(format!("{}: PSNR {rp:.2} against {pp:.2}, rows in place {rs:.3} against {ps:.3}", case.id));
        }
    }
    assert_eq!(groups.len(), 8);
    for ((mode, group), v) in groups {
        let n = v.len() as f64;
        let mean = |i: usize| v.iter().map(|x| x[i]).sum::<f64>() / n;
        let (pp, ps, rp, rs) = (mean(0), mean(1), mean(2), mean(3));
        if rp < pp || rs < ps {
            bad.push(format!("{mode} {group}, mean of {n}: PSNR {rp:.2} against {pp:.2}, rows {rs:.3} against {ps:.3}"));
        }
    }
    assert!(bad.is_empty(), "worse than sstv.py:\n{}", bad.join("\n"));
}

/// The table: every golden case, Rust against Python, and the same
/// channels for the modes sstv.py lacks.
#[test]
#[ignore]
fn sweep() {
    println!("| Case | Python PSNR | Rust PSNR | Python rows in place | Rust rows in place | Rust CPU |");
    println!("|---|---|---|---|---|---|");
    for (case, py, s, cpu) in golden_runs() {
        let (rp, rs) = s.as_ref().map(|s| (s.psnr, s.in_sync)).unwrap_or((0.0, 0.0));
        println!(
            "| {} | {:.2} | {:.2} | {:.1} % | {:.1} % | {:.2} % |",
            case.id,
            py["psnr"].as_f64().unwrap(),
            rp,
            100.0 * py["inSync"].as_f64().unwrap(),
            100.0 * rs,
            100.0 * cpu
        );
    }
    let conds: Vec<(&str, tx::Channel)> = vec![
        ("clean", ch(None)),
        ("awgn20", ch(Some(20.0))),
        ("awgn10", ch(Some(10.0))),
        ("awgn6", ch(Some(6.0))),
        ("offset-100", tx::Channel { offset_hz: -100.0, ..ch(Some(20.0)) }),
        ("clock+200", tx::Channel { clock_ppm: 200.0, ..ch(Some(20.0)) }),
        ("clock-200", tx::Channel { clock_ppm: -200.0, ..ch(Some(20.0)) }),
        ("fading-moderate", tx::Channel { fading: Some(Watterson::MODERATE), ..ch(Some(25.0)) }),
        ("joined", tx::Channel { cut: 0.4, ..ch(Some(20.0)) }),
    ];
    println!("\n| Mode | {} |", conds.iter().map(|c| c.0).collect::<Vec<_>>().join(" | "));
    println!("|---|{}", "---|".repeat(conds.len()));
    let jobs: Vec<(&'static Mode, usize)> = modes::MODES.iter().flat_map(|m| (0..conds.len()).map(move |i| (m, i))).collect();
    let cells = par(&jobs, |(m, i)| {
        let (s, _, _) = run_case(m, &conds[*i].1);
        s.map(|s| format!("{:.1} / {:.0} %", s.psnr, 100.0 * s.in_sync)).unwrap_or_else(|| "none".into())
    });
    for (j, m) in modes::MODES.iter().enumerate() {
        println!("| {} | {} |", m.id, cells[j * conds.len()..(j + 1) * conds.len()].join(" | "));
    }
}

// ---------------------------------------------------------------- every mode

/// Every mode, sstv.py's four and the ten it never had: clean, every row
/// in place; and through 15 dB with the sender 150 ppm fast and the dial
/// 80 Hz low, at most a few rows out. The floors are the weakest mode's
/// (Robot 36's 27 dB clean, 21 at 15 dB) less 1.5 dB.
#[test]
fn every_mode_decodes_straight() {
    let clean = par(&all_modes(), |m| run_case(m, &ch(None)).0);
    let rough = tx::Channel { clock_ppm: 150.0, offset_hz: -80.0, seed: 7, ..ch(Some(15.0)) };
    let hard = par(&all_modes(), |m| run_case(m, &rough).0);
    for ((m, c), h) in modes::MODES.iter().zip(clean).zip(hard) {
        let c = c.unwrap_or_else(|| panic!("{}: no picture", m.id));
        let h = h.unwrap_or_else(|| panic!("{}: no picture at 15 dB", m.id));
        assert!(c.psnr >= 25.5 && c.in_sync == 1.0 && c.max_shift < 1.0, "{}: {c:?}", m.id);
        assert!(h.psnr >= 19.5 && h.in_sync >= 0.9, "{}: {h:?}", m.id);
    }
}

/// Joined part way (the header lost with the first 40 %): every mode
/// starts from its line syncs, in the right mode, with no row from before
/// the join and every row in place.
#[test]
fn every_mode_starts_from_its_line_syncs() {
    let joined = tx::Channel { cut: 0.4, ..ch(Some(20.0)) };
    let got = par(&all_modes(), |m| {
        let (s, run, _) = run_case(m, &joined);
        let pics: Vec<(String, Option<Value>)> = run
            .pics
            .iter()
            .map(|p| (text(&p.meta, "mode").unwrap_or("").to_string(), field(&p.meta, "partial").cloned()))
            .collect();
        (s, pics)
    });
    for (m, (s, pics)) in modes::MODES.iter().zip(got) {
        assert_eq!(pics, vec![(m.id.to_string(), Some(Value::Bool(true)))], "{}", m.id);
        let s = s.unwrap();
        // The whole lines after the cut (40 % of the transmission, header
        // and all), less at most two; and Scottie's line the cut fell in,
        // whose sync (after its green and blue) came after it.
        let lay = m.layout();
        let line_s = lay.line_ms / 1000.0;
        let sent = (modes::VIS_MS + m.head_ms) / 1000.0 + m.lines() as f64 * line_s;
        let first = ((0.4 * sent - (modes::VIS_MS + m.head_ms) / 1000.0) / line_s).ceil() as u32;
        let left = ((m.lines() - first) * m.rows) as usize;
        let most = left + usize::from(lay.sync_off_ms > 0.0);
        assert!(s.rows <= most && s.rows + 2 * m.rows as usize >= left, "{}: {left} rows left, {s:?}", m.id);
        assert!(s.in_sync >= 0.98 && s.psnr >= 20.0, "{}: {s:?}", m.id);
    }
}

// ---------------------------------------------------------------- inputs

/// The same picture as IQ (centred on 1900 Hz, as a session's channel
/// would be), and as audio at 48 and 44.1 kHz, decodes as 12 kHz audio
/// does: the same filters, decimated, never resampled.
#[test]
fn iq_and_other_rates_decode_alike() {
    let m = modes::by_id("martin1").unwrap();
    let img = tx::test_card(m.width, m.height);
    let c = ch(Some(20.0));
    let base = best_score(&decode(&tx::audio(m, &img, RATE, &c), Params::new()), &img, m, false).unwrap();
    for rate in [48_000.0, 44_100.0] {
        let a = tx::audio(m, &img, rate, &c);
        let s = best_score(&decode_at(&a, rate, (rate / 10.0) as usize, Params::new()), &img, m, false).unwrap();
        assert!(s.psnr >= base.psnr - 0.7 && s.in_sync == 1.0, "{rate}: {s:?} against {base:?}");
    }
    for rate in [12_000.0, 48_000.0] {
        let iq = tx::iq(m, &img, rate, &c);
        let block = (rate / 10.0) as usize;
        let mut d = Sstv::new();
        let mut sink = Sink::new();
        let (mut run, mut cur) = (Run::default(), None);
        for (k, b) in iq.chunks(block).enumerate() {
            let input = Input::Iq { samples: b, rate, t0: T0 + k as f64 * 0.1, index: (k * block) as u64 };
            d.feed(input, &mut sink).unwrap();
            collect(sink.take(), &mut run, &mut cur);
        }
        let s = best_score(&run, &img, m, false).unwrap();
        assert!(s.psnr >= base.psnr - 0.7 && s.in_sync == 1.0, "IQ {rate}: {s:?} against {base:?}");
    }
    // Too slow to carry 2300 Hz.
    let mut d = Sstv::new();
    let err = d.feed(Input::Audio { samples: &[0.0; 100], rate: 4_000.0, t0: 0.0, index: 0 }, &mut Sink::new());
    assert_eq!(err.unwrap_err().code(), "rate");
}

/// What the feed says about a picture: its mode and header, when it
/// started (from the samples), the sender's clock error, the SNR and the
/// tuning offset, and the file it was saved as.
#[test]
fn the_meta_describes_the_picture() {
    let m = modes::by_id("scottie1").unwrap();
    let cases = [(200.0, 40.0, 20.0), (-200.0, -150.0, 10.0)];
    let runs = par(&cases, |&(ppm, off, snr)| run_case(m, &tx::Channel { clock_ppm: ppm, offset_hz: off, ..ch(Some(snr)) }).1);
    for ((ppm, off, snr), run) in cases.iter().zip(runs) {
        assert_eq!(run.pics.len(), 1);
        let p = &run.pics[0];
        let f = &p.meta;
        assert_eq!(text(f, "state"), Some("done"));
        assert_eq!(text(f, "mode"), Some("scottie1"));
        assert_eq!(num(f, "vis"), Some(60.0));
        assert_eq!(field(f, "partial"), Some(&Value::Bool(false)));
        assert_eq!((num(f, "width"), num(f, "height"), num(f, "rows")), (Some(320.0), Some(256.0), Some(256.0)));
        // The header began 0.5 s into the samples (the channel's lead).
        assert!((num(f, "started").unwrap() - (T0 + 0.5)).abs() < 0.005, "{f:?}");
        assert!((num(f, "slantPpm").unwrap() - ppm).abs() < 5.0, "{f:?}");
        assert!((num(f, "offsetHz").unwrap() - off).abs() < 2.0, "{f:?}");
        assert!((num(f, "snrDb").unwrap() - snr).abs() < 1.5, "{f:?}");
        assert_eq!(text(f, "file"), Some(p.file.as_str()));
        assert_eq!(p.file, "sstv_20270115-080000Z_scottie1.png");
    }
    // While it arrives: at the start and every sixteen lines.
    let run = run_case(modes::by_id("robot36").unwrap(), &ch(Some(20.0))).1;
    let receiving = run.metas.iter().filter(|f| text(f, "state") == Some("receiving")).count();
    assert!((12..=16).contains(&receiving), "{receiving}");
}

/// The picture is sent as it arrives: every row in a `Lines` event before
/// the end, and after the final straightening every row as sent is what
/// the file holds.
#[test]
fn rows_arrive_as_they_are_read() {
    let m = modes::by_id("pd90").unwrap();
    let (_, run, _) = run_case(m, &tx::Channel { clock_ppm: 200.0, ..ch(Some(15.0)) });
    let p = &run.pics[0];
    assert!(p.lines_events >= (m.lines() / 2) as usize, "{}", p.lines_events);
    let w3 = m.width as usize * 3;
    for (r, row) in p.live.iter().enumerate() {
        let want = &p.rgb[r * w3..(r + 1) * w3];
        let got = row.as_deref().unwrap_or_else(|| panic!("row {r} never sent"));
        assert!(got.iter().zip(want).all(|(a, b)| (*a as i32 - *b as i32).abs() <= 6), "row {r}");
    }
}

// ---------------------------------------------------------------- not a picture

/// A minute of noise, a steady 1200 Hz carrier and silence start nothing.
#[test]
fn noise_a_carrier_and_silence_start_nothing() {
    let n = (60.0 * RATE) as usize;
    let mut rng = shack_sdr::testsig::Rng::new(5);
    let noise: Vec<f32> = (0..n).map(|_| (0.3 * rng.gauss()) as f32).collect();
    let carrier: Vec<f32> = (0..n)
        .map(|i| (0.5 * shack_sdr::testsig::sin_cos_turns(1200.0 * i as f64 / RATE).0 + 0.05 * rng.gauss()) as f32)
        .collect();
    let silence = vec![0.0f32; n / 4];
    for (name, x) in [("noise", noise), ("carrier", carrier), ("silence", silence)] {
        let run = decode(&x, Params::new());
        assert_eq!(run.starts, 0, "{name}");
        assert!(run.metas.is_empty(), "{name}: {:?}", run.metas);
    }
}

/// A header for a mode not decoded here (Wraase SC2-180, VIS 0x37) is
/// named in the feed, and starts no picture.
#[test]
fn an_unsupported_header_is_named() {
    let segs: Vec<(f64, f64)> = modes::vis_segments(0x37).into_iter().map(|(ms, hz)| (hz, ms / 1000.0)).collect();
    let mut x = vec![0.0f32; 6000];
    x.extend(fsk(&segs, RATE, 0.0, 0.0).iter().map(|z| (0.5 * z.re) as f32));
    x.extend(vec![0.0f32; 24_000]);
    let run = decode(&x, Params::new());
    assert_eq!(run.starts, 0);
    assert_eq!(run.metas.len(), 1);
    assert_eq!(text(&run.metas[0], "state"), Some("unsupported"));
    assert_eq!(text(&run.metas[0], "mode"), Some("wraase_sc2_180"));
    assert_eq!(num(&run.metas[0], "vis"), Some(55.0));
}

// ---------------------------------------------------------------- robustness

/// Two pictures back to back, the first cut off half way by the second's
/// header (the sender gave up on it): the first ends there, "lost", with
/// the rows it had; the second is whole. (And a header heard in a
/// picture's own content - PD's white rows pass for one - never cuts it:
/// every PD picture in these tests is whole.)
#[test]
fn a_new_header_ends_the_picture_before_it() {
    let (m1, m2) = (modes::by_id("martin1").unwrap(), modes::by_id("robot36").unwrap());
    let (i1, i2) = (tx::test_card(m1.width, m1.height), tx::test_card(m2.width, m2.height));
    let a1 = tx::audio(m1, &i1, RATE, &tx::Channel { tail_s: 0.0, ..ch(Some(25.0)) });
    let a2 = tx::audio(m2, &i2, RATE, &tx::Channel { lead_s: 0.0, seed: 2, ..ch(Some(25.0)) });
    let mut x = a1[..a1.len() / 2].to_vec();
    x.extend_from_slice(&a2);
    let run = decode(&x, Params::new());
    assert_eq!(run.pics.len(), 2, "{:?}", run.metas);
    let (p1, p2) = (&run.pics[0], &run.pics[1]);
    assert_eq!(text(&p1.meta, "state"), Some("lost"));
    let rows = num(&p1.meta, "rows").unwrap();
    assert!((120.0..=130.0).contains(&rows), "{rows}");
    let s1 = tx::score(&i1, m1.width, m1.height, &p1.rgb[..120 * 320 * 3], true);
    assert!(s1.row_offset == 0 && s1.in_sync == 1.0 && s1.psnr > 25.0, "{s1:?}");
    assert_eq!(text(&p2.meta, "mode"), Some("robot36"));
    let s2 = tx::score(&i2, m2.width, m2.height, &p2.rgb, false);
    assert!(s2.in_sync == 1.0 && s2.psnr > 20.0, "{s2:?}");
}

/// Blocks of any size, and a half-second gap in the samples (a dropped
/// USB buffer): the picture comes out straight, the gap bridged in time.
#[test]
fn ragged_blocks_and_a_gap() {
    let m = modes::by_id("robot72").unwrap();
    let img = tx::test_card(m.width, m.height);
    let a = tx::audio(m, &img, RATE, &ch(Some(25.0)));
    let gap = (RATE * 40.0) as usize..(RATE * 40.5) as usize;
    let mut d = Sstv::new();
    let mut sink = Sink::new();
    let (mut run, mut cur) = (Run::default(), None);
    let sizes = [37usize, 500, 1, 2000, 193];
    let (mut i, mut s) = (0usize, 0usize);
    while i < a.len() {
        let n = sizes[s % sizes.len()].min(a.len() - i);
        s += 1;
        if !gap.contains(&i) {
            let input = Input::Audio { samples: &a[i..i + n], rate: RATE, t0: T0 + i as f64 / RATE, index: i as u64 };
            d.feed(input, &mut sink).unwrap();
            collect(sink.take(), &mut run, &mut cur);
        }
        i += n;
    }
    assert_eq!(run.pics.len(), 1);
    let s = best_score(&run, &img, m, false).unwrap();
    // The two lines the gap took are noise; every other row is in place.
    assert!(s.in_sync >= 0.98 && s.psnr > 18.0, "{s:?}");
}

/// reset() drops a picture in progress; the next header starts afresh.
#[test]
fn reset_drops_the_picture() {
    let m = modes::by_id("robot36").unwrap();
    let img = tx::test_card(m.width, m.height);
    let a = tx::audio(m, &img, RATE, &ch(Some(25.0)));
    let mut d = Sstv::new();
    let mut sink = Sink::new();
    for (k, c) in a[..(12.0 * RATE) as usize].chunks(1200).enumerate() {
        d.feed(Input::Audio { samples: c, rate: RATE, t0: k as f64 * 0.1, index: (k * 1200) as u64 }, &mut sink).unwrap();
    }
    assert!(d.status().sync);
    d.reset();
    assert!(!d.status().sync);
    sink.take();
    let (mut run, mut cur) = (Run::default(), None);
    for (k, c) in a.chunks(1200).enumerate() {
        let input = Input::Audio { samples: c, rate: RATE, t0: 100.0 + k as f64 * 0.1, index: (k * 1200) as u64 };
        d.feed(input, &mut sink).unwrap();
        collect(sink.take(), &mut run, &mut cur);
    }
    assert_eq!(run.starts, 1);
    assert_eq!(best_score(&run, &img, m, false).unwrap().in_sync, 1.0);
}

#[test]
fn parameters_are_checked_and_obeyed() {
    let mut d = Sstv::new();
    assert!(d.configure(&Params::new().with("mode", Value::Text("pd120".into()))).is_ok());
    assert!(d.configure(&Params::new().with("mode", Value::Text("".into()))).is_ok());
    assert_eq!(d.configure(&Params::new().with("mode", Value::Text("wraase".into()))).unwrap_err().code(), "bad_param");
    assert_eq!(d.configure(&Params::new().with("afcRangeHz", Value::Num(400.0))).unwrap_err().code(), "bad_param");
    assert_eq!(d.configure(&Params::new().with("slant", Value::Bool(true))).unwrap_err().code(), "unknown_param");
    // `mode` decodes that mode only; with `syncStart` off only a header
    // starts a picture.
    let m = modes::by_id("martin2").unwrap();
    let img = tx::test_card(m.width, m.height);
    let a = tx::audio(m, &img, RATE, &ch(Some(20.0)));
    let only = Params::new().with("mode", Value::Text("martin1".into())).with("syncStart", Value::Bool(false));
    assert_eq!(decode(&a, only).starts, 0);
    let joined = tx::audio(m, &img, RATE, &tx::Channel { cut: 0.3, ..ch(Some(20.0)) });
    assert_eq!(decode(&joined, Params::new().with("syncStart", Value::Bool(false))).starts, 0);
    assert_eq!(decode(&joined, Params::new()).starts, 1);
    // A header 140 Hz off tune is found within the default range only.
    let off = tx::audio(m, &img, RATE, &tx::Channel { offset_hz: 140.0, ..ch(Some(20.0)) });
    assert_eq!(decode(&off, Params::new()).pics.len(), 1);
    let narrow = Params::new().with("afcRangeHz", Value::Num(50.0)).with("syncStart", Value::Bool(false));
    assert_eq!(decode(&off, narrow).starts, 0);
}

// ---------------------------------------------------------------- the host

fn poll_until(h: &Host, secs: f64, mut pred: impl FnMut(&[Item]) -> bool) -> Vec<Item> {
    let mut all = Vec::new();
    let end = Instant::now() + Duration::from_secs_f64(secs);
    while Instant::now() < end {
        all.extend(h.poll(5000));
        if pred(&all) {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    all
}

/// Through the host, as an app runs it: the picture arrives as `image`
/// results (start, lines, done) and `meta`; the PNG is kept, written to the
/// files folder whole (never a half-written file), named after the
/// session, and served by `file()` - after a restart too, from the folder -
/// while a name that could leave the folder is refused.
#[test]
fn the_host_keeps_and_serves_the_picture() {
    let dir = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("sstv-files-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let cfg = HostConfig { ring_s: 60.0, files_dir: Some(dir.clone()), ..Default::default() };
    let h = Host::new(cfg.clone());
    let sid = h.start("sstv", Params::new(), InputKind::Audio, RATE, 0.0, false).unwrap();
    let m = modes::by_id("robot36").unwrap();
    let img = tx::test_card(m.width, m.height);
    let a = tx::audio(m, &img, RATE, &ch(Some(25.0)));
    for (k, c) in a.chunks(12_000).enumerate() {
        h.feed_audio(&sid, c, T0 + k as f64, (k * 12_000) as u64).unwrap();
    }
    let items = poll_until(&h, 30.0, |all| {
        all.iter().any(|i| matches!(&i.body, Body::Output(Output::Image { event: ImageEvent::Done { .. } })))
    });
    let mut rows = 0;
    let mut done = None;
    let mut meta_file = None;
    for i in &items {
        if let Body::Output(Output::Meta { fields }) = &i.body {
            if text(fields, "state") == Some("done") {
                meta_file = text(fields, "file").map(str::to_string);
            }
        }
        if let Body::Output(Output::Image { event }) = &i.body {
            match event {
                ImageEvent::Lines { rgb, .. } => rows += rgb.len() / (320 * 3),
                ImageEvent::Done { file, png, path } => {
                    assert!(png.is_empty(), "the bytes stay with the host");
                    done = Some((file.clone(), path.clone()));
                }
                _ => {}
            }
        }
    }
    assert!(rows >= 240);
    let (file, path) = done.expect("a picture");
    assert!(file.starts_with(&format!("{sid}_sstv_")) && file.ends_with("_robot36.png"), "{file}");
    assert_eq!(path.as_deref(), dir.join(&file).to_str());
    // The picture's meta names the file as the host keeps it.
    assert_eq!(meta_file.as_deref(), Some(file.as_str()));
    assert_eq!(h.status(&sid).unwrap().meta.as_deref().and_then(|m| text(m, "file")), Some(file.as_str()));
    let bytes = h.file(&file).unwrap();
    assert_eq!(std::fs::read(dir.join(&file)).unwrap(), bytes);
    let (_, _, rgb) = png::decode(&bytes).unwrap();
    assert_eq!(tx::score(&img, 320, 240, &rgb, false).in_sync, 1.0);
    let listed = h.files();
    assert_eq!(listed.len(), 1);
    assert_eq!((listed[0].name.as_str(), listed[0].session.as_str()), (file.as_str(), sid.as_str()));
    assert_eq!((listed[0].decoder.as_str(), listed[0].size), ("sstv", bytes.len()));
    for bad in ["../x.png", "/etc/passwd", ".hidden", "a/b.png", ""] {
        assert_eq!(h.file(bad), None, "{bad}");
    }
    assert!(std::fs::read_dir(&dir).unwrap().all(|e| !e.unwrap().file_name().to_string_lossy().ends_with(".part")));
    assert!(h.close());
    // After a restart: from the folder.
    let h2 = Host::new(cfg);
    assert_eq!(h2.file(&file), Some(bytes));
    assert_eq!(h2.files().iter().map(|f| f.name.clone()).collect::<Vec<_>>(), vec![file]);
    let refused = h2.start("sstv", Params::new().with("bogus", Value::Num(1.0)), InputKind::Audio, RATE, 0.0, false);
    assert_eq!(refused.unwrap_err(), HostError::BadParams("unknown_param"));
    assert!(h2.close());
    let _ = std::fs::remove_dir_all(&dir);
}

// ---------------------------------------------------------------- CPU

/// CPU per second of audio, thread time, for the mode with the most
/// pixels a second (PD120) at 12 and 48 kHz: printed on every run, failing
/// only above 3x what one Ryzen 9 6900HX core measured (see BUDGET;
/// SHACK_SDR_CPU_BUDGET_SCALE sets the multiple), so it catches a
/// regression rather than a busy host.
#[test]
fn cpu_per_second_of_audio() {
    const BUDGET: [(f64, f64); 2] = [(12_000.0, 0.005), (48_000.0, 0.010)];
    let scale: f64 = std::env::var("SHACK_SDR_CPU_BUDGET_SCALE").ok().and_then(|x| x.parse().ok()).unwrap_or(3.0);
    let m = modes::by_id("pd120").unwrap();
    let img = tx::test_card(m.width, m.height);
    for (rate, budget) in BUDGET {
        let a = tx::audio(m, &img, rate, &ch(Some(20.0)));
        let run = decode_at(&a, rate, (rate / 10.0) as usize, Params::new());
        let share = run.cpu_s / (a.len() as f64 / rate);
        println!("sstv pd120 at {rate} Hz: {:.2} % of a core", 100.0 * share);
        assert_eq!(run.pics.len(), 1);
        assert!(share < budget * scale, "{:.2} % at {rate}", 100.0 * share);
    }
}
