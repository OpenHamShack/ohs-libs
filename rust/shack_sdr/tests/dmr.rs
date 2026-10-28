//! DMR end to end: the test transmitter (dmr::tx) through the decoder, as a
//! Host would feed it. Every test reads what the decoder says - results,
//! meta, sound, the vectors a vocoder was handed - never its insides.
//!
//! mbelib is never ours: the test that needs it looks for the file named by
//! SHACK_TEST_MBELIB (a library the developer built, ~/build/mbelib) and
//! says it is skipping when there is none. Everything else uses a stand-in
//! vocoder that records the 49-bit vectors it is given, which is how the
//! FEC is held to the transmitter bit for bit.

use rustfft::num_complex::Complex32;
use shack_sdr::decoder::{CodecId, DecodeError, Decoder, Fields, Input, Output, Sink, Value};
use shack_sdr::dmr::tx::*;
use shack_sdr::dmr::vocoder::{Vocoder, FRAME_SAMPLES};
use shack_sdr::dmr::Dmr;
use std::sync::{Arc, Mutex};

/// The codec loader's choice is one per process: tests that set it or
/// rely on its absence take turns.
static CODEC: Mutex<()> = Mutex::new(());

type Heard = Arc<Mutex<Vec<(usize, [u8; 49])>>>;

/// A vocoder that keeps what it is given. The n-th one made answers every
/// frame with the level LEVELS[n], so a slot's sound can be told apart.
struct Recorder {
    id: usize,
    heard: Heard,
}

const LEVELS: [f32; 2] = [0.25, -0.5];

impl Vocoder for Recorder {
    fn decode(&mut self, bits: &[u8; 49], _: u32, _: u32) -> [f32; FRAME_SAMPLES] {
        self.heard.lock().unwrap().push((self.id, *bits));
        [LEVELS[self.id.min(1)]; FRAME_SAMPLES]
    }
}

fn recording() -> (Dmr, Heard) {
    let heard: Heard = Arc::default();
    let h = heard.clone();
    let made = Arc::new(Mutex::new(0usize));
    let d = Dmr::with_vocoder(Box::new(move || {
        let mut n = made.lock().unwrap();
        *n += 1;
        Some(Box::new(Recorder { id: *n - 1, heard: h.clone() }) as Box<dyn Vocoder>)
    }));
    (d, heard)
}

struct Run {
    out: Vec<(f64, Output)>,
    errors: Vec<DecodeError>,
}

impl Run {
    fn frames(&self, kind: &str) -> Vec<Fields> {
        self.out
            .iter()
            .filter_map(|(_, o)| match o {
                Output::Frame { kind: k, fields, .. } if *k == kind => Some(fields.clone()),
                _ => None,
            })
            .collect()
    }
    fn audio(&self) -> Vec<f32> {
        self.out
            .iter()
            .filter_map(|(_, o)| match o {
                Output::Audio { pcm, rate, channels } => {
                    assert_eq!((*rate, *channels), (8_000, 1));
                    Some(pcm.clone())
                }
                _ => None,
            })
            .flatten()
            .collect()
    }
    fn last_meta(&self) -> Fields {
        self.out
            .iter()
            .rev()
            .find_map(|(_, o)| match o {
                Output::Meta { fields } => Some(fields.clone()),
                _ => None,
            })
            .expect("a meta")
    }
}

fn get<'a>(f: &'a Fields, k: &str) -> &'a Value {
    &f.iter().find(|(n, _)| n == k).unwrap_or_else(|| panic!("no {k} in {f:?}")).1
}

fn num(f: &Fields, k: &str) -> Option<f64> {
    match get(f, k) {
        Value::Num(x) => Some(*x),
        Value::Null => None,
        v => panic!("{k} is {v:?}"),
    }
}

fn text(f: &Fields, k: &str) -> Option<String> {
    match get(f, k) {
        Value::Text(x) => Some(x.clone()),
        Value::Null => None,
        v => panic!("{k} is {v:?}"),
    }
}

fn flag(f: &Fields, k: &str) -> bool {
    matches!(get(f, k), Value::Bool(true))
}

fn slot_meta(meta: &Fields, slot: usize) -> Fields {
    match get(meta, "slots") {
        Value::List(l) => match &l[slot - 1] {
            Value::Map(m) => m.clone(),
            v => panic!("{v:?}"),
        },
        v => panic!("{v:?}"),
    }
}

/// Feed `iq` at `rate` in 100 ms blocks, then a second of quiet so calls
/// that end by timing out do.
fn run(d: &mut Dmr, iq: &[Complex32], rate: f64) -> Run {
    let mut sink = Sink::new();
    let mut errors = Vec::new();
    let quiet = vec![Complex32::new(0.0, 0.0); rate as usize];
    let block = (rate / 10.0) as usize;
    let mut index = 0u64;
    for b in iq.chunks(block).chain(quiet.chunks(block)) {
        let t0 = 1_800_000_000.0 + index as f64 / rate;
        if let Err(e) = d.feed(Input::Iq { samples: b, rate, t0, index }, &mut sink) {
            errors.push(e);
        }
        index += b.len() as u64;
    }
    Run { out: sink.take(), errors }
}

fn call(src: u32, dst: u32, superframes: usize) -> VoiceCall {
    VoiceCall::new(src, dst, superframes)
}

#[test]
fn both_slots_decode_at_every_snr_offset_and_rate() {
    // Two calls at once on a repeater, across the conditions a real channel
    // gives: weak, off frequency, and at the rates apps hand over.
    for &(snr, offset, rate) in &[
        (None, 0.0, 48_000.0),
        (Some(20.0), 1_500.0, 48_000.0),
        (Some(12.0), -1_500.0, 48_000.0),
        (Some(9.0), 0.0, 48_000.0),
        (Some(15.0), 700.0, 24_000.0),
        (Some(15.0), -300.0, 62_500.0),
    ] {
        let mut b = call(2_350_101, 9, 3);
        b.group = false;
        let case = DmrCase {
            slots: [vec![Item::Voice(call(2_351_234, 2350, 3))], vec![Item::Gap(2), Item::Voice(b)]],
            snr_db: snr,
            offset_hz: offset,
            rate,
            ..Default::default()
        };
        let sig = dmr_iq(&case);
        let (mut d, heard) = recording();
        let r = run(&mut d, &sig.iq, rate);
        let what = format!("snr {snr:?} offset {offset} rate {rate}");
        let starts = r.frames("call_start");
        let ends = r.frames("call_end");
        assert_eq!(starts.len(), 2, "{what}: {starts:?}");
        assert_eq!(ends.len(), 2, "{what}: {ends:?}");
        let one = ends.iter().find(|f| num(f, "slot") == Some(1.0)).expect(&what);
        let two = ends.iter().find(|f| num(f, "slot") == Some(2.0)).expect(&what);
        assert_eq!((num(one, "src"), num(one, "dst"), text(one, "callType").as_deref()), (Some(2_351_234.0), Some(2350.0), Some("group")), "{what}");
        assert_eq!((num(two, "src"), num(two, "dst"), text(two, "callType").as_deref()), (Some(2_350_101.0), Some(9.0), Some("private")), "{what}");
        // Three superframes and the header and terminator: 1.08 s of voice.
        let dur = num(one, "durationS").unwrap();
        assert!((1.1..1.3).contains(&dur), "{what}: {dur}");
        // Every vector came through in each slot's order: bit for bit on a
        // fair signal; at 9-12 dB C2 and C3 (unprotected, as in any AMBE+2
        // radio) take some errors, as they do on the air.
        let allowed = if snr.is_some_and(|s| s < 15.0) { sig.voice[0].len() / 8 } else { 0 };
        let h = heard.lock().unwrap();
        for (id, slot) in [(0usize, 0usize), (1, 1)] {
            let got: Vec<[u8; 49]> = h.iter().filter(|(i, _)| *i == id).map(|(_, b)| *b).collect();
            assert_eq!(got.len(), sig.voice[slot].len(), "{what}: slot {}", slot + 1);
            let wrong = got.iter().zip(&sig.voice[slot]).filter(|(a, b)| a != b).count();
            assert!(wrong <= allowed, "{what}: slot {} has {wrong} frames wrong", slot + 1);
        }
        let ber = num(one, "ber").unwrap();
        assert!(ber < 0.05, "{what}: ber {ber}");
        assert!(r.errors.is_empty(), "{what}: {:?}", r.errors);
    }
}

#[test]
fn direct_mode_names_its_slots_and_a_lone_radio_is_heard() {
    let case = DmrCase {
        mode: Mode::Direct,
        cc: 5,
        slots: [vec![Item::Gap(4), Item::Voice(call(11, 100, 2))], vec![Item::Voice(call(22, 200, 2))]],
        snr_db: Some(18.0),
        ..Default::default()
    };
    let (mut d, _) = recording();
    let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
    let ends = r.frames("call_end");
    let by_slot: Vec<(f64, f64, f64)> = ends.iter().map(|f| (num(f, "slot").unwrap(), num(f, "src").unwrap(), num(f, "cc").unwrap())).collect();
    assert!(by_slot.contains(&(1.0, 11.0, 5.0)) && by_slot.contains(&(2.0, 22.0, 5.0)), "{by_slot:?}");
    assert_eq!(text(&r.last_meta(), "mode").as_deref(), Some("direct"));

    let case = DmrCase { mode: Mode::Ms, cc: 3, slots: [vec![Item::Voice(call(7, 70, 2))], vec![]], snr_db: Some(18.0), ..Default::default() };
    let (mut d, _) = recording();
    let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
    let ends = r.frames("call_end");
    assert_eq!(ends.len(), 1, "{ends:?}");
    assert_eq!((num(&ends[0], "src"), num(&ends[0], "cc")), (Some(7.0), Some(3.0)));
    assert_eq!(text(&r.last_meta(), "mode").as_deref(), Some("ms"));
}

#[test]
fn a_call_heard_without_its_header_is_entered_late() {
    let mut c = call(2_351_999, 91, 3);
    c.no_header = true;
    c.no_terminator = true;
    let case = DmrCase { slots: [vec![Item::Voice(c)], vec![]], snr_db: Some(20.0), ..Default::default() };
    let (mut d, _) = recording();
    let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
    let s = r.frames("call_start");
    assert_eq!(s.len(), 1);
    assert!(flag(&s[0], "lateEntry"));
    assert_eq!(num(&s[0], "src"), None, "the IDs are not known yet");
    // The embedded LC names the call before it ends, and with no
    // terminator it ends by timing out.
    let e = r.frames("call_end");
    assert_eq!((num(&e[0], "src"), num(&e[0], "dst")), (Some(2_351_999.0), Some(91.0)));
    let m = slot_meta(&r.last_meta(), 1);
    assert_eq!((text(&m, "state").as_deref(), num(&m, "src")), (Some("idle"), Some(2_351_999.0)));
}

#[test]
fn talker_alias_is_put_back_together_from_the_superframes() {
    for alias in ["GM4BRI", "GM4BRI Brian Ayrshire", "Ünïcödé 名前"] {
        let mut c = call(2_351_234, 2350, 6);
        c.alias = Some(alias.into());
        let case = DmrCase { slots: [vec![], vec![Item::Voice(c)]], snr_db: Some(15.0), ..Default::default() };
        let (mut d, _) = recording();
        let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
        let a = r.frames("talker_alias");
        assert_eq!(a.len(), 1, "{alias}: {a:?}");
        assert_eq!((text(&a[0], "alias").as_deref(), num(&a[0], "slot"), num(&a[0], "src")), (Some(alias), Some(2.0), Some(2_351_234.0)));
        assert_eq!(text(&r.frames("call_end")[0], "alias").as_deref(), Some(alias));
        assert_eq!(text(&slot_meta(&r.last_meta(), 2), "alias").as_deref(), Some(alias));
    }
}

#[test]
fn an_encrypted_call_is_flagged_and_its_voice_never_decoded() {
    let mut c = call(1001, 2002, 2);
    c.encrypted = true;
    c.emergency = true;
    let case = DmrCase { slots: [vec![Item::Voice(c)], vec![]], snr_db: Some(20.0), ..Default::default() };
    let (mut d, heard) = recording();
    let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
    let s = &r.frames("call_start")[0];
    let e = &r.frames("call_end")[0];
    assert!(flag(s, "encrypted") && flag(e, "encrypted") && flag(e, "emergency"));
    assert!(flag(&slot_meta(&r.last_meta(), 1), "encrypted"));
    assert!(heard.lock().unwrap().is_empty(), "encrypted voice reached the vocoder");
    assert!(r.audio().iter().all(|&x| x == 0.0));
    // Nor is it a codec's absence: there is nothing to decode.
    assert!(r.errors.is_empty());
}

#[test]
fn short_messages_come_through_at_both_rates() {
    let sms = |text: &str, rate34| Item::Sms(Sms { src: 2_351_234, dst: 2_351_235, group: false, text: text.into(), rate34 });
    let case = DmrCase {
        slots: [vec![sms("Rate one half, a longer message to span blocks", false)], vec![Item::Gap(1), sms("Three quarters", true)]],
        snr_db: Some(14.0),
        ..Default::default()
    };
    let (mut d, _) = recording();
    let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
    let mut got: Vec<(f64, String, f64, f64)> = r
        .frames("sms")
        .iter()
        .map(|f| (num(f, "slot").unwrap(), text(f, "text").unwrap(), num(f, "src").unwrap(), num(f, "dstPort").unwrap()))
        .collect();
    got.sort_by(|a, b| a.0.total_cmp(&b.0));
    assert_eq!(
        got,
        vec![
            (1.0, "Rate one half, a longer message to span blocks".into(), 2_351_234.0, 4007.0),
            (2.0, "Three quarters".into(), 2_351_234.0, 4007.0)
        ]
    );
}

#[test]
fn without_mbelib_everything_but_the_sound_decodes() {
    // No vocoder factory and no mbelib path given: the real loader has
    // nothing, so the session needs the codec, and still says who called.
    let _g = CODEC.lock().unwrap_or_else(|e| e.into_inner());
    let case = DmrCase { slots: [vec![Item::Voice(call(5, 6, 2))], vec![]], ..Default::default() };
    let mut d = Dmr::new();
    let r = run(&mut d, &dmr_iq(&case).iq, 48_000.0);
    assert!(r.errors.contains(&DecodeError::NeedsCodec(CodecId::Mbelib)), "{:?}", r.errors);
    assert_eq!(num(&r.frames("call_end")[0], "src"), Some(5.0));
    assert!(r.audio().is_empty());
}

#[test]
fn the_slot_parameter_chooses_whose_voice_is_heard() {
    // Slot 1 talks first, so its vocoder is the first made (level 0.25),
    // slot 2's the second (-0.5). Both overlap for a while.
    let case = DmrCase {
        slots: [vec![Item::Voice(call(1, 10, 4))], vec![Item::Gap(10), Item::Voice(call(2, 20, 4))]],
        ..Default::default()
    };
    let sig = dmr_iq(&case);
    let levels = |which: &str| {
        let (mut d, _) = recording();
        d.configure(&shack_sdr::decoder::Params::new().with("slot", Value::Text(which.into()))).unwrap();
        let a = run(&mut d, &sig.iq, 48_000.0).audio();
        let mut seen: Vec<i32> = a.iter().map(|x| (x * 100.0).round() as i32).collect();
        seen.dedup();
        seen.sort();
        seen.dedup();
        seen
    };
    assert_eq!(levels("1"), vec![0, 25]);
    assert_eq!(levels("2"), vec![-50, 0]);
    assert_eq!(levels("both"), vec![-50, -25, 0, 25]);
    let (mut d, _) = recording();
    assert!(d.configure(&shack_sdr::decoder::Params::new().with("slot", Value::Text("3".into()))).is_err());
}

#[test]
fn audio_is_continuous_eight_khz_through_a_call() {
    let case = DmrCase { slots: [vec![Item::Voice(call(1, 10, 5))], vec![]], ..Default::default() };
    let sig = dmr_iq(&case);
    let (mut d, _) = recording();
    let r = run(&mut d, &sig.iq, 48_000.0);
    let a = r.audio();
    let secs = sig.iq.len() as f64 / 48_000.0 + 1.0;
    // All of the input's time, less the fixed delay, and no more.
    assert!(((a.len() as f64 / 8_000.0) - (secs - 0.3)).abs() < 0.15, "{} s of {secs}", a.len() as f64 / 8_000.0);
    // The call's 30 frames of voice are one unbroken run of 0.25.
    let run_len = a.split(|&x| x != 0.25).map(|r| r.len()).max().unwrap();
    assert_eq!(run_len, 5 * 18 * FRAME_SAMPLES);
}

#[test]
fn mbelib_when_present_turns_the_voice_into_sound() {
    let Some(path) = std::env::var_os("SHACK_TEST_MBELIB").map(std::path::PathBuf::from).filter(|p| p.exists()) else {
        eprintln!("skipping: SHACK_TEST_MBELIB names no mbelib");
        return;
    };
    let _g = CODEC.lock().unwrap_or_else(|e| e.into_inner());
    // The loader's own check passes it first, as the app's would.
    let v = shack_sdr::codecs::probe(CodecId::Mbelib, &path).expect("mbelib passes the probe");
    assert!(v.is_some());
    shack_sdr::codecs::use_path(CodecId::Mbelib, Some(path));
    let mut c = call(1, 2, 3);
    c.payload = Payload::Random(77);
    let case = DmrCase { slots: [vec![Item::Voice(c)], vec![]], snr_db: Some(25.0), ..Default::default() };
    let sig = dmr_iq(&case);
    let mut d = Dmr::new();
    let r = run(&mut d, &sig.iq, 48_000.0);
    shack_sdr::codecs::use_path(CodecId::Mbelib, None);
    assert!(r.errors.is_empty(), "{:?}", r.errors);
    // The decoder's sound is what mbelib makes of the vectors sent, decoded
    // by a fresh mbelib directly: frame by frame the same loudness (its
    // unvoiced phases come from rand(), so never the same samples).
    shack_sdr::codecs::use_path(CodecId::Mbelib, std::env::var_os("SHACK_TEST_MBELIB").map(Into::into));
    let mut m = shack_sdr::dmr::vocoder::Mbelib::open().expect("opens");
    shack_sdr::codecs::use_path(CodecId::Mbelib, None);
    let want: Vec<f32> = sig.voice[0].iter().flat_map(|f| m.decode(f, 0, 0)).collect();
    let a = r.audio();
    let mut energy = vec![0f64; a.len() + 1];
    for (i, x) in a.iter().enumerate() {
        energy[i + 1] = energy[i] + (*x as f64).powi(2);
    }
    let level = |e: f64| (e / FRAME_SAMPLES as f64 + 1e-12).log10();
    let wl: Vec<f64> = want.chunks(FRAME_SAMPLES).map(|c| level(c.iter().map(|x| (*x as f64).powi(2)).sum())).collect();
    let corr = |lag: usize| {
        let gl: Vec<f64> = (0..wl.len()).map(|k| level(energy[lag + (k + 1) * FRAME_SAMPLES] - energy[lag + k * FRAME_SAMPLES])).collect();
        let (mw, mg) = (wl.iter().sum::<f64>() / wl.len() as f64, gl.iter().sum::<f64>() / gl.len() as f64);
        let c: f64 = wl.iter().zip(&gl).map(|(x, y)| (x - mw) * (y - mg)).sum();
        let n = (wl.iter().map(|x| (x - mw).powi(2)).sum::<f64>() * gl.iter().map(|y| (y - mg).powi(2)).sum::<f64>()).sqrt();
        c / n.max(1e-12)
    };
    let best = (0..a.len() - want.len()).map(corr).fold(f64::MIN, f64::max);
    assert!(best > 0.95, "the envelopes agree only to {best}");
    let rms = (want.iter().map(|x| x * x).sum::<f32>() / want.len() as f32).sqrt();
    assert!(rms > 1e-3, "rms {rms}");
}
