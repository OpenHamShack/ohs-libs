//! The Python face's native half, `shack_sdr._native` (RADIO-SDR-PLAN
//! §4.2). The Python package (python/shack_sdr) wraps it; apps import
//! `shack_sdr`, never this module.
//!
//! - The GIL is released for every call that does work (`py.detach`), as
//!   ohsdigi does: feeding copies samples with it released, and the decoding
//!   itself happens on the host's own threads, which never touch Python.
//! - Samples cross through the buffer protocol and are read in place: a
//!   numpy float32 array (complex64 arrives viewed as float32 pairs, which
//!   the Python half does without a copy). Not converted to bytes, as
//!   HsReceiver does: the IQ path is heavier.
//! - Refusals raise `DecoderError(code, detail)`: codes, never sentences.

use crate::catalogue as cat;
use crate::codecs;
use crate::decoder::{Info, InputKind, Output, ParamDefault, ParamKind, Params, Value};
use crate::host::{Body, Host, HostConfig, HostError, Item, SessionInfo};
use crate::testsig;
use pyo3::buffer::PyBuffer;
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use rustfft::num_complex::Complex32;

create_exception!(_native, DecoderError, PyException, "A decoder call was refused: args are (code, detail).");

fn err(e: HostError) -> PyErr {
    DecoderError::new_err((e.code(), e.detail()))
}

fn params_from(d: Option<&Bound<'_, PyDict>>) -> PyResult<Params> {
    let mut p = Params::new();
    if let Some(d) = d {
        for (k, v) in d.iter() {
            let key: String = k.extract()?;
            // bool before int: a Python bool is an int.
            let val = if v.is_instance_of::<PyBool>() {
                Value::Bool(v.extract()?)
            } else if v.is_instance_of::<PyInt>() || v.is_instance_of::<PyFloat>() {
                Value::Num(v.extract()?)
            } else if v.is_instance_of::<PyString>() {
                Value::Text(v.extract()?)
            } else {
                return Err(DecoderError::new_err(("bad_params", "bad_param")));
            };
            p.0.insert(key, val);
        }
    }
    Ok(p)
}

fn value_py<'py>(py: Python<'py>, v: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match v {
        Value::Num(x) => x.into_pyobject(py)?.into_any(),
        Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
        Value::Text(s) => PyString::new(py, s).into_any(),
        Value::List(items) => {
            let l = PyList::empty(py);
            for it in items {
                l.append(value_py(py, it)?)?;
            }
            l.into_any()
        }
        Value::Map(fields) => fields_py(py, fields)?.into_any(),
        Value::Null => py.None().into_bound(py),
    })
}

fn params_py<'py>(py: Python<'py>, p: &Params) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in &p.0 {
        d.set_item(k, value_py(py, v)?)?;
    }
    Ok(d)
}

fn fields_py<'py>(py: Python<'py>, f: &[(String, Value)]) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in f {
        d.set_item(k, value_py(py, v)?)?;
    }
    Ok(d)
}

fn item_py<'py>(py: Python<'py>, it: &Item) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("session", &it.session)?;
    d.set_item("rev", it.rev)?;
    d.set_item("t", it.t)?;
    match &it.body {
        Body::State { state, why } => {
            d.set_item("type", "state")?;
            d.set_item("state", state.code())?;
            d.set_item("why", why.as_deref())?;
        }
        Body::Output(o) => {
            d.set_item("type", o.kind().code())?;
            match o {
                Output::Text { text, offset_hz, snr_db } => {
                    d.set_item("text", text)?;
                    d.set_item("offsetHz", *offset_hz)?;
                    d.set_item("snrDb", *snr_db)?;
                }
                Output::Frame { kind, fields, raw } => {
                    d.set_item("frame", *kind)?;
                    d.set_item("fields", fields_py(py, fields)?)?;
                    d.set_item("raw", raw.as_ref().map(|r| PyBytes::new(py, r)))?;
                }
                Output::Meta { fields } => d.set_item("fields", fields_py(py, fields)?)?,
                Output::Audio { pcm, rate, channels } => {
                    let bytes: Vec<u8> = pcm.iter().flat_map(|x| x.to_le_bytes()).collect();
                    d.set_item("pcm", PyBytes::new(py, &bytes))?;
                    d.set_item("rate", *rate)?;
                    d.set_item("channels", *channels)?;
                }
                Output::Image { event } => {
                    use crate::decoder::ImageEvent;
                    match event {
                        ImageEvent::Start { width, height, format } => {
                            d.set_item("event", "start")?;
                            d.set_item("width", *width)?;
                            d.set_item("height", *height)?;
                            d.set_item("format", *format)?;
                        }
                        ImageEvent::Lines { from, rgb } => {
                            d.set_item("event", "lines")?;
                            d.set_item("from", *from)?;
                            d.set_item("rgb", PyBytes::new(py, rgb))?;
                        }
                        ImageEvent::Done { file, path, .. } => {
                            d.set_item("event", "done")?;
                            d.set_item("file", file)?;
                            d.set_item("path", path.as_deref())?;
                        }
                    }
                }
            }
        }
    }
    Ok(d)
}

fn status_py<'py>(py: Python<'py>, s: &SessionInfo) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("id", &s.id)?;
    d.set_item("decoder", s.decoder)?;
    d.set_item("input", s.input.code())?;
    d.set_item("rate", s.rate)?;
    d.set_item("params", params_py(py, &s.params)?)?;
    d.set_item("pinned", s.pinned)?;
    d.set_item("state", s.state.code())?;
    d.set_item("why", s.why.as_deref())?;
    d.set_item("since", s.since)?;
    d.set_item("rev", s.rev)?;
    let st = PyDict::new(py);
    st.set_item("snrDb", s.decoder_status.snr_db)?;
    st.set_item("offsetHz", s.decoder_status.offset_hz)?;
    st.set_item("baud", s.decoder_status.baud)?;
    st.set_item("sync", s.decoder_status.sync)?;
    st.set_item("cpu", s.cpu)?;
    st.set_item("fed", s.fed)?;
    st.set_item("dropped", s.dropped)?;
    for (k, v) in &s.decoder_status.counters {
        st.set_item(*k, *v)?;
    }
    d.set_item("stats", st)?;
    d.set_item("meta", s.meta.as_ref().map(|m| fields_py(py, m)).transpose()?)?;
    let a = PyDict::new(py);
    a.set_item("queued", s.audio_queued)?;
    a.set_item("dropped", s.audio_dropped)?;
    d.set_item("audio", a)?;
    Ok(d)
}

fn info_py<'py>(py: Python<'py>, i: &Info) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("id", i.id)?;
    d.set_item("family", i.family.code())?;
    d.set_item("inputs", i.inputs.iter().map(|x| x.code()).collect::<Vec<_>>())?;
    d.set_item("rateHz", i.rate_hz)?;
    d.set_item("bandwidthHz", i.bandwidth_hz)?;
    d.set_item("outputs", i.outputs.iter().map(|x| x.code()).collect::<Vec<_>>())?;
    d.set_item("panel", i.panel.code())?;
    if let crate::decoder::Panel::Frames { columns } = i.panel {
        d.set_item("columns", columns.to_vec())?;
    }
    let params = PyList::empty(py);
    for p in i.params {
        let pd = PyDict::new(py);
        pd.set_item("key", p.key)?;
        match p.kind {
            ParamKind::Range { min, max } => {
                pd.set_item("type", "number")?;
                pd.set_item("min", min)?;
                pd.set_item("max", max)?;
            }
            ParamKind::Choice(c) => {
                pd.set_item("type", "choice")?;
                pd.set_item("choices", c.to_vec())?;
            }
            ParamKind::Bool => pd.set_item("type", "bool")?,
            ParamKind::Text => pd.set_item("type", "text")?,
        }
        match p.default {
            ParamDefault::Num(x) => pd.set_item("default", x)?,
            ParamDefault::Bool(b) => pd.set_item("default", b)?,
            ParamDefault::Text(t) => pd.set_item("default", t)?,
        }
        params.append(pd)?;
    }
    d.set_item("params", params)?;
    d.set_item("needs", i.needs.iter().map(|c| c.code()).collect::<Vec<_>>())?;
    let nf = PyDict::new(py);
    for (c, parts) in i.needs_for {
        nf.set_item(c.code(), parts.to_vec())?;
    }
    d.set_item("needsFor", nf)?;
    d.set_item("minRateHz", i.min_rate_hz)?;
    d.set_item("engine", "shack_sdr")?;
    d.set_item("version", crate::VERSION)?;
    Ok(d)
}

/// A read-only view of a float32 buffer, its pointer usable with the GIL
/// released: the exporter (a numpy array) cannot resize or free its memory
/// while the view is held, and the view outlives the use.
pub(crate) struct View {
    pub(crate) ptr: usize,
    pub(crate) len: usize,
}

pub(crate) fn view(buf: &PyBuffer<f32>) -> PyResult<View> {
    if !buf.is_c_contiguous() {
        return Err(PyValueError::new_err("samples must be contiguous"));
    }
    Ok(View { ptr: buf.buf_ptr() as usize, len: buf.item_count() })
}

#[pyclass(name = "Host", frozen, module = "shack_sdr._native")]
struct PyHost {
    inner: Host,
}

#[pymethods]
impl PyHost {
    #[new]
    #[pyo3(signature = (threads=1, cpu_max=0.25, ring_s=2.0, max_sessions=17, testing=false, files_dir=None, files_keep=16))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        threads: usize,
        cpu_max: f64,
        ring_s: f64,
        max_sessions: usize,
        testing: bool,
        files_dir: Option<std::path::PathBuf>,
        files_keep: usize,
    ) -> Self {
        PyHost {
            inner: Host::new(HostConfig {
                threads,
                cpu_max,
                ring_s,
                max_sessions,
                testing,
                files_dir,
                files_keep,
                ..Default::default()
            }),
        }
    }

    /// A decoder's file by name (`api/decoders/files?id=`), or None.
    fn file<'py>(&self, py: Python<'py>, name: &str) -> Option<Bound<'py, PyBytes>> {
        self.inner.file(name).map(|b| PyBytes::new(py, &b))
    }

    /// [{"name", "session", "decoder", "t", "size", "path"}], oldest first.
    fn files<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
        self.inner
            .files()
            .into_iter()
            .map(|f| {
                let d = PyDict::new(py);
                d.set_item("name", f.name)?;
                d.set_item("session", f.session)?;
                d.set_item("decoder", f.decoder)?;
                d.set_item("t", f.t)?;
                d.set_item("size", f.size)?;
                d.set_item("path", f.path.map(|p| p.to_string_lossy().into_owned()))?;
                Ok(d)
            })
            .collect()
    }

    #[pyo3(signature = (decoder, params=None, input="iq", rate=12_000.0, center_hz=0.0, pinned=false))]
    fn start(
        &self,
        decoder: &str,
        params: Option<&Bound<'_, PyDict>>,
        input: &str,
        rate: f64,
        center_hz: f64,
        pinned: bool,
    ) -> PyResult<String> {
        let kind = InputKind::parse(input).ok_or_else(|| DecoderError::new_err(("bad_input", "input")))?;
        let p = params_from(params)?;
        self.inner.start(decoder, p, kind, rate, center_hz, pinned).map_err(err)
    }

    /// Complex samples as interleaved float32 (a complex64 array viewed as
    /// float32).
    fn feed_iq(&self, py: Python<'_>, sid: &str, samples: PyBuffer<f32>, t0: f64, index: u64) -> PyResult<()> {
        let v = view(&samples)?;
        if v.len % 2 != 0 {
            return Err(PyValueError::new_err("IQ must be float32 pairs"));
        }
        let r = py.detach(|| {
            // SAFETY: `samples` holds the buffer for this whole call (see View).
            let x = unsafe { std::slice::from_raw_parts(v.ptr as *const Complex32, v.len / 2) };
            self.inner.feed_iq(sid, x, t0, index)
        });
        drop(samples);
        r.map_err(err)
    }

    fn feed_audio(&self, py: Python<'_>, sid: &str, samples: PyBuffer<f32>, t0: f64, index: u64) -> PyResult<()> {
        let v = view(&samples)?;
        let r = py.detach(|| {
            // SAFETY: as feed_iq.
            let x = unsafe { std::slice::from_raw_parts(v.ptr as *const f32, v.len) };
            self.inner.feed_audio(sid, x, t0, index)
        });
        drop(samples);
        r.map_err(err)
    }

    #[pyo3(signature = (max_items=500))]
    fn poll<'py>(&self, py: Python<'py>, max_items: usize) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let items = py.detach(|| self.inner.poll(max_items));
        items.iter().map(|it| item_py(py, it)).collect()
    }

    fn status<'py>(&self, py: Python<'py>, sid: &str) -> PyResult<Bound<'py, PyDict>> {
        let s = py.detach(|| self.inner.status(sid)).map_err(err)?;
        status_py(py, &s)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = py.detach(|| self.inner.stats());
        let d = PyDict::new(py);
        d.set_item("sessions", s.sessions)?;
        d.set_item("threads", s.threads)?;
        d.set_item("cpu", s.cpu)?;
        d.set_item("cpuMax", s.cpu_max)?;
        d.set_item("queued", s.queued)?;
        d.set_item("lost", s.lost)?;
        Ok(d)
    }

    /// New parameters for a running session (checked now, applied live).
    #[pyo3(signature = (sid, params))]
    fn configure(&self, sid: &str, params: &Bound<'_, PyDict>) -> PyResult<()> {
        let p = params_from(Some(params))?;
        self.inner.configure(sid, p).map_err(err)
    }

    /// The session's sound: [{"t", "rate", "channels", "pcm"}], pcm as
    /// float32 bytes (interleaved when stereo), at most `max_s` seconds,
    /// converted to `rate`/`channels` when given.
    #[pyo3(signature = (sid, max_s=1.0, rate=None, channels=None))]
    fn audio<'py>(
        &self,
        py: Python<'py>,
        sid: &str,
        max_s: f64,
        rate: Option<u32>,
        channels: Option<u8>,
    ) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let chunks = py.detach(|| self.inner.audio(sid, max_s, rate, channels)).map_err(err)?;
        chunks
            .iter()
            .map(|c| {
                let d = PyDict::new(py);
                d.set_item("t", c.t)?;
                d.set_item("rate", c.rate)?;
                d.set_item("channels", c.channels)?;
                let bytes: Vec<u8> = c.pcm.iter().flat_map(|x| x.to_le_bytes()).collect();
                d.set_item("pcm", PyBytes::new(py, &bytes))?;
                Ok(d)
            })
            .collect()
    }

    fn sessions(&self) -> Vec<String> {
        self.inner.sessions()
    }

    fn reset(&self, sid: &str) -> PyResult<()> {
        self.inner.reset(sid).map_err(err)
    }

    fn set_outside(&self, sid: &str, outside: bool) -> PyResult<()> {
        self.inner.set_outside(sid, outside).map_err(err)
    }

    fn stop(&self, sid: &str) -> PyResult<()> {
        self.inner.stop(sid).map_err(err)
    }

    fn close(&self, py: Python<'_>) -> bool {
        py.detach(|| self.inner.close())
    }
}

/// The native decoders' catalogue entries.
#[pyfunction]
fn catalogue<'py>(py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
    cat::kinds().iter().map(|k| info_py(py, k.info)).collect()
}

/// A synthetic RTTY test signal (testsig.rs), as float32 bytes (audio) or
/// complex64 bytes (iq=True). `fading` is None, "good", "moderate" or "poor".
#[pyfunction]
#[pyo3(signature = (text, baud=45.45, shift=170.0, reverse=false, rate=12_000.0, centre=1000.0, drift=0.0,
                    snr_db=None, fading=None, lead_s=1.0, tail_s=1.0, seed=1, iq=false, clock_ppm=0.0,
                    stop_bits=1.5))]
#[allow(clippy::too_many_arguments)]
fn testsig_rtty<'py>(
    py: Python<'py>,
    text: &str,
    baud: f64,
    shift: f64,
    reverse: bool,
    rate: f64,
    centre: f64,
    drift: f64,
    snr_db: Option<f64>,
    fading: Option<&str>,
    lead_s: f64,
    tail_s: f64,
    seed: u64,
    iq: bool,
    clock_ppm: f64,
    stop_bits: f64,
) -> PyResult<Bound<'py, PyBytes>> {
    let fading = match fading {
        None => None,
        Some("good") => Some(testsig::Watterson::GOOD),
        Some("moderate") => Some(testsig::Watterson::MODERATE),
        Some("poor") => Some(testsig::Watterson::POOR),
        Some(_) => return Err(PyValueError::new_err("fading is good, moderate or poor")),
    };
    let case = testsig::RttyCase {
        text: text.to_string(),
        tx: testsig::RttyTx { baud, shift, reverse, stop_bits, clock_ppm, ..Default::default() },
        rate,
        centre,
        drift,
        snr_db,
        fading,
        lead_s,
        tail_s,
        seed,
    };
    let bytes: Vec<u8> = py.detach(|| {
        if iq {
            testsig::rtty_iq(&case).iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect()
        } else {
            testsig::rtty_audio(&case).iter().flat_map(|x| x.to_le_bytes()).collect()
        }
    });
    Ok(PyBytes::new(py, &bytes))
}

/// The DAB transmitter model's IQ (dab::tx): `frames` 96 ms frames of the
/// test ensemble (an MP2 service C221 and a DAB+ service C3A7) as complex64
/// bytes at `rate`, through a channel of `snr_db` (None: clean),
/// `offset_hz`, `clock_ppm` and an echo of `echo_db` at `echo_us`.
/// `sites`, a list of (main, sub, delay_us, gain_db), makes it a
/// single-frequency network of transmitters sending those TII codes
/// (dab::tx::sfn_iq); `eid` replaces the test ensemble's identifier, so an
/// app can be tested against a real network's.
#[pyfunction]
#[pyo3(signature = (frames=10, snr_db=None, offset_hz=0.0, rate=2_048_000.0, clock_ppm=0.0, echo_us=0.0, echo_db=-6.0, seed=1, sites=None, eid=None))]
#[allow(clippy::too_many_arguments)]
fn testsig_dab<'py>(
    py: Python<'py>,
    frames: usize,
    snr_db: Option<f64>,
    offset_hz: f64,
    rate: f64,
    clock_ppm: f64,
    echo_us: f64,
    echo_db: f64,
    seed: u64,
    sites: Option<Vec<(u8, u8, f64, f64)>>,
    eid: Option<u16>,
) -> PyResult<Bound<'py, PyBytes>> {
    use crate::dab::tx::{sfn_iq, Channel, Path, Site, TxEnsemble};
    let sites: Vec<Site> = match sites {
        None => vec![Site { tii: None, delay: 0, gain: 1.0 }],
        Some(v) => v
            .into_iter()
            .map(|(main, sub, delay_us, gain_db)| {
                if crate::dab::tii::carriers(main, sub).is_none() || !(0.0..=1000.0).contains(&delay_us) {
                    return Err(PyValueError::new_err("a site is (main 0-69, sub 0-23, delay_us 0-1000, gain_db)"));
                }
                Ok(Site { tii: Some((main, sub)), delay: (delay_us * 2.048).round() as usize, gain: 10f64.powf(gain_db / 20.0) })
            })
            .collect::<PyResult<_>>()?,
    };
    let mut ensemble = TxEnsemble::standard();
    if let Some(e) = eid {
        ensemble.eid = e;
    }
    if !(rate >= 2_048_000.0 && rate <= 10_000_000.0) || frames > 400 {
        return Err(PyValueError::new_err("rate 2.048-10 MS/s, at most 400 frames"));
    }
    let paths = if echo_us > 0.0 {
        vec![
            Path { delay: 0, gain: 1.0, doppler_hz: 0.0 },
            Path { delay: (echo_us * 2.048).round() as usize, gain: 10f64.powf(echo_db / 20.0), doppler_hz: 0.0 },
        ]
    } else {
        vec![]
    };
    let ch = Channel { snr_db, offset_hz, rate, clock_ppm, paths, seed, ..Default::default() };
    let bytes: Vec<u8> = py.detach(|| {
        sfn_iq(&ensemble, &sites, frames, &ch)
            .iter()
            .flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes()))
            .collect()
    });
    Ok(PyBytes::new(py, &bytes))
}

/// dab::survey on complex64 samples viewed as float32: {"meta", "syncS",
/// "completeS", "listenedS"}.
#[pyfunction]
#[pyo3(signature = (samples, rate=2_048_000.0, give_up_s=1.0))]
fn dab_survey<'py>(py: Python<'py>, samples: PyBuffer<f32>, rate: f64, give_up_s: f64) -> PyResult<Bound<'py, PyDict>> {
    let v = view(&samples)?;
    if v.len % 2 != 0 {
        return Err(PyValueError::new_err("IQ must be float32 pairs"));
    }
    let r = py.detach(|| {
        // SAFETY: `samples` holds the buffer for this whole call (see View).
        let x = unsafe { std::slice::from_raw_parts(v.ptr as *const Complex32, v.len / 2) };
        crate::dab::survey(x, rate, give_up_s)
    });
    drop(samples);
    let s = r.map_err(|e| DecoderError::new_err(("bad_input", e.code())))?;
    let d = PyDict::new(py);
    d.set_item("meta", fields_py(py, &s.meta)?)?;
    d.set_item("syncS", s.sync_s)?;
    d.set_item("completeS", s.complete_s)?;
    d.set_item("listenedS", s.listened_s)?;
    Ok(d)
}

/// The Band III blocks: [(name, centre Hz)].
#[pyfunction]
fn dab_blocks() -> Vec<(&'static str, u32)> {
    crate::dab::tables::BAND_III.to_vec()
}

/// What a decoder should print for `text` (the printer's own rules).
#[pyfunction]
fn rtty_truth(text: &str) -> String {
    testsig::rtty_truth(text)
}

#[pyfunction]
fn levenshtein(a: &str, b: &str) -> usize {
    testsig::levenshtein(a, b)
}

/// The codec table (codecs.rs): id, what it is for, whether we may ship it,
/// and the file names tried on this OS.
#[pyfunction]
fn codec_entries<'py>(py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
    codecs::ENTRIES
        .iter()
        .map(|e| {
            let d = PyDict::new(py);
            d.set_item("id", e.id.code())?;
            d.set_item("for", e.used_for.to_vec())?;
            d.set_item("shippable", e.shippable)?;
            d.set_item("names", codecs::names(e.id).to_vec())?;
            d.set_item("hostLibrary", e.host_library)?;
            Ok(d)
        })
        .collect()
}

/// Load `path` as codec `id` in THIS process and run its checks and
/// known-answer test. Only the probe child calls this (shack_sdr.codecs):
/// a library that crashes takes the child with it, not the app.
#[pyfunction]
fn codec_probe<'py>(py: Python<'py>, id: &str, path: &str) -> PyResult<Bound<'py, PyDict>> {
    let cid = crate::decoder::CodecId::parse(id).ok_or_else(|| PyValueError::new_err("unknown codec"))?;
    let path = path.to_string();
    let r = py.detach(move || codecs::probe(cid, std::path::Path::new(&path)));
    let d = PyDict::new(py);
    match r {
        Ok(version) => {
            d.set_item("ok", true)?;
            d.set_item("version", version)?;
        }
        Err(e) => {
            d.set_item("ok", false)?;
            d.set_item("why", e.code())?;
            d.set_item("detail", e.detail())?;
        }
    }
    Ok(d)
}

/// The file the loader chose for codec `id` (one that passed the probe),
/// or None; the decoders that play through it open that file and no other.
#[pyfunction]
#[pyo3(signature = (id, path=None))]
fn codec_use(id: &str, path: Option<String>) -> PyResult<()> {
    let cid = crate::decoder::CodecId::parse(id).ok_or_else(|| PyValueError::new_err("unknown codec"))?;
    codecs::use_path(cid, path.map(std::path::PathBuf::from));
    Ok(())
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", crate::VERSION)?;
    m.add("DecoderError", m.py().get_type::<DecoderError>())?;
    m.add_class::<PyHost>()?;
    m.add_function(wrap_pyfunction!(catalogue, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_rtty, m)?)?;
    m.add_function(wrap_pyfunction!(rtty_truth, m)?)?;
    m.add_function(wrap_pyfunction!(levenshtein, m)?)?;
    m.add_function(wrap_pyfunction!(codec_entries, m)?)?;
    m.add_function(wrap_pyfunction!(codec_probe, m)?)?;
    m.add_function(wrap_pyfunction!(codec_use, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_dab, m)?)?;
    m.add_function(wrap_pyfunction!(dab_blocks, m)?)?;
    m.add_function(wrap_pyfunction!(dab_survey, m)?)?;
    crate::wfm::py::register(m)?;
    crate::psk::py::register(m)?;
    crate::sstv::py::register(m)?;
    Ok(())
}
