//! DAB's data, end to end through the transmitter model's IQ (dab::tx) and
//! the `dab` decoder as the host drives it: SlideShow from old DAB's and
//! DAB+'s X-PAD, station logos and the programme guide from an SPI
//! carousel on a packet-mode data service, DL Plus, and the FIC's service
//! information in the feed (genre, language, country, clock).

use shack_sdr::dab::codes::mjd_utc;
use shack_sdr::dab::mot::{CT_IMAGE, IMAGE_JFIF, IMAGE_PNG};
use shack_sdr::dab::tx::{
    content_name, dab_iq, fig, mot_directory_mode, mot_header, mot_header_mode, mot_param, spi_bearer, spi_pi_file,
    spi_si_file, spi_time, test_jpeg, test_png, Channel, TxEnsemble,
};
use shack_sdr::dab::Dab;
use shack_sdr::decoder::{DecodeError, Decoder, Fields, Input, Output, Params, Sink, Value};
use rustfft::num_complex::Complex32;

fn field<'a>(f: &'a [(String, Value)], k: &str) -> &'a Value {
    &f.iter().find(|(n, _)| n == k).unwrap_or_else(|| panic!("no field {k}")).1
}

fn get<'a>(v: &'a Value, k: &str) -> &'a Value {
    match v {
        Value::Map(m) => field(m, k),
        _ => panic!("not a map: {v:?}"),
    }
}

fn text(s: &str) -> Value {
    Value::Text(s.into())
}

/// A service's map in a meta.
fn service<'a>(m: &'a [(String, Value)], sid: &str) -> &'a Value {
    match field(m, "services") {
        Value::List(l) => l.iter().find(|s| get(s, "sid") == &text(sid)).unwrap_or_else(|| panic!("no {sid}")),
        v => panic!("services: {v:?}"),
    }
}

#[derive(Default)]
struct Run {
    metas: Vec<Fields>,
    /// (kind, fields, bytes) of each frame output.
    frames: Vec<(&'static str, Fields, Option<Vec<u8>>)>,
}

impl Run {
    fn of(&self, kind: &str) -> Vec<&(&'static str, Fields, Option<Vec<u8>>)> {
        self.frames.iter().filter(|f| f.0 == kind).collect()
    }
}

/// Feed `iq` in 100 ms blocks from sample time `t0`, as the host would.
fn run(d: &mut Dab, iq: &[Complex32], t0: f64) -> Run {
    let mut r = Run::default();
    let rate = 2_048_000.0;
    let block = 204_800;
    for (i, chunk) in iq.chunks(block).enumerate() {
        let mut sink = Sink::new();
        match d.feed(Input::Iq { samples: chunk, rate, t0: t0 + (i * block) as f64 / rate, index: (i * block) as u64 }, &mut sink) {
            Ok(()) | Err(DecodeError::NeedsCodec(_)) => {}
            Err(e) => panic!("{e:?}"),
        }
        for (_, o) in sink.take() {
            match o {
                Output::Meta { fields } => r.metas.push(fields),
                Output::Frame { kind, fields, raw } => r.frames.push((kind, fields, raw)),
                _ => {}
            }
        }
    }
    r
}

fn choose(d: &mut Dab, sid: &str) {
    d.configure(&Params::new().with("service", text(sid))).unwrap();
}

/// FIG 0/13: SlideShow in this service's audio X-PAD (app type 12).
fn slideshow_in_xpad(sid: u16) -> Vec<u8> {
    let s = sid.to_be_bytes();
    fig(0, vec![0x0d, s[0], s[1], 0x01, 0x00, 0x42, 0x0c, 0x3c])
}

fn slide_groups(tid: u16, name: &str, body: &[u8]) -> Vec<Vec<u8>> {
    let h = mot_header(
        body.len(),
        CT_IMAGE,
        IMAGE_JFIF,
        &[content_name(name), mot_param(0x05, &[0, 0, 0, 0]), mot_param(0x26, b"Now playing"), mot_param(0x27, b"https://example.org/np")],
    );
    mot_header_mode(tid, &h, body, 700)
}

#[test]
fn slides_from_old_dab_and_dab_plus_x_pad_come_out_whole_with_their_details() {
    let mut ens = TxEnsemble::standard();
    let mp2_slide = test_jpeg(320, 240, 1200);
    let plus_slide = test_jpeg(640, 480, 2000);
    ens.services[0].xpad_groups = slide_groups(0x0101, "mp2-slide.jpg", &mp2_slide);
    ens.services[1].xpad_groups = slide_groups(0x0202, "plus-slide.jpg", &plus_slide);
    ens.figs = vec![slideshow_in_xpad(0xc221), slideshow_in_xpad(0xc3a7)];
    let iq = dab_iq(&ens, 50, &Channel { snr_db: Some(15.0), ..Default::default() });
    for (sid, body, name) in [("C221", &mp2_slide, "mp2-slide.jpg"), ("C3A7", &plus_slide, "plus-slide.jpg")] {
        let mut d = Dab::new(); // no faad2: DAB+'s PAD needs none
        choose(&mut d, sid);
        let r = run(&mut d, &iq, 1000.0);
        let slides = r.of("slide");
        assert!(!slides.is_empty(), "{sid}: no slide");
        let (_, f, raw) = slides[0];
        assert_eq!(raw.as_deref(), Some(body.as_slice()), "{sid}: the picture's bytes");
        assert_eq!(field(f, "sid"), &text(sid));
        assert_eq!(field(f, "contentType"), &text("image/jpeg"));
        assert_eq!(field(f, "name"), &text(name));
        assert_eq!(field(f, "categoryTitle"), &text("Now playing"));
        assert_eq!(field(f, "clickThroughUrl"), &text("https://example.org/np"));
        assert_eq!(field(f, "size"), &Value::Num(body.len() as f64));
        // The carousel repeats it; it is handed over once.
        assert_eq!(slides.len(), 1, "{sid}: one slide, not its repeats");
        let m = r.metas.last().unwrap();
        assert_eq!(get(field(m, "slide"), "name"), &text(name));
        assert_eq!(get(service(m, sid), "hasSlideshow"), &Value::Bool(true));
        assert!(matches!(get(service(m, sid), "userApps"), Value::List(l) if l.contains(&text("slideshow"))));
    }
}

/// The SPI carousel: C221's logos, an SI file naming them, and a PI file.
fn spi_carousel(t0_mjd: u32) -> Vec<Vec<u8>> {
    let png = |w, h| test_png(w, h, 150);
    let mut objs = Vec::new();
    for (i, (name, w, h)) in [("c221_32.png", 32, 32), ("c221_128.png", 128, 128), ("c221_320.png", 320, 240)].iter().enumerate() {
        let body = png(*w, *h);
        objs.push((10 + i as u16, mot_header(body.len(), CT_IMAGE, IMAGE_PNG, &[content_name(name)]), body));
    }
    let si = spi_si_file(0xe1, 0xc1ce, &[(0xc221, "Shack MP2 Tone", vec![("c221_32.png", 32, 32), ("c221_128.png", 128, 128), ("c221_320.png", 320, 240)])]);
    objs.push((20, mot_header(si.len(), 7, 0, &[content_name("SI.dat")]), si));
    let pi = spi_pi_file(
        0xe1,
        0xc1ce,
        0xc221,
        &[("Early Tones", spi_time(t0_mjd, 0, 0, 0), 3600, Some("Sine waves before dawn")), ("Later Tones", spi_time(t0_mjd, 1, 0, 0), 3600, None)],
    );
    objs.push((21, mot_header(pi.len(), 7, 1, &[content_name("C221_PI.dat"), mot_param(0x27, &spi_bearer(0xe1, 0xc1ce, 0xc221))]), pi));
    mot_directory_mode(1, &objs, 256)
}

#[test]
fn logos_and_the_programme_guide_come_from_the_spi_data_service_while_a_service_plays() {
    // Sample time: MJD 40587 (1970-01-01) 00:16:40, inside "Early Tones".
    let mut ens = TxEnsemble::with_data(0x007, spi_carousel(40_587));
    // Genre, language, country and the clock, for the feed.
    let mut clock = vec![0x0a];
    clock.extend((40_587u32 << 14 | 1 << 11).to_be_bytes());
    clock.extend([0, 0]);
    ens.figs = vec![
        fig(0, vec![0x11, 0xc2, 0x21, 0x00, 0x0a]),
        fig(0, vec![0x05, 0x01, 0x09]),
        fig(0, vec![0x09, 0x02, 0xe1, 0x01]),
        fig(0, clock),
    ];
    let iq = dab_iq(&ens, 50, &Channel { snr_db: Some(15.0), ..Default::default() });
    let t0 = mjd_utc(40_587, 0, 16, 40);
    // Listing only, nothing playing: SPI is not decoded.
    let mut d = Dab::new();
    let r = run(&mut d, &iq[..iq.len() / 2], t0);
    assert!(r.of("logo").is_empty());
    // Playing C221: its logos and its schedule.
    let mut d = Dab::new();
    choose(&mut d, "C221");
    let r = run(&mut d, &iq, t0);
    let logos: Vec<(Value, Value, Value)> =
        r.of("logo").iter().map(|(_, f, _)| (field(f, "sid").clone(), field(f, "variant").clone(), field(f, "width").clone())).collect();
    assert_eq!(
        logos,
        vec![(text("C221"), text("square"), Value::Num(128.0)), (text("C221"), text("wide"), Value::Num(320.0))],
    );
    let (_, f, raw) = r.of("logo")[0];
    assert_eq!(raw.as_ref().map(|b| b.len()), Some(test_png(128, 128, 150).len()));
    assert_eq!(field(f, "contentType"), &text("image/png"));
    let sched = r.of("schedule");
    assert!(!sched.is_empty());
    assert!(matches!(field(&sched[0].1, "programmes"), Value::List(l) if l.len() == 2));
    let m = r.metas.last().unwrap();
    let s = service(m, "C221");
    assert_eq!(get(get(s, "now"), "name"), &text("Early Tones"));
    assert_eq!(get(get(s, "now"), "description"), &text("Sine waves before dawn"));
    assert_eq!(get(get(s, "next"), "start"), &text("1970-01-01T01:00:00Z"));
    assert_eq!(get(get(s, "pty"), "id"), &text("pop_music"));
    assert_eq!(get(s, "language"), &text("en"));
    let e = field(m, "ensemble");
    assert_eq!((get(e, "country"), get(e, "ltoMinutes")), (&text("GB"), &Value::Num(60.0)));
    assert_eq!(get(e, "utc"), &text("1970-01-01T00:00:00Z"));
    assert_eq!(get(field(m, "spi"), "sid"), &text("E1C00001"));
    // The SPI sub-channel's decoding is counted (--nocapture shows it).
    let c = |k: &str| d.status().counters.iter().find(|c| c.0 == k).map(|c| c.1).unwrap();
    println!("SPI: {} ms of thread time over {:.1} s of signal", c("spiCpuMs"), iq.len() as f64 / 2_048_000.0);
    assert_eq!(c("logos"), 2);
}

#[test]
fn dl_plus_titles_reach_the_feed_with_the_label() {
    let mut ens = TxEnsemble::standard();
    ens.services[1].dls = vec![(0, "Blue Monday - New Order".into())];
    ens.services[1].dl_plus = vec![(1, 0, 11), (4, 14, 9)];
    let iq = dab_iq(&ens, 24, &Channel { snr_db: Some(15.0), ..Default::default() });
    let mut d = Dab::new();
    choose(&mut d, "C3A7");
    let r = run(&mut d, &iq, 1000.0);
    let m = r.metas.last().unwrap();
    assert_eq!(field(m, "dls"), &text("Blue Monday - New Order"));
    let dl = field(m, "dlPlus");
    assert_eq!((get(dl, "itemTitle"), get(dl, "itemArtist")), (&text("Blue Monday"), &text("New Order")));
    assert_eq!(get(dl, "itemRunning"), &Value::Bool(true));
}

/// A recording: SHACK_DAB_IQ (SigMF ci16_le, centred on the block) at
/// SHACK_DAB_RATE, the first `secs` seconds of it.
fn recording(secs: f64) -> (Vec<Complex32>, f64) {
    let home = std::env::var("HOME").unwrap_or_default();
    let path = std::env::var("SHACK_DAB_IQ").unwrap_or(format!("{home}/build/dab11b/11b.sigmf-data"));
    let rate: f64 = std::env::var("SHACK_DAB_RATE").ok().and_then(|r| r.parse().ok()).unwrap_or(2_400_000.0);
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("{path}: {e} (set SHACK_DAB_IQ)"));
    let n = ((secs * rate) as usize * 4).min(bytes.len() / 4 * 4);
    let iq = bytes[..n]
        .chunks_exact(4)
        .map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0, i16::from_le_bytes([c[2], c[3]]) as f32 / 32768.0))
        .collect();
    (iq, rate)
}

/// Block 11B (Ayr), recorded off air on 23/10/2026 by Radio 0.1.22 through
/// an RSPdx at 2.4 MS/s: 62 s, 600 MB, so kept out of the repository (on
/// shack in ~/build/dab11b). What it carries was inventoried with figdump
/// (tools/dab-figdump): FIG 0/17 (Pop and Varied), 0/9 (E1, +1 h), 0/10,
/// sixteen linkage sets, 0/21 and 0/24 for ensemble C182 on 11C, and DL
/// Plus on one service. Run with --ignored.
#[test]
#[ignore]
fn the_service_information_of_a_real_ensemble_decodes() {
    let (iq, rate) = recording(30.0);
    let mut d = Dab::new();
    choose(&mut d, "C435"); // BBC Scotland: an MP2 service, so no faad2 is needed
    let mut metas = Vec::new();
    let block = (rate / 10.0) as usize;
    let t0 = 1_792_796_901.0; // 2026-10-23T23:08:21Z, the recording's start
    for (i, chunk) in iq.chunks(block).enumerate() {
        let mut sink = Sink::new();
        let _ = d.feed(Input::Iq { samples: chunk, rate, t0: t0 + (i * block) as f64 / rate, index: (i * block) as u64 }, &mut sink);
        metas.extend(sink.take().into_iter().filter_map(|(_, o)| if let Output::Meta { fields } = o { Some(fields) } else { None }));
    }
    let m = metas.last().expect("a meta");
    let e = field(m, "ensemble");
    assert_eq!(get(e, "id"), &text("C19B"));
    assert_eq!((get(e, "ecc"), get(e, "country"), get(e, "ltoMinutes")), (&text("E1"), &text("GB"), &Value::Num(60.0)));
    assert!(matches!(get(e, "utc"), Value::Text(t) if t.starts_with("2026-10-23T23:")), "{:?}", get(e, "utc"));
    assert_eq!(get(get(service(m, "CDF3"), "pty"), "id"), &text("pop_music"));
    assert_eq!(get(get(service(m, "C435"), "pty"), "id"), &text("varied"));
    // A service carried in ensemble C182 too (FIG 0/24), which FIG 0/21 places on 11C.
    let mut also = Vec::new();
    if let Value::List(l) = field(m, "services") {
        for s in l {
            if let Value::List(ens) = get(get(s, "alsoOn"), "ensembles") {
                also.extend(ens.iter().map(|x| (get(x, "eid").clone(), get(x, "block").clone())));
            }
        }
    }
    println!("also on: {also:?}");
    let links: usize = d.ensemble().links.len();
    println!("linkage sets: {links}; frequency info: {:?}", d.ensemble().freqs);
    assert!(links >= 3, "linkage sets heard: {links}");
    assert!(d.ensemble().freqs.values().any(|f| f.id == 0xc182 && f.khz == vec![220_352]), "C182 on 11C (220.352 MHz)");
    assert_eq!(dls_texts(&metas).is_empty(), false, "C435's programme text");
}

fn dls_texts(metas: &[Fields]) -> Vec<String> {
    metas.iter().filter_map(|m| if let Value::Text(t) = field(m, "dls") { Some(t.clone()) } else { None }).collect()
}

/// 11B's DL Plus: only STV Radio sends it (each MP2 service is played
/// for 20 s; run with --ignored --nocapture to see what each gave).
#[test]
#[ignore]
fn dl_plus_from_a_real_ensemble() {
    let (iq, rate) = recording(20.0);
    let block = (rate / 10.0) as usize;
    let mut found = Vec::new();
    for sid in ["C1CA", "C24A", "C336", "C435", "C756", "CCB1"] {
        let mut d = Dab::new();
        choose(&mut d, sid);
        let mut last = Value::Null;
        for (i, chunk) in iq.chunks(block).enumerate() {
            let mut sink = Sink::new();
            let _ = d.feed(Input::Iq { samples: chunk, rate, t0: (i * block) as f64 / rate, index: (i * block) as u64 }, &mut sink);
            for (_, o) in sink.take() {
                if let Output::Meta { fields } = o {
                    if field(&fields, "dlPlus") != &Value::Null {
                        last = field(&fields, "dlPlus").clone();
                    }
                }
            }
        }
        println!("{sid}: {last:?}");
        if last != Value::Null {
            found.push((sid, get(&last, "itemTitle").clone(), get(&last, "itemArtist").clone()));
        }
    }
    // STV Radio, at 23:08 on 23/10/2026.
    assert_eq!(found, vec![("C756", text("Crush"), text("Jennifer Paige"))]);
}

#[test]
fn the_showcase_ensemble_shows_everything_within_an_apps_loop() {
    // An app's simulator replays 24 frames (2.3 s) in a loop: within two
    // passes every slide, logo and schedule must be out.
    let mjd = 61_337;
    let one = dab_iq(&TxEnsemble::showcase(mjd), 24, &Channel { snr_db: None, lead_s: 0.0, ..Default::default() });
    let iq: Vec<Complex32> = one.iter().chain(one.iter()).copied().collect();
    for sid in ["C221", "C3A7"] {
        let mut d = Dab::new();
        choose(&mut d, sid);
        let r = run(&mut d, &iq, mjd_utc(mjd, 12, 0, 0));
        assert_eq!(r.of("slide").len(), 1, "{sid}: a slide");
        let slide = r.of("slide")[0].2.clone().unwrap();
        assert!(slide.starts_with(b"\x89PNG"), "a real PNG");
        let logos: Vec<_> = r.of("logo").iter().map(|(_, f, _)| (field(f, "sid").clone(), field(f, "variant").clone())).collect();
        assert_eq!(logos.len(), 4, "{sid}: both logos of both services: {logos:?}");
        let m = r.metas.last().unwrap();
        assert_eq!(get(get(service(m, sid), "now"), "name"), &text(&format!("{} Afternoon", if sid == "C221" { "Shack MP2 Tone" } else { "Shack DAB+ Data" })));
        assert_ne!(get(service(m, sid), "pty"), &Value::Null);
        // The whole carousel came round once: SPI rests, its cost capped.
        assert_eq!(get(field(m, "spi"), "state"), &text("resting"));
    }
}

/// One of the 23/10/2026 captures of the local blocks (ohs-box's RSPdx,
/// 2.4 MS/s, 62 s each, 600 MB, never committed), in SHACK_DAB_DIR (default
/// ~/build/dab11b): the first `secs` seconds.
fn capture(name: &str, secs: f64) -> Vec<Complex32> {
    let dir = std::env::var("SHACK_DAB_DIR").unwrap_or(format!("{}/build/dab11b", std::env::var("HOME").unwrap_or_default()));
    let path = format!("{dir}/{name}.sigmf-data");
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("{path}: {e} (set SHACK_DAB_DIR)"));
    let n = ((secs * 2_400_000.0) as usize * 4).min(bytes.len() / 4 * 4);
    bytes[..n]
        .chunks_exact(4)
        .map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0, i16::from_le_bytes([c[2], c[3]]) as f32 / 32768.0))
        .collect()
}

/// `sid` played from a capture, in 100 ms blocks at 2.4 MS/s.
fn play_capture(iq: &[Complex32], sid: &str, t0: f64) -> (Dab, Run) {
    let mut d = Dab::new();
    choose(&mut d, sid);
    let rate = 2_400_000.0;
    let block = 240_000;
    let mut r = Run::default();
    for (i, chunk) in iq.chunks(block).enumerate() {
        let mut sink = Sink::new();
        let _ = d.feed(Input::Iq { samples: chunk, rate, t0: t0 + (i * block) as f64 / rate, index: (i * block) as u64 }, &mut sink);
        for (_, o) in sink.take() {
            match o {
                Output::Meta { fields } => r.metas.push(fields),
                Output::Frame { kind, fields, raw } => r.frames.push((kind, fields, raw)),
                _ => {}
            }
        }
    }
    (d, r)
}

/// Write the pictures a run gave into SHACK_DAB_SAVE, when it is set, for a
/// person to look at: `<prefix>-<sid>-<kind>-<n>.<jpg|png>`.
fn save_pictures(r: &Run, prefix: &str) {
    let Ok(dir) = std::env::var("SHACK_DAB_SAVE") else { return };
    for (n, (kind, f, raw)) in r.frames.iter().enumerate() {
        let (Some(raw), Value::Text(sid)) = (raw, field(f, "sid")) else { continue };
        let ext = if raw.starts_with(&[0xff, 0xd8]) { "jpg" } else { "png" };
        std::fs::write(format!("{dir}/{prefix}-{sid}-{kind}-{n}.{ext}"), raw).unwrap();
    }
}

fn counter(d: &Dab, k: &str) -> u64 {
    d.status().counters.iter().find(|c| c.0 == k).map(|c| c.1).unwrap()
}

/// Block 12B, BBC National DAB: its SPI service ("BBC Guide", E1C79E5E) is
/// packet mode on a 16 kbit/s sub-channel with the packet FEC (FIG 0/14),
/// the only SPI heard locally. Playing Radio 4 (C224, MP2) for the whole
/// capture: logos and programme guides come out of it. Run with --ignored
/// --nocapture to see what came.
#[test]
#[ignore]
fn spi_logos_and_the_guide_from_bbc_national_on_12b() {
    let iq = capture("20261023-234145Z-225.648MHz", 62.0);
    // Recorded from 2026-10-23T23:41:45Z.
    let (d, r) = play_capture(&iq, "C224", mjd_utc(61_336, 23, 41, 45));
    save_pictures(&r, "12B");
    let logos: Vec<String> = r.of("logo").iter().map(|(_, f, raw)| {
        format!("{:?} {:?} {:?}x{:?} {} bytes", field(f, "sid"), field(f, "variant"), field(f, "width"), field(f, "height"), raw.as_ref().map(|b| b.len()).unwrap_or(0))
    }).collect();
    println!("logos: {logos:#?}");
    println!("schedules: {}", r.of("schedule").len());
    println!("counters: {:?}", d.status().counters);
    let m = r.metas.last().unwrap();
    println!("spi: {:?}", field(m, "spi"));
    println!("C224 now: {:?}", get(service(m, "C224"), "now"));
    assert_eq!(get(field(m, "spi"), "sid"), &text("E1C79E5E"));
    assert!(counter(&d, "packetErrors") < 50, "packet CRC errors: {}", counter(&d, "packetErrors"));
    assert!(counter(&d, "motObjects") > 0, "no MOT object in 62 s of SPI");
    assert!(!r.of("logo").is_empty(), "no logo from BBC National's SPI");
    // Radio 1's square and wide logos, the first pictures its carousel sends.
    let c221: Vec<(Value, Value)> =
        r.of("logo").iter().filter(|(_, f, _)| field(f, "sid") == &text("C221")).map(|(_, f, _)| (field(f, "variant").clone(), field(f, "width").clone())).collect();
    assert!(c221.contains(&(text("square"), Value::Num(128.0))) && c221.contains(&(text("wide"), Value::Num(320.0))), "{c221:?}");
    // The programme guide: schedules out, and what Radio 4 has on now.
    assert!(r.of("schedule").len() >= 10, "schedules: {}", r.of("schedule").len());
    let now = get(service(m, "C224"), "now");
    assert_ne!(now, &Value::Null);
    assert_ne!(get(service(m, "C224"), "next"), &Value::Null);
}

/// Blocks 9A and 9B: SlideShow on every service (FIG 0/13), all DAB+, whose
/// PAD needs no faad2. Every slide that completes must be a real picture
/// of its declared type. Run with --ignored --nocapture to see them.
#[test]
#[ignore]
fn slides_from_the_dab_plus_services_on_9a_and_9b() {
    for (name, sids) in [
        ("20261023-233340Z-202.928MHz", &["CCF1", "CDC2"][..]),
        ("20261023-233449Z-204.640MHz", &["C0C8", "CD89"][..]),
    ] {
        let iq = capture(name, 62.0);
        for sid in sids {
            let (d, r) = play_capture(&iq, sid, mjd_utc(61_336, 23, 34, 0));
            save_pictures(&r, &name[..15]);
            let slides: Vec<String> = r.of("slide").iter().map(|(_, f, raw)| {
                format!("{:?} {:?} {:?}x{:?} {} bytes {:?}", field(f, "name"), field(f, "contentType"), field(f, "width"), field(f, "height"), raw.as_ref().map(|b| b.len()).unwrap_or(0), field(f, "categoryTitle"))
            }).collect();
            let m = r.metas.last().unwrap();
            println!("{name} {sid}: {} slides {slides:#?}\n   dls {:?}\n   dl+ {:?}\n   mot {} groups, {} objects, {} errors", slides.len(), field(m, "dls"), field(m, "dlPlus"), counter(&d, "motGroups"), counter(&d, "motObjects"), counter(&d, "motErrors"));
            // SlideShow's share of a DAB+ X-PAD here is about 100 bytes a
            // second, so a picture can take longer than the capture: what
            // must hold is that its data groups arrive whole and correct.
            assert!(counter(&d, "motGroups") > 0, "{name} {sid}: no MOT data group in 62 s");
            assert_eq!(counter(&d, "motErrors"), 0, "{name} {sid}: data groups failing their CRC");
            assert!(field(m, "dls") != &Value::Null, "{name} {sid}: no programme text");
            for (_, f, raw) in r.of("slide") {
                let raw = raw.as_ref().unwrap();
                let jpeg = raw.starts_with(&[0xff, 0xd8]);
                let png = raw.starts_with(b"\x89PNG");
                assert!(jpeg || png, "a real picture");
                assert_eq!(field(f, "contentType"), &text(if jpeg { "image/jpeg" } else { "image/png" }));
            }
        }
    }
}
