//! AIS's part of `shack_sdr._native`: writing messages and bursts for
//! tests and for an app's simulator (Radio's simulated ships), and reading
//! a payload or its sentences without a session. The decoder itself is a
//! Host session like any other.

use super::msg::{self, Raw, RawV};
use super::tx::{scene_iq, Burst, Channel, Scene};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

fn raw_from(d: &Bound<'_, PyDict>) -> PyResult<Raw> {
    let mut raw = Raw::new();
    for (k, v) in d.iter() {
        let k: String = k.extract()?;
        let v = if let Ok(s) = v.extract::<String>() {
            RawV::Text(s)
        } else if let Ok(b) = v.extract::<bool>() {
            RawV::Int(b as i64)
        } else if let Ok(i) = v.extract::<i64>() {
            RawV::Int(i)
        } else {
            return Err(PyValueError::new_err(format!("{k}: an int, bool or str")));
        };
        raw.insert(k, v);
    }
    Ok(raw)
}

fn bits_of(payload: &[u8]) -> Vec<u8> {
    payload.iter().flat_map(|&b| (0..8).rev().map(move |i| (b >> i) & 1)).collect()
}

fn octets_of(bits: &[u8]) -> Vec<u8> {
    bits.chunks(8).map(|c| c.iter().fold(0u8, |v, &b| (v << 1) | b) << (8 - c.len())).collect()
}

/// A message from its raw fields (the standard's names and units: `type`,
/// `mmsi`, `lat` in 1/10000 minute, ...) as payload octets, MSB first.
#[pyfunction]
fn ais_encode<'py>(py: Python<'py>, fields: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyBytes>> {
    let bits = msg::encode(&raw_from(fields)?).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &octets_of(&bits)))
}

/// A payload's fields as the decoder reports them (msg::fields), or None
/// when it is no message AIS sends.
#[pyfunction]
fn ais_parse<'py>(py: Python<'py>, payload: &[u8]) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Some(m) = msg::parse(&bits_of(payload)) else {
        return Ok(None);
    };
    Ok(Some(crate::py::fields_py(py, &msg::fields(&m))?))
}

/// The `!AIVDM` sentences for a payload heard on `channel`.
#[pyfunction]
#[pyo3(signature = (payload, channel="A", seq=0))]
fn ais_nmea(payload: &[u8], channel: &str, seq: u8) -> PyResult<Vec<String>> {
    let ch = Channel::parse(channel).ok_or_else(|| PyValueError::new_err("channel is A or B"))?;
    Ok(msg::nmea(&bits_of(payload), ch.code(), seq))
}

/// Complex64 IQ (as bytes) of AIS bursts over noise: `bursts` is a list of
/// dicts {"t": s, "channel": "A"|"B", "payload": bytes (ais_encode's) or
/// "fields": dict, "level_db": 0, "offset_hz": 0}. 162.000 MHz sits at
/// `centre_hz` from the IQ's centre; `snr_db` is a 0 dB burst's Eb/N0.
#[pyfunction]
#[pyo3(signature = (bursts, rate=96_000.0, duration_s=1.0, centre_hz=0.0, snr_db=None, seed=1))]
fn testsig_ais<'py>(
    py: Python<'py>,
    bursts: &Bound<'py, PyList>,
    rate: f64,
    duration_s: f64,
    centre_hz: f64,
    snr_db: Option<f64>,
    seed: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    if !(rate >= 96_000.0 && rate <= 20e6 && duration_s > 0.0 && duration_s * rate <= 2e8) {
        return Err(PyValueError::new_err("rate 96 kS/s to 20 MS/s, at most 2e8 samples"));
    }
    let mut list = Vec::new();
    for b in bursts.iter() {
        let d = b.cast::<PyDict>()?;
        let get = |k: &str| d.get_item(k);
        let bits = if let Some(p) = get("payload")? {
            bits_of(&p.extract::<Vec<u8>>()?)
        } else if let Some(f) = get("fields")? {
            msg::encode(&raw_from(f.cast::<PyDict>()?)?).map_err(PyValueError::new_err)?
        } else {
            return Err(PyValueError::new_err("each burst needs payload or fields"));
        };
        let ch: String = get("channel")?.map(|c| c.extract()).transpose()?.unwrap_or_else(|| "A".into());
        let mut burst = Burst::new(
            get("t")?.map(|t| t.extract()).transpose()?.unwrap_or(0.0),
            Channel::parse(&ch).ok_or_else(|| PyValueError::new_err("channel is A or B"))?,
            bits,
        );
        burst.level_db = get("level_db")?.map(|t| t.extract()).transpose()?.unwrap_or(0.0);
        burst.offset_hz = get("offset_hz")?.map(|t| t.extract()).transpose()?.unwrap_or(0.0);
        list.push(burst);
    }
    let scene = Scene { rate, duration_s, centre_hz, snr_db, seed, bursts: list };
    let bytes: Vec<u8> = py.detach(|| {
        scene_iq(&scene).iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect()
    });
    Ok(PyBytes::new(py, &bytes))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ais_encode, m)?)?;
    m.add_function(wrap_pyfunction!(ais_parse, m)?)?;
    m.add_function(wrap_pyfunction!(ais_nmea, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_ais, m)?)?;
    Ok(())
}
