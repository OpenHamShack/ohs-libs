//! The `wfm` decoder: a broadcast FM station as a Host session (RADIO-SDR-PLAN
//! §4), for a fixed "listen" session or any app that runs decoders through
//! the Host. Radio's own dial plays through `WfmReceiver` directly on its
//! receive thread (python/shack_sdr/wfm.py `Receiver`), so the speaker has
//! no ring in its path; both are the same receiver.
//!
//! **Input.** The station's channel IQ at R, a whole multiple of 48 kHz of
//! at least 144 kS/s (Radio's bank channel is 240 kS/s), or its composite
//! (`input="audio"`, the discriminator's output at R, 75 kHz = 1.0).
//! Another rate is refused at the first block (`bad_input`, `rate`).
//!
//! **Parameters**, all live: `deemphasis` (50, 75 or 0 µs), `stereo`
//! (false: always mono), `blend` (false: full stereo whenever there is a
//! pilot), `rds`.
//!
//! **Output.** Sound as `Output::Audio`, 48 kHz stereo interleaved, into the
//! session's audio ring (Host::audio pulls it, converted if asked). And a
//! `meta` result whenever the stereo flag or a published RDS field changes:
//! `{"stereo", "pi" ("C201") | null, "ps" | null, "rt" | null, "pty" (0-31,
//! the European table) | null, "tp" | null, "ta" | null, "ct" (ISO 8601
//! local time with its offset) | null, "af" ([Hz, ...])}`. The app names
//! the PTY (shack_sdr.wfm.pty_name). Status: `sync` is RDS's, `snrDb` the
//! stereo channel's estimated SNR, `offsetHz` the carrier's offset; the
//! counters are RDS's blocks and groups, `stereo`, and the blend and the
//! pilot's level in thousandths.

use super::receiver::{Config, WfmReceiver};
use crate::decoder::*;

const DEEMPHASIS: &[f64] = &[50.0, 75.0, 0.0];

const PARAMS: &[ParamSpec] = &[
    ParamSpec { key: "deemphasis", kind: ParamKind::Choice(DEEMPHASIS), default: ParamDefault::Num(50.0) },
    ParamSpec { key: "stereo", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
    ParamSpec { key: "blend", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
    ParamSpec { key: "rds", kind: ParamKind::Bool, default: ParamDefault::Bool(true) },
];

pub static INFO: Info = Info {
    id: "wfm",
    family: Family::Broadcast,
    inputs: &[InputKind::Iq, InputKind::Audio],
    rate_hz: 240_000.0,
    bandwidth_hz: 180_000.0,
    outputs: &[OutputKind::Audio, OutputKind::Meta],
    panel: Panel::Meta,
    params: PARAMS,
    needs: &[],
    needs_for: &[],
    min_rate_hz: 144_000.0,
};

pub struct Wfm {
    cfg: Config,
    rx: Option<WfmReceiver>,
    next_index: Option<u64>,
    t_base: f64,
    last_rev: Option<u64>,
    pcm: Vec<f32>,
}

impl Default for Wfm {
    fn default() -> Self {
        Self::new()
    }
}

impl Wfm {
    pub fn new() -> Self {
        Wfm { cfg: Config::default(), rx: None, next_index: None, t_base: 0.0, last_rev: None, pcm: Vec::new() }
    }

    fn meta(&self) -> Fields {
        let rx = self.rx.as_ref().expect("made on the first block");
        let st = rx.status();
        let f = rx.rds().cloned().unwrap_or_default();
        let opt_text = |s: Option<String>| s.map(Value::Text).unwrap_or(Value::Null);
        let opt_bool = |b: Option<bool>| b.map(Value::Bool).unwrap_or(Value::Null);
        vec![
            ("stereo".into(), Value::Bool(st.stereo)),
            ("pi".into(), opt_text(f.pi.map(|p| format!("{p:04X}")))),
            ("ps".into(), opt_text(f.ps)),
            ("rt".into(), opt_text(f.rt)),
            ("pty".into(), f.pty.map(|p| Value::Num(p as f64)).unwrap_or(Value::Null)),
            ("tp".into(), opt_bool(f.tp)),
            ("ta".into(), opt_bool(f.ta)),
            ("ct".into(), opt_text(f.ct.map(|c| c.iso()))),
            ("af".into(), Value::List(f.af.iter().map(|&hz| Value::Num(hz as f64)).collect())),
        ]
    }
}

impl Decoder for Wfm {
    fn info(&self) -> &'static Info {
        &INFO
    }

    fn configure(&mut self, params: &Params) -> Result<(), DecodeError> {
        check_params(params, PARAMS)?;
        if let Some(us) = params.num("deemphasis") {
            self.cfg.deemph_us = us;
        }
        if let Some(on) = params.flag("stereo") {
            self.cfg.stereo = on;
        }
        if let Some(on) = params.flag("blend") {
            self.cfg.blend = on;
        }
        let rds = params.flag("rds").unwrap_or(self.cfg.rds);
        if let Some(rx) = self.rx.as_mut() {
            rx.set_deemphasis(self.cfg.deemph_us).map_err(|_| DecodeError::BadInput("bad_param"))?;
            rx.set_stereo(self.cfg.stereo);
            rx.set_blend(self.cfg.blend);
            if rds != self.cfg.rds {
                // RDS on or off is a new receiver; the next block makes it.
                self.rx = None;
                self.next_index = None;
            }
        }
        self.cfg.rds = rds;
        Ok(())
    }

    fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
        let (rate, t0, index, n) = (input.rate(), input.t0(), input.index(), input.len());
        if self.rx.as_ref().is_none_or(|r| (r.rate() - rate).abs() > 1e-6) {
            self.rx = Some(WfmReceiver::new(rate, self.cfg.clone()).map_err(|e| DecodeError::BadInput(e.code()))?);
            self.next_index = None;
            self.last_rev = None;
        }
        let rx = self.rx.as_mut().expect("made above");
        if self.next_index != Some(index) {
            // A gap (or the start): FM needs nothing re-found, so the
            // receiver carries on; only the clock is set again.
            self.t_base = t0 - rx.samples() as f64 / rate;
        }
        self.next_index = Some(index + n as u64);
        let t = self.t_base + rx.samples() as f64 / rate;
        self.pcm.clear();
        match input {
            Input::Iq { samples, .. } => rx.process_iq(samples, &mut self.pcm),
            Input::Audio { samples, .. } => rx.process_mpx(samples, &mut self.pcm),
        }
        if !self.pcm.is_empty() {
            out.push(t, Output::Audio { pcm: self.pcm.clone(), rate: super::receiver::AUDIO_RATE, channels: 2 });
        }
        let (rev, ts) = rx.rds_rev();
        if self.last_rev != Some(rev) {
            self.last_rev = Some(rev);
            let t_meta = self.t_base + ts as f64 / rate;
            out.push(t_meta, Output::Meta { fields: self.meta() });
        }
        Ok(())
    }

    fn reset(&mut self) {
        if let Some(rx) = self.rx.as_mut() {
            rx.reset();
        }
        self.next_index = None;
    }

    fn status(&self) -> Status {
        let Some(rx) = self.rx.as_ref() else {
            return Status::default();
        };
        let s = rx.status();
        Status {
            sync: s.rds_sync,
            snr_db: s.snr_db,
            offset_hz: Some(s.offset_hz),
            baud: None,
            counters: vec![
                ("blocks", s.rds_counts.ok + s.rds_counts.corrected),
                ("corrected", s.rds_counts.corrected),
                ("blockErrors", s.rds_counts.bad),
                ("groups", s.rds_counts.groups),
                ("stereo", s.stereo as u64),
                ("blend", (s.blend * 1000.0).round() as u64),
                ("pilot", (s.pilot.max(0.0) * 1000.0).round() as u64),
            ],
        }
    }
}
