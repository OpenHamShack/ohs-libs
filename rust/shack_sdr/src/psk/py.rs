//! PSK's part of `shack_sdr._native`: the test transmitter and the text a
//! perfect receiver prints, for the golden-number capture
//! (tools/psk_golden.py), the Python tests and the wheel's smoke test. The
//! decoder itself is a Host session like any other.

use super::tx::{psk_audio, psk_iq, Interferer, PskCase, PskTx};
use crate::testsig::Watterson;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// A synthetic PSK test signal (psk/tx.rs) as float32 bytes (audio) or
/// complex64 bytes (iq=True). `fading` is None, "good", "moderate" or
/// "poor"; `interferer` is None or (offset_hz, level_db, baud, text).
#[pyfunction]
#[pyo3(signature = (text, baud=31.25, rate=12_000.0, centre=1000.0, drift=0.0, snr_db=None, fading=None,
                    interferer=None, lead_s=1.0, tail_s=2.0, seed=1, iq=false, clock_ppm=0.0,
                    preamble_s=1.0, postamble_s=0.5, imd_db=None))]
#[allow(clippy::too_many_arguments)]
fn testsig_psk<'py>(
    py: Python<'py>,
    text: &str,
    baud: f64,
    rate: f64,
    centre: f64,
    drift: f64,
    snr_db: Option<f64>,
    fading: Option<&str>,
    interferer: Option<(f64, f64, f64, String)>,
    lead_s: f64,
    tail_s: f64,
    seed: u64,
    iq: bool,
    clock_ppm: f64,
    preamble_s: f64,
    postamble_s: f64,
    imd_db: Option<f64>,
) -> PyResult<Bound<'py, PyBytes>> {
    let fading = match fading {
        None => None,
        Some("good") => Some(Watterson::GOOD),
        Some("moderate") => Some(Watterson::MODERATE),
        Some("poor") => Some(Watterson::POOR),
        Some(_) => return Err(PyValueError::new_err("fading is good, moderate or poor")),
    };
    if !(baud > 0.0 && rate >= 1000.0 && lead_s >= 0.0 && tail_s >= 0.0 && lead_s + tail_s < 3600.0) {
        return Err(PyValueError::new_err("baud > 0, rate >= 1 kHz, at most an hour"));
    }
    let case = PskCase {
        text: text.to_string(),
        tx: PskTx { baud, preamble_s, postamble_s, clock_ppm, imd_db },
        rate,
        centre,
        drift,
        snr_db,
        fading,
        interferer: interferer.map(|(offset_hz, level_db, baud, text)| Interferer { offset_hz, level_db, baud, text }),
        lead_s,
        tail_s,
        seed,
    };
    let bytes: Vec<u8> = py.detach(|| {
        if iq {
            psk_iq(&case).iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect()
        } else {
            psk_audio(&case).iter().flat_map(|x| x.to_le_bytes()).collect()
        }
    });
    Ok(PyBytes::new(py, &bytes))
}

/// What a perfect receiver prints for `text`.
#[pyfunction]
fn psk_truth(text: &str) -> String {
    super::varicode::truth(text)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(testsig_psk, m)?)?;
    m.add_function(wrap_pyfunction!(psk_truth, m)?)?;
    Ok(())
}
