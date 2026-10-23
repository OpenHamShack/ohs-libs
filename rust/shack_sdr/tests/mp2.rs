//! Our MPEG-1/2 Layer II decoder against our own encoder (mp2::encode):
//! tones through stereo, joint stereo and 24 kHz mono at DAB's rates come
//! back at the right level, frequency and channel, a corrupted frame is
//! refused by its CRC, and the filter bank's state carries across frames.

use shack_sdr::mp2::encode::Encoder;
use shack_sdr::mp2::{Decoder, Header, Mp2Error};

/// `secs` of a stereo pair of tones (left fl, right fr), interleaved.
fn tones(rate: u32, fl: f64, fr: f64, amp: f64, frames: usize) -> Vec<f32> {
    (0..frames * 1152)
        .flat_map(|i| {
            let t = i as f64 / rate as f64;
            [(amp * (2.0 * std::f64::consts::PI * fl * t).sin()) as f32, (amp * (2.0 * std::f64::consts::PI * fr * t).sin()) as f32]
        })
        .collect()
}

/// Encode then decode; the decoded PCM per channel.
fn round_trip(bitrate: u16, rate: u32, mode: u8, ext: u8, pcm: &[f32], nch_in: usize) -> (Vec<Vec<f32>>, Header) {
    let mut enc = Encoder::new(bitrate, rate, mode, ext);
    let nch = if mode == 3 { 1 } else { 2 };
    let mut dec = Decoder::new();
    let mut out = vec![Vec::new(); nch];
    let frames = pcm.len() / (1152 * nch_in);
    for f in 0..frames {
        let block: Vec<f32> = (0..1152)
            .flat_map(|i| (0..nch).map(move |c| (i, c)))
            .map(|(i, c)| pcm[(f * 1152 + i) * nch_in + c.min(nch_in - 1)])
            .collect();
        let bytes = enc.frame(&block);
        assert_eq!(bytes.len(), enc.header.length());
        let fr = dec.decode(&bytes).expect("our own frame decodes");
        assert_eq!((fr.rate, fr.channels as usize, fr.pcm.len()), (rate, nch, 1152 * nch));
        for (i, v) in fr.pcm.iter().enumerate() {
            out[i % nch].push(*v);
        }
    }
    (out, enc.header)
}

/// Level of a tone at f in x (after its first `skip` samples).
fn tone_level(x: &[f32], f: f64, rate: u32, skip: usize) -> f64 {
    let y = &x[skip..];
    let (mut c, mut s) = (0.0, 0.0);
    for (i, v) in y.iter().enumerate() {
        let p = 2.0 * std::f64::consts::PI * f * i as f64 / rate as f64;
        c += *v as f64 * p.cos();
        s += *v as f64 * p.sin();
    }
    2.0 * (c * c + s * s).sqrt() / y.len() as f64
}

/// Signal-to-everything-else of a single tone: its power over the rest.
fn tone_snr_db(x: &[f32], f: f64, rate: u32, skip: usize) -> f64 {
    let a = tone_level(x, f, rate, skip);
    let total: f64 = x[skip..].iter().map(|v| (*v as f64).powi(2)).sum::<f64>() / (x.len() - skip) as f64;
    let sig = a * a / 2.0;
    10.0 * (sig / (total - sig).max(1e-20)).log10()
}

#[test]
fn stereo_at_dabs_128_kbits_keeps_each_channels_tone() {
    let pcm = tones(48_000, 1000.0, 2500.0, 0.5, 20);
    let (out, _) = round_trip(128, 48_000, 0, 0, &pcm, 2);
    assert!((tone_level(&out[0], 1000.0, 48_000, 4000) - 0.5).abs() < 0.01);
    assert!((tone_level(&out[1], 2500.0, 48_000, 4000) - 0.5).abs() < 0.01);
    // Nothing of the other channel's tone leaks across.
    assert!(tone_level(&out[0], 2500.0, 48_000, 4000) < 0.002);
    let snr = tone_snr_db(&out[0], 1000.0, 48_000, 4000);
    assert!(snr > 40.0, "{snr}");
}

#[test]
fn joint_stereo_above_the_bound_carries_both_channels() {
    // Bound 8 (mode_ext 1): a 12 kHz tone sits in subband 16, above it.
    let pcm = tones(48_000, 12_000.0, 12_000.0, 0.3, 20);
    let (out, h) = round_trip(160, 48_000, 1, 1, &pcm, 2);
    assert_eq!(h.mode, 1);
    for ch in &out {
        assert!((tone_level(ch, 12_000.0, 48_000, 4000) - 0.3).abs() < 0.01);
    }
}

#[test]
fn mono_at_24_khz_as_dab_sends_low_rate_services() {
    let pcm = tones(24_000, 440.0, 440.0, 0.7, 12);
    let (out, h) = round_trip(64, 24_000, 3, 0, &pcm, 2);
    assert!(h.lsf && h.length() == 384);
    assert!((tone_level(&out[0], 440.0, 24_000, 3000) - 0.7).abs() < 0.01);
    assert!(tone_snr_db(&out[0], 440.0, 24_000, 3000) > 35.0);
}

#[test]
fn a_corrupted_frame_is_refused_by_its_crc() {
    let mut enc = Encoder::new(128, 48_000, 0, 0);
    let mut f = enc.frame(&tones(48_000, 1000.0, 1000.0, 0.5, 1));
    let mut dec = Decoder::new();
    assert!(dec.decode(&f).is_ok());
    f[7] ^= 0x10; // inside the allocation
    assert_eq!(dec.decode(&f), Err(Mp2Error::Crc));
    assert_eq!(dec.crc_errors, 1);
    assert_eq!(dec.decode(&f[..100]), Err(Mp2Error::Short));
}

/// Frames from an independent encoder, decoded by us and by FFmpeg's own
/// Layer II decoder: the two outputs must agree. Uses this host's ffmpeg
/// (its mp2 encoder, and twolame's through it with joint stereo and CRCs),
/// run at test time; skipped, saying so, where there is no ffmpeg. Nothing
/// it makes is kept.
#[test]
fn streams_from_other_encoders_decode_as_ffmpeg_decodes_them() {
    use std::process::Command;
    if Command::new("ffmpeg").arg("-version").output().is_err() {
        eprintln!("SKIPPED: no ffmpeg on this host to make independent MP2 streams");
        return;
    }
    let dir = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join("mp2-ffmpeg");
    std::fs::create_dir_all(&dir).unwrap();
    const TWO: &str = "sine=frequency=997:duration=2,aeval=val(0)|0.6*sin(2*PI*3001*t):c=stereo";
    let cases: [(&str, &str, &[&str]); 3] = [
        ("ffmpeg-48k-stereo", TWO, &["-c:a", "mp2", "-b:a", "192k", "-ar", "48000"]),
        ("twolame-48k-stereo-crc", TWO, &["-c:a", "libtwolame", "-b:a", "128k", "-ar", "48000", "-mode", "stereo", "-error_protection", "1"]),
        ("ffmpeg-24k-mono", TWO, &["-c:a", "mp2", "-b:a", "64k", "-ar", "24000", "-ac", "1"]),
    ];
    let mut ran = 0;
    // twolame picks joint stereo frame by frame and seldom, so joint stereo
    // is checked on our own encoder's frames: whoever wrote the bits, FFmpeg
    // decides what they mean.
    let joint = dir.join("ours-48k-joint-crc.mp2");
    let mut enc = Encoder::new(160, 48_000, 1, 1);
    let pcm = tones(48_000, 997.0, 1500.0, 0.4, 40);
    let mut f: Vec<u8> = Vec::new();
    for k in 0..40 {
        let mut block = pcm[k * 2304..(k + 1) * 2304].to_vec();
        // Add a 13 kHz tone to both channels, in subband 17, above the bound of 8.
        for (i, v) in block.iter_mut().enumerate() {
            *v += 0.2 * (2.0 * std::f64::consts::PI * 13_000.0 * ((k * 1152 + i / 2) as f64) / 48_000.0).sin() as f32;
        }
        f.extend(enc.frame(&block));
    }
    std::fs::write(&joint, f).unwrap();
    let cases: Vec<(&str, &str, &[&str])> = cases.into_iter().chain([("ours-48k-joint-crc", "", &[][..])]).collect();
    for (name, source, args) in cases {
        let mp2 = dir.join(format!("{name}.mp2"));
        let made = name.starts_with("ours") || Command::new("ffmpeg")
            .args(["-v", "error", "-y", "-f", "lavfi", "-i", source])
            .args(args)
            .args(["-f", "mp2"])
            .arg(&mp2)
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !made {
            eprintln!("SKIPPED {name}: this ffmpeg cannot make it");
            continue;
        }
        let want_raw = Command::new("ffmpeg")
            .args(["-v", "error", "-c:a", "mp2float", "-i"])
            .arg(&mp2)
            .args(["-f", "f32le", "-"])
            .output()
            .unwrap()
            .stdout;
        let want: Vec<f32> = want_raw.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        let bytes = std::fs::read(&mp2).unwrap();
        let mut dec = Decoder::new();
        let mut got = Vec::new();
        let (mut at, mut joint, mut nch) = (0, 0, 1);
        while at + 4 <= bytes.len() {
            let h = Header::parse(&bytes[at..]).expect("ffmpeg writes whole frames");
            joint += (h.mode == 1) as usize;
            nch = h.channels();
            got.extend(dec.decode(&bytes[at..]).expect("decodes").pcm);
            at += h.length();
        }
        assert_eq!(dec.crc_errors, 0, "{name}");
        if name.contains("joint") {
            assert!(joint >= 40, "{name}: only {joint} joint stereo frames");
        }
        // FFmpeg's decoder can drop whole frames at the start (it drops the
        // first when the stream's mode changes after it): compare at the
        // best whole-frame offset.
        let frame = 1152 * nch;
        let (snr, skipped) = (0..3)
            .map(|k| {
                let a = &got[k * frame..];
                let n = a.len().min(want.len());
                let err: f64 = a[..n].iter().zip(&want[..n]).map(|(x, y)| ((x - y) as f64).powi(2)).sum::<f64>();
                let sig: f64 = want[..n].iter().map(|y| (*y as f64).powi(2)).sum::<f64>();
                (10.0 * (sig / err.max(1e-30)).log10(), k)
            })
            .fold((f64::MIN, 0), |a, b| if b.0 > a.0 { b } else { a });
        assert!(snr > 80.0, "{name}: {snr:.1} dB");
        assert!(want.len() + skipped * frame >= got.len() - frame, "{name}: FFmpeg gave {} of {}", want.len(), got.len());
        eprintln!("{name}: ours against FFmpeg's decoder {snr:.1} dB ({skipped} frames FFmpeg skipped)");
        assert!(snr > 80.0, "{name}: {snr:.1} dB");
        ran += 1;
    }
    let _ = std::fs::remove_dir_all(&dir);
    assert!(ran > 0, "ffmpeg made none of the streams");
}
