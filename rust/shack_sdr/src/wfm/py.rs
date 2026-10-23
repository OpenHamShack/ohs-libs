//! Broadcast FM's part of `shack_sdr._native`: the receiver as a stream
//! object for an app's own receive thread (`WfmReceiver`), the band
//! scanner's function (`wfm_scan`), and the test transmitter for the
//! Python tests and the wheel's smoke test. python/shack_sdr/wfm.py wraps
//! them (numpy in and out, PTY names); apps import that, never this.
//!
//! The GIL is released for the work, as everywhere in the crate; samples
//! are read in place through the buffer protocol (complex64 viewed as
//! float32 pairs by the Python half).

use super::rds::{Fields, Station};
use super::receiver::{Config, Status, WfmReceiver};
use super::scan::{scan, ScanOptions};
use super::tx::{self, Audio, BandStation, Channel, Tx};
use crate::py::{view, DecoderError};
use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use rustfft::num_complex::Complex32;

fn f32_bytes<'py>(py: Python<'py>, x: &[f32]) -> Bound<'py, PyBytes> {
    let bytes: Vec<u8> = x.iter().flat_map(|v| v.to_le_bytes()).collect();
    PyBytes::new(py, &bytes)
}

fn iq_bytes<'py>(py: Python<'py>, x: &[Complex32]) -> Bound<'py, PyBytes> {
    let bytes: Vec<u8> = x.iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect();
    PyBytes::new(py, &bytes)
}

fn fields_into(d: &Bound<'_, PyDict>, f: &Fields) -> PyResult<()> {
    d.set_item("pi", f.pi.map(|p| format!("{p:04X}")))?;
    d.set_item("ps", f.ps.as_deref())?;
    d.set_item("rt", f.rt.as_deref())?;
    d.set_item("pty", f.pty)?;
    d.set_item("ta", f.ta)?;
    d.set_item("tp", f.tp)?;
    d.set_item("ct", f.ct.map(|c| c.iso()))?;
    d.set_item("af", f.af.clone())?;
    Ok(())
}

fn status_py<'py>(py: Python<'py>, s: &Status) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("stereo", s.stereo)?;
    d.set_item("blend", s.blend)?;
    d.set_item("pilot", s.pilot)?;
    d.set_item("snrDb", s.snr_db)?;
    d.set_item("levelDb", s.level_db)?;
    d.set_item("offsetHz", s.offset_hz)?;
    d.set_item("rdsSync", s.rds_sync)?;
    d.set_item("rdsSnrDb", s.rds_snr_db)?;
    d.set_item("blocks", s.rds_counts.ok + s.rds_counts.corrected)?;
    d.set_item("corrected", s.rds_counts.corrected)?;
    d.set_item("blockErrors", s.rds_counts.bad)?;
    d.set_item("groups", s.rds_counts.groups)?;
    Ok(d)
}

#[pyclass(name = "WfmReceiver", module = "shack_sdr._native")]
pub(crate) struct PyWfm {
    rx: WfmReceiver,
}

impl PyWfm {
    fn run(&mut self, py: Python<'_>, samples: PyBuffer<f32>, iq: bool) -> PyResult<Vec<f32>> {
        let v = view(&samples)?;
        if iq && v.len % 2 != 0 {
            return Err(PyValueError::new_err("IQ must be float32 pairs"));
        }
        let rx = &mut self.rx;
        let out = py.detach(|| {
            let mut out = Vec::new();
            if iq {
                // SAFETY: `samples` holds the buffer for this whole call (py.rs View).
                let x = unsafe { std::slice::from_raw_parts(v.ptr as *const Complex32, v.len / 2) };
                rx.process_iq(x, &mut out);
            } else {
                // SAFETY: as above.
                let x = unsafe { std::slice::from_raw_parts(v.ptr as *const f32, v.len) };
                rx.process_mpx(x, &mut out);
            }
            out
        });
        drop(samples);
        Ok(out)
    }
}

#[pymethods]
impl PyWfm {
    #[new]
    #[pyo3(signature = (rate=240_000.0, deemphasis_us=50.0, stereo=true, blend=true, rds=true, audio=true, audio_rate=48_000, channels=2))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        rate: f64,
        deemphasis_us: f64,
        stereo: bool,
        blend: bool,
        rds: bool,
        audio: bool,
        audio_rate: u32,
        channels: u8,
    ) -> PyResult<Self> {
        let cfg = Config { deemph_us: deemphasis_us, stereo, blend, rds, audio, audio_rate, channels };
        let rx = WfmReceiver::new(rate, cfg).map_err(|e| DecoderError::new_err(("bad_input", e.code())))?;
        Ok(PyWfm { rx })
    }

    /// Channel IQ (complex64 as float32 pairs) in; audio out as float32
    /// bytes, interleaved when stereo.
    fn process_iq<'py>(&mut self, py: Python<'py>, samples: PyBuffer<f32>) -> PyResult<Bound<'py, PyBytes>> {
        let out = self.run(py, samples, true)?;
        Ok(f32_bytes(py, &out))
    }

    /// The composite (75 kHz = 1.0) in; audio out as for process_iq.
    fn process_mpx<'py>(&mut self, py: Python<'py>, samples: PyBuffer<f32>) -> PyResult<Bound<'py, PyBytes>> {
        let out = self.run(py, samples, false)?;
        Ok(f32_bytes(py, &out))
    }

    fn status<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        status_py(py, &self.rx.status())
    }

    /// The published RDS fields, the stereo flag, `rev` (rises on every
    /// change) and `sample` (the sample count at the last change).
    fn rds<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        fields_into(&d, &self.rx.rds().cloned().unwrap_or_default())?;
        d.set_item("stereo", self.rx.status().stereo)?;
        let (rev, sample) = self.rx.rds_rev();
        d.set_item("rev", rev)?;
        d.set_item("sample", sample)?;
        Ok(d)
    }

    fn reset(&mut self) {
        self.rx.reset();
    }

    fn set_deemphasis(&mut self, us: f64) -> PyResult<()> {
        self.rx.set_deemphasis(us).map_err(|e| DecoderError::new_err(("bad_params", e.code())))
    }

    fn set_stereo(&mut self, on: bool) {
        self.rx.set_stereo(on);
    }

    fn set_blend(&mut self, on: bool) {
        self.rx.set_blend(on);
    }

    /// Samples taken since the start or the last reset.
    fn samples(&self) -> u64 {
        self.rx.samples()
    }

    fn rate(&self) -> f64 {
        self.rx.rate()
    }
}

/// wfm::scan on complex64 samples viewed as float32: {"stations": [...],
/// "usedS", "timedOut", "cpuS", "wallS", "noiseDb"}.
#[pyfunction]
#[pyo3(signature = (samples, rate, centre_hz, budget_s=3.0, threads=0, min_snr_db=10.0, rt=false))]
#[allow(clippy::too_many_arguments)]
fn wfm_scan<'py>(
    py: Python<'py>,
    samples: PyBuffer<f32>,
    rate: f64,
    centre_hz: f64,
    budget_s: f64,
    threads: usize,
    min_snr_db: f64,
    rt: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let v = view(&samples)?;
    if v.len % 2 != 0 {
        return Err(PyValueError::new_err("IQ must be float32 pairs"));
    }
    if !(rate >= 250_000.0 && rate.is_finite()) {
        return Err(DecoderError::new_err(("bad_input", "rate")));
    }
    let opts = ScanOptions { budget_s, threads, min_snr_db, rt };
    let r = py.detach(|| {
        // SAFETY: `samples` holds the buffer for this whole call (py.rs View).
        let x = unsafe { std::slice::from_raw_parts(v.ptr as *const Complex32, v.len / 2) };
        scan(x, rate, centre_hz, &opts)
    });
    drop(samples);
    let d = PyDict::new(py);
    let mut list = Vec::new();
    for f in &r.stations {
        let s = PyDict::new(py);
        s.set_item("hz", f.hz.round() as i64)?;
        s.set_item("levelDb", f.level_db)?;
        s.set_item("stereo", f.stereo)?;
        fields_into(&s, &f.fields)?;
        s.set_item("rdsSync", f.rds_sync)?;
        s.set_item("complete", f.complete)?;
        list.push(s);
    }
    d.set_item("stations", list)?;
    d.set_item("usedS", r.used_s)?;
    d.set_item("timedOut", r.timed_out)?;
    d.set_item("cpuS", r.cpu_s)?;
    d.set_item("wallS", r.wall_s)?;
    d.set_item("noiseDb", r.noise_db)?;
    Ok(d)
}

fn station(pi: u16, ps: &str, pty: u8, rt: &str) -> Station {
    let mut st = Station::new(pi, ps, pty);
    st.rt = rt.as_bytes().to_vec();
    st
}

/// The test transmitter (wfm::tx): `seconds` of an FM stereo station with
/// RDS as complex64 bytes at `rate` (or its composite as float32 with
/// `composite`), through a channel of `cnr_db` (None: clean) and
/// `offset_hz`. A tone of `amp` at `left_hz` in L and at `right_hz` in R
/// (0: silent); no pilot (mono) with `pilot=False`; no RDS with `pi=None`.
#[pyfunction]
#[pyo3(signature = (seconds=2.0, rate=240_000.0, cnr_db=None, offset_hz=0.0, left_hz=1_000.0, right_hz=0.0, amp=0.5,
                    pilot=true, pi=Some(0xC201), ps="TEST FM ", rt="Hello from the test transmitter", pty=10,
                    composite=false, seed=1))]
#[allow(clippy::too_many_arguments)]
fn testsig_wfm<'py>(
    py: Python<'py>,
    seconds: f64,
    rate: f64,
    cnr_db: Option<f64>,
    offset_hz: f64,
    left_hz: f64,
    right_hz: f64,
    amp: f64,
    pilot: bool,
    pi: Option<u16>,
    ps: &str,
    rt: &str,
    pty: u8,
    composite: bool,
    seed: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    if !(rate >= 128_000.0 && rate <= 2_400_000.0) || !(0.0..=60.0).contains(&seconds) || pty > 31 {
        return Err(PyValueError::new_err("rate 128 kS/s-2.4 MS/s, at most 60 s, pty 0-31"));
    }
    let mut t = Tx::new(rate);
    let tone = |hz: f64| if hz > 0.0 { Audio::Tone { hz, amp } } else { Audio::Silence };
    t.left = tone(left_hz);
    t.right = tone(right_hz);
    if !pilot {
        t.pilot = 0.0;
    }
    t.rds = pi.map(|pi| station(pi, ps, pty, rt));
    let ch = Channel { cnr_db, offset_hz, seed, ..Default::default() };
    if composite {
        let m = py.detach(|| tx::mpx(&t, seconds));
        return Ok(f32_bytes(py, &m));
    }
    let x = py.detach(|| tx::channel(&tx::fm(&tx::mpx(&t, seconds), rate), rate, &ch));
    Ok(iq_bytes(py, &x))
}

/// A synthetic band (wfm::tx::band): `stations` as (offset_hz, level_db,
/// pi, ps, pty, stereo), `seconds` of complex64 bytes at `rate`, in noise
/// of `noise_db` dBFS over the whole band.
#[pyfunction]
#[pyo3(signature = (stations, rate=8_000_000.0, seconds=2.0, noise_db=-60.0, seed=1))]
fn testsig_fm_band<'py>(
    py: Python<'py>,
    stations: Vec<(f64, f64, u16, String, u8, bool)>,
    rate: f64,
    seconds: f64,
    noise_db: f64,
    seed: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    if !(rate >= 1_000_000.0 && rate <= 12_000_000.0) || !(0.0..=10.0).contains(&seconds) {
        return Err(PyValueError::new_err("rate 1-12 MS/s, at most 10 s"));
    }
    let band: Vec<BandStation> = stations
        .iter()
        .map(|(offset_hz, level_db, pi, ps, pty, stereo)| {
            let mut t = Tx::new(250_000.0);
            t.left = Audio::Noise { amp: 0.25, seed: *pi as u64 };
            t.right = Audio::Noise { amp: 0.25, seed: *pi as u64 + 1 };
            if !stereo {
                t.pilot = 0.0;
            }
            t.rds = Some(station(*pi, ps, *pty, ""));
            BandStation { offset_hz: *offset_hz, level_db: *level_db, tx: t }
        })
        .collect();
    let x = py.detach(|| tx::band(rate, seconds, &band, noise_db, seed));
    Ok(iq_bytes(py, &x))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyWfm>()?;
    m.add_function(wrap_pyfunction!(wfm_scan, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_wfm, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_fm_band, m)?)?;
    Ok(())
}
