//! SSTV's part of `shack_sdr._native`: the mode table, the test
//! transmitter and test card, the score, and a reader for the PNGs the
//! decoder writes. python/shack_sdr/sstv.py wraps them (numpy in and out);
//! apps import that, never this. The decoder itself is a Host session
//! ("sstv"), as every decoder is.

use super::modes::{by_id, MODES};
use super::tx::{self, Channel};
use crate::testsig::Watterson;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

#[pyfunction]
fn sstv_modes<'py>(py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
    MODES
        .iter()
        .map(|m| {
            let d = PyDict::new(py);
            // No name: the app words the id (I18N rule 1), as for every code.
            d.set_item("id", m.id)?;
            d.set_item("vis", m.vis)?;
            d.set_item("width", m.width)?;
            d.set_item("height", m.height)?;
            d.set_item("lineMs", m.layout().line_ms)?;
            d.set_item("seconds", m.seconds())?;
            Ok(d)
        })
        .collect()
}

/// A picture sent in `mode` through a channel: {"samples": float32 bytes
/// (complex64 with iq=True, centred on 1900 Hz), "image": the RGB sent,
/// "width", "height"}. `image` defaults to the test card.
#[pyfunction]
#[pyo3(signature = (mode, image=None, rate=12_000.0, snr_db=None, offset_hz=0.0, clock_ppm=0.0, fading=None,
                    lead_s=0.5, tail_s=1.0, cut=0.0, seed=1, vis=true, iq=false))]
#[allow(clippy::too_many_arguments)]
fn testsig_sstv<'py>(
    py: Python<'py>,
    mode: &str,
    image: Option<Vec<u8>>,
    rate: f64,
    snr_db: Option<f64>,
    offset_hz: f64,
    clock_ppm: f64,
    fading: Option<&str>,
    lead_s: f64,
    tail_s: f64,
    cut: f64,
    seed: u64,
    vis: bool,
    iq: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let m = by_id(mode).ok_or_else(|| PyValueError::new_err("unknown mode"))?;
    let img = image.unwrap_or_else(|| tx::test_card(m.width, m.height));
    if img.len() != (m.width * m.height * 3) as usize {
        return Err(PyValueError::new_err("image is not width x height x 3"));
    }
    let fading = match fading {
        None => None,
        Some("good") => Some(Watterson::GOOD),
        Some("moderate") => Some(Watterson::MODERATE),
        Some("poor") => Some(Watterson::POOR),
        Some(_) => return Err(PyValueError::new_err("fading is good, moderate or poor")),
    };
    let ch = Channel { snr_db, offset_hz, clock_ppm, fading, lead_s, tail_s, cut, seed, vis };
    let bytes: Vec<u8> = py.detach(|| {
        if iq {
            tx::iq(m, &img, rate, &ch).iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect()
        } else {
            tx::audio(m, &img, rate, &ch).iter().flat_map(|x| x.to_le_bytes()).collect()
        }
    });
    let d = PyDict::new(py);
    d.set_item("samples", PyBytes::new(py, &bytes))?;
    d.set_item("image", PyBytes::new(py, &img))?;
    d.set_item("width", m.width)?;
    d.set_item("height", m.height)?;
    Ok(d)
}

#[pyfunction]
fn sstv_test_card<'py>(py: Python<'py>, width: u32, height: u32) -> Bound<'py, PyBytes> {
    PyBytes::new(py, &tx::test_card(width, height))
}

/// {"psnr", "rows", "rowOffset", "inSync", "maxShift", "meanShift"} for
/// `got` (RGB rows `width` wide) against `sent` (width x height).
#[pyfunction]
#[pyo3(signature = (sent, width, height, got, partial=false))]
fn sstv_score<'py>(
    py: Python<'py>,
    sent: Vec<u8>,
    width: u32,
    height: u32,
    got: Vec<u8>,
    partial: bool,
) -> PyResult<Bound<'py, PyDict>> {
    if sent.len() != (width * height * 3) as usize || got.len() % (width as usize * 3) != 0 {
        return Err(PyValueError::new_err("sizes"));
    }
    let s = py.detach(|| tx::score(&sent, width, height, &got, partial));
    let d = PyDict::new(py);
    d.set_item("psnr", s.psnr)?;
    d.set_item("rows", s.rows)?;
    d.set_item("rowOffset", s.row_offset)?;
    d.set_item("inSync", s.in_sync)?;
    d.set_item("maxShift", s.max_shift)?;
    d.set_item("meanShift", s.mean_shift)?;
    Ok(d)
}

/// (width, height, rgb bytes) of a PNG the decoder wrote, or None.
#[pyfunction]
fn sstv_png_rgb<'py>(py: Python<'py>, png: Vec<u8>) -> Option<(u32, u32, Bound<'py, PyBytes>)> {
    super::png::decode(&png).map(|(w, h, rgb)| (w, h, PyBytes::new(py, &rgb)))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sstv_modes, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_sstv, m)?)?;
    m.add_function(wrap_pyfunction!(sstv_test_card, m)?)?;
    m.add_function(wrap_pyfunction!(sstv_score, m)?)?;
    m.add_function(wrap_pyfunction!(sstv_png_rgb, m)?)?;
    Ok(())
}
