//! The decoder host's promises (RADIO-SDR-PLAN §8.3 "The host"), each run
//! for real on its threads: feed never blocks and counts what a full ring
//! drops; a panicking decoder is one session's `error` and nothing else;
//! close() joins within a second; the budget pauses the newest session at
//! the cap and never the pinned one; and audio reaches an IQ decoder.

use shack_sdr::decoder::{InputKind, Output, Params, Value};
use shack_sdr::host::{Body, Host, HostConfig, HostError, Item, State};
use shack_sdr::testsig::{rtty_audio, rtty_iq, rtty_truth, RttyCase};
use std::time::{Duration, Instant};

fn host(cfg: HostConfig) -> Host {
    Host::new(HostConfig { testing: true, ..cfg })
}

fn ms(x: f64) -> Params {
    Params::new().with("ms", Value::Num(x))
}

/// Poll until `pred` holds over everything polled so far, or give up.
fn poll_until(h: &Host, secs: f64, mut pred: impl FnMut(&[Item]) -> bool) -> Vec<Item> {
    let mut all = Vec::new();
    let end = Instant::now() + Duration::from_secs_f64(secs);
    while Instant::now() < end {
        all.extend(h.poll(500));
        if pred(&all) {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    all
}

fn text_of(items: &[Item], sid: &str) -> String {
    items
        .iter()
        .filter(|i| i.session == sid)
        .filter_map(|i| match &i.body {
            Body::Output(Output::Text { text, .. }) => Some(text.as_str()),
            _ => None,
        })
        .collect()
}

#[test]
fn feed_never_blocks_and_a_full_ring_counts_its_drops() {
    let h = host(HostConfig::default());
    // 200 ms asleep per block, a ring of 2 s at 1 kS/s.
    let sid = h.start("test_sleep", ms(200.0), InputKind::Audio, 1000.0, 0.0, false).unwrap();
    let block = vec![0.0f32; 100];
    let t = Instant::now();
    for k in 0..100u64 {
        h.feed_audio(&sid, &block, 0.0, k * 100).unwrap();
    }
    let took = t.elapsed();
    // A hundred blocks, ten seconds of samples, against a decoder that
    // needs twenty seconds for them: feed returned at once regardless.
    assert!(took < Duration::from_millis(100), "{took:?}");
    let st = h.status(&sid).unwrap();
    assert_eq!(st.fed, 10_000);
    assert!(st.dropped >= 7_000, "dropped {}", st.dropped); // at most ~2 s kept, a block or two taken
    assert!(h.close());
}

#[test]
fn a_panicking_decoder_is_one_sessions_error_and_nothing_else() {
    let h = host(HostConfig::default());
    let bad = h.start("test_panic", Params::new().with("after", Value::Num(2.0)), InputKind::Iq, 1000.0, 0.0, false).unwrap();
    let good = h.start("test_echo", Params::new(), InputKind::Iq, 1000.0, 0.0, false).unwrap();
    let block = vec![Default::default(); 100];
    for k in 0..10u64 {
        h.feed_iq(&bad, &block, k as f64, k * 100).unwrap();
        h.feed_iq(&good, &block, k as f64, k * 100).unwrap();
        std::thread::sleep(Duration::from_millis(5));
    }
    let items = poll_until(&h, 3.0, |all| text_of(all, &good).matches('@').count() == 10);
    let st = h.status(&bad).unwrap();
    assert_eq!(st.state, State::Error);
    assert_eq!(st.why.as_deref(), Some("panic"));
    // Its two good blocks were reported before it failed, and the state change was announced.
    assert_eq!(text_of(&items, &bad), "100@0100@100");
    assert!(items.iter().any(|i| i.session == bad && matches!(i.body, Body::State { state: State::Error, .. })));
    // The other session, on the same worker, never noticed.
    assert_eq!(text_of(&items, &good).matches('@').count(), 10);
    assert!(h.status(&good).unwrap().state != State::Error);
    // An errored session takes no more samples, silently.
    let fed = h.status(&bad).unwrap().fed;
    h.feed_iq(&bad, &block, 0.0, 1000).unwrap();
    assert_eq!(h.status(&bad).unwrap().fed, fed);
    assert!(h.close());
}

#[test]
fn close_joins_within_a_second_while_decoding() {
    let h = host(HostConfig { threads: 3, ..Default::default() });
    let ids: Vec<String> =
        (0..3).map(|_| h.start("test_sleep", ms(150.0), InputKind::Audio, 1000.0, 0.0, false).unwrap()).collect();
    for k in 0..20u64 {
        for id in &ids {
            h.feed_audio(id, &[0.0; 100], 0.0, k * 100).unwrap();
        }
    }
    std::thread::sleep(Duration::from_millis(50));
    let t = Instant::now();
    assert!(h.close(), "every worker joined");
    assert!(t.elapsed() < Duration::from_secs(1), "{:?}", t.elapsed());
    assert_eq!(h.start("test_echo", Params::new(), InputKind::Iq, 1.0, 0.0, false), Err(HostError::Closed));
}

#[test]
fn the_budget_pauses_the_newest_session_and_never_the_pinned_one() {
    // A cap of 25 % of a core, and three sessions each burning 12 % of one
    // (12 ms of CPU per 100 ms block, fed in real time).
    let h = host(HostConfig { cpu_max: 0.25, ..Default::default() });
    let pinned = h.start("test_burn", ms(12.0), InputKind::Audio, 1000.0, 0.0, true).unwrap();
    let older = h.start("test_burn", ms(12.0), InputKind::Audio, 1000.0, 0.0, false).unwrap();
    let newest = h.start("test_burn", ms(12.0), InputKind::Audio, 1000.0, 0.0, false).unwrap();
    let start = Instant::now();
    let mut k = 0u64;
    while start.elapsed() < Duration::from_secs(4) {
        for id in [&pinned, &older, &newest] {
            h.feed_audio(id, &[0.0; 100], 0.0, k * 100).unwrap();
        }
        k += 1;
        std::thread::sleep(Duration::from_millis(100));
    }
    let state = |id: &str| h.status(id).unwrap().state;
    assert_eq!(state(&newest), State::Budget);
    assert_eq!(h.status(&newest).unwrap().why.as_deref(), Some("cpu_budget"));
    assert!(state(&older) != State::Budget, "{:?}", state(&older));
    assert!(state(&pinned) != State::Budget);
    // With the older one stopped there is room again: it comes back.
    h.stop(&older).unwrap();
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(5) && state(&newest) == State::Budget {
        h.feed_audio(&pinned, &[0.0; 100], 0.0, k * 100).unwrap();
        h.feed_audio(&newest, &[0.0; 100], 0.0, k * 100).unwrap();
        k += 1;
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(state(&newest) != State::Budget);
    let s = h.stats();
    assert!(s.cpu < 0.35, "{}", s.cpu);
    h.close();
}

#[test]
fn outside_is_fed_nothing_and_comes_back() {
    let h = host(HostConfig::default());
    let sid = h.start("test_echo", Params::new(), InputKind::Iq, 1000.0, 0.0, false).unwrap();
    h.set_outside(&sid, true).unwrap();
    h.feed_iq(&sid, &[Default::default(); 10], 0.0, 0).unwrap();
    assert_eq!(h.status(&sid).unwrap().state, State::Outside);
    assert_eq!(h.status(&sid).unwrap().why.as_deref(), Some("outside_span"));
    assert_eq!(h.status(&sid).unwrap().fed, 0);
    h.set_outside(&sid, false).unwrap();
    h.feed_iq(&sid, &[Default::default(); 10], 0.0, 10).unwrap();
    let items = poll_until(&h, 2.0, |all| !text_of(all, &sid).is_empty());
    assert_eq!(text_of(&items, &sid), "10@10");
    h.close();
}

#[test]
fn refusals_are_codes() {
    let h = Host::new(HostConfig { max_sessions: 1, ..Default::default() });
    assert_eq!(h.start("test_echo", Params::new(), InputKind::Iq, 1000.0, 0.0, false), Err(HostError::UnknownDecoder));
    assert_eq!(h.start("nonesuch", Params::new(), InputKind::Iq, 1000.0, 0.0, false), Err(HostError::UnknownDecoder));
    let bad = Params::new().with("baud", Value::Num(110.0));
    assert_eq!(h.start("rtty", bad, InputKind::Iq, 12_000.0, 0.0, false), Err(HostError::BadParams("bad_param")));
    let typo = Params::new().with("bauds", Value::Num(45.45));
    assert_eq!(h.start("rtty", typo, InputKind::Iq, 12_000.0, 0.0, false).unwrap_err().code(), "bad_params");
    let sid = h.start("rtty", Params::new(), InputKind::Iq, 12_000.0, 0.0, false).unwrap();
    assert_eq!(h.start("rtty", Params::new(), InputKind::Iq, 12_000.0, 0.0, false), Err(HostError::TooManySessions));
    assert_eq!(h.feed_audio(&sid, &[0.0], 0.0, 0), Err(HostError::BadInput("input_kind")));
    assert_eq!(h.status("ds_nope").unwrap_err().code(), "unknown_session");
    h.close();
}

fn decode_through_host(input: InputKind, case: &RttyCase) -> String {
    // Seven seconds of signal arrive at once: a ring that holds them all,
    // and no CPU cap, which in real time would be the budget's business.
    let h = Host::new(HostConfig { cpu_max: 0.0, ring_s: 30.0, ..Default::default() });
    let sid = h.start("rtty", Params::new(), input, case.rate, case.centre, false).unwrap();
    let blocks = 1200usize;
    match input {
        InputKind::Audio => {
            for (k, c) in rtty_audio(case).chunks(blocks).enumerate() {
                h.feed_audio(&sid, c, k as f64 * 0.1, (k * blocks) as u64).unwrap();
            }
        }
        InputKind::Iq => {
            for (k, c) in rtty_iq(case).chunks(blocks).enumerate() {
                h.feed_iq(&sid, c, k as f64 * 0.1, (k * blocks) as u64).unwrap();
            }
        }
    }
    let want = rtty_truth(&case.text);
    let items = poll_until(&h, 10.0, |all| text_of(all, &sid).ends_with('\n'));
    h.close();
    let got = text_of(&items, &sid);
    assert_eq!(got, want);
    got
}

#[test]
fn rtty_decodes_iq_and_audio_alike_through_the_host() {
    let text = "CQ CQ DE GM4BRI GM4BRI K\n";
    let audio = RttyCase { text: text.into(), snr_db: Some(0.0), seed: 11, ..Default::default() };
    decode_through_host(InputKind::Audio, &audio);
    let iq = RttyCase { centre: 0.0, ..audio };
    decode_through_host(InputKind::Iq, &iq);
}

/// Wait until the session has decoded `n` samples' worth of blocks.
fn wait_blocks(h: &Host, sid: &str, n: u64) {
    let end = Instant::now() + Duration::from_secs(5);
    while Instant::now() < end {
        let st = h.status(sid).unwrap();
        if st.decoder_status.counters.iter().any(|(k, v)| *k == "blocks" && *v >= n) {
            return;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    panic!("the session never decoded {n} blocks");
}

#[test]
fn sound_goes_to_its_own_ring_not_the_results() {
    let h = host(HostConfig::default());
    let sid = h.start("test_sound", Params::new(), InputKind::Iq, 48_000.0, 0.0, false).unwrap();
    let block = vec![rustfft::num_complex::Complex32::new(0.0, 0.0); 4800];
    for k in 0..10u64 {
        h.feed_iq(&sid, &block, 100.0 + k as f64 * 0.1, k * 4800).unwrap();
    }
    wait_blocks(&h, &sid, 10);
    // No result carries sound; the meta is there, and kept for status.
    let items = poll_until(&h, 1.0, |_| false);
    assert!(items.iter().all(|i| !matches!(i.body, Body::Output(Output::Audio { .. }))));
    assert!(items.iter().any(|i| matches!(i.body, Body::Output(Output::Meta { .. }))));
    let st = h.status(&sid).unwrap();
    let meta = st.meta.expect("the last meta is kept");
    assert!(meta.iter().any(|(k, v)| k == "blocks" && *v == Value::Num(10.0)));
    assert_eq!(st.audio_queued, 48_000);
    // One second of stereo 48 kHz, in one run, timed from the samples.
    let a = h.audio(&sid, 1_000_000, None, None).unwrap();
    assert_eq!(a.len(), 1);
    assert_eq!((a[0].rate, a[0].channels, a[0].frames()), (48_000, 2, 48_000));
    assert!((a[0].t - 100.0).abs() < 1e-9);
    assert!(h.audio(&sid, 100, None, None).unwrap().is_empty());
}

#[test]
fn sound_pulled_at_the_apps_rate_is_converted_and_continuous() {
    let h = host(HostConfig::default());
    let sid = h.start("test_sound", Params::new(), InputKind::Iq, 48_000.0, 0.0, false).unwrap();
    let block = vec![rustfft::num_complex::Complex32::new(0.0, 0.0); 4800];
    for k in 0..10u64 {
        h.feed_iq(&sid, &block, k as f64 * 0.1, k * 4800).unwrap();
    }
    wait_blocks(&h, &sid, 10);
    // Radio's ordinary audio: 12 kHz mono, pulled in small pieces.
    let mut pcm = Vec::new();
    loop {
        let a = h.audio(&sid, 1234, Some(12_000), Some(1)).unwrap();
        if a.is_empty() {
            break;
        }
        for c in a {
            assert_eq!((c.rate, c.channels), (12_000, 1));
            pcm.extend(c.pcm);
        }
    }
    assert!((pcm.len() as i64 - 12_000).abs() < 20, "{}", pcm.len());
    // Still the 1 kHz tone at 0.5, unbroken across the pulls: compare with a
    // fitted sine over the settled part.
    let y = &pcm[200..];
    let (mut c, mut s) = (0.0f64, 0.0f64);
    for (i, v) in y.iter().enumerate() {
        let p = 2.0 * std::f64::consts::PI * 1000.0 * i as f64 / 12_000.0;
        c += *v as f64 * p.cos();
        s += *v as f64 * p.sin();
    }
    let amp = 2.0 * (c * c + s * s).sqrt() / y.len() as f64;
    assert!((amp - 0.5).abs() < 0.01, "{amp}");
    assert!(h.audio(&sid, 10, Some(0), None).is_err());
}

#[test]
fn a_full_sound_ring_drops_the_oldest_and_counts_them() {
    let h = host(HostConfig { ring_s: 1.0, ..Default::default() });
    let sid = h.start("test_sound", Params::new(), InputKind::Iq, 48_000.0, 0.0, false).unwrap();
    let block = vec![rustfft::num_complex::Complex32::new(0.0, 0.0); 4800];
    // 3 s of sound, nobody pulling, a 1 s ring (the inbox is fed slowly
    // enough that it drops nothing itself).
    for k in 0..30u64 {
        h.feed_iq(&sid, &block, k as f64 * 0.1, k * 4800).unwrap();
        wait_blocks(&h, &sid, k + 1);
    }
    let st = h.status(&sid).unwrap();
    assert_eq!(st.audio_queued, 48_000);
    assert_eq!(st.audio_dropped, 96_000);
    let a = h.audio(&sid, 1_000_000, None, None).unwrap();
    // What is left is the newest second, timed as such.
    assert!((a[0].t - 2.0).abs() < 1e-6, "{}", a[0].t);
}

#[test]
fn configure_is_checked_at_once_and_applied_live() {
    let h = host(HostConfig::default());
    let sid = h.start("test_sound", Params::new().with("name", Value::Text("a".into())), InputKind::Iq, 48_000.0, 0.0, false).unwrap();
    assert_eq!(h.configure(&sid, Params::new().with("nope", Value::Num(1.0))), Err(HostError::BadParams("unknown_param")));
    assert_eq!(h.configure(&sid, Params::new().with("level", Value::Num(3.0))), Err(HostError::BadParams("bad_param")));
    assert_eq!(h.configure("ds_none", Params::new()), Err(HostError::UnknownSession));
    h.configure(&sid, Params::new().with("name", Value::Text("b".into()))).unwrap();
    // The session's parameters show it at once, merged with the rest.
    let st = h.status(&sid).unwrap();
    assert_eq!(st.params.text("name"), Some("b"));
    let block = vec![rustfft::num_complex::Complex32::new(0.0, 0.0); 480];
    h.feed_iq(&sid, &block, 0.0, 0).unwrap();
    wait_blocks(&h, &sid, 1);
    let meta = h.status(&sid).unwrap().meta.unwrap();
    assert!(meta.iter().any(|(k, v)| k == "name" && *v == Value::Text("b".into())), "{meta:?}");
}


#[test]
fn iq_below_a_decoders_minimum_rate_is_refused() {
    // DAB's OFDM needs 2.048 MS/s or more (its catalogue entry says so).
    let h = Host::new(HostConfig::default());
    assert_eq!(h.start("dab", Params::new(), InputKind::Iq, 1_536_000.0, 0.0, false), Err(HostError::BadInput("rate")));
}

#[test]
fn a_parameter_only_the_decoder_can_judge_is_refused_at_once() {
    let h = Host::new(HostConfig::default());
    let sid = h.start("dab", Params::new(), InputKind::Iq, 2_048_000.0, 0.0, false).unwrap();
    assert_eq!(h.configure(&sid, Params::new().with("service", Value::Text("not hex".into()))), Err(HostError::BadParams("bad_param")));
    assert_eq!(h.configure(&sid, Params::new().with("block", Value::Text("99Z".into()))), Err(HostError::BadParams("bad_param")));
    h.configure(&sid, Params::new().with("service", Value::Text("c221".into()))).unwrap();
    assert_eq!(h.status(&sid).unwrap().params.text("service"), Some("c221"));
}
