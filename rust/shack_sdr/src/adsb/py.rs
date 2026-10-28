//! ADS-B's part of `shack_sdr._native`: writing messages and keying them
//! for tests and for an app's simulator (Radio's simulated aircraft), and
//! reading one message without a session. The decoder itself is a Host
//! session like any other.

use super::msg::{self, Body, Es, Make};
use super::tx::{scene_iq, Reply, Scene};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

fn get<'py, T: FromPyObjectOwned<'py>>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Option<T>> {
    match d.get_item(k)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract().map_err(|_| PyValueError::new_err(format!("{k}: wrong type")))?)),
        _ => Ok(None),
    }
}

fn need<'py, T: FromPyObjectOwned<'py>>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<T> {
    get(d, k)?.ok_or_else(|| PyValueError::new_err(format!("{k} is needed")))
}

/// A message as bytes from its description: {"kind": "ident" | "airborne" |
/// "surface" | "velocity" | "airspeed" | "status" | "opstatus" | "allcall" |
/// "altitude" | "identity", "icao": int, ...} with the fields of
/// msg::Make in snake_case (`callsign`, `alt_ft`, `lat`, `lon`, `odd`,
/// `east_kn`, `north_kn`, `vrate_fpm`, `squawk`, ...).
#[pyfunction]
fn adsb_encode<'py>(py: Python<'py>, fields: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyBytes>> {
    let f = fields;
    let kind: String = need(f, "kind")?;
    let icao: u32 = need(f, "icao")?;
    let mk = match kind.as_str() {
        "ident" => Make::Ident { icao, tc: get(f, "tc")?.unwrap_or(4), ca: get(f, "ca")?.unwrap_or(3), callsign: need(f, "callsign")? },
        "airborne" => Make::Airborne { icao, tc: get(f, "tc")?.unwrap_or(11), alt_ft: get(f, "alt_ft")?, odd: get(f, "odd")?.unwrap_or(false), lat: need(f, "lat")?, lon: need(f, "lon")? },
        "surface" => Make::Surface { icao, tc: get(f, "tc")?.unwrap_or(7), speed_kn: get(f, "speed_kn")?, track: get(f, "track")?, odd: get(f, "odd")?.unwrap_or(false), lat: need(f, "lat")?, lon: need(f, "lon")? },
        "velocity" => Make::Velocity {
            icao,
            subtype: get(f, "subtype")?.unwrap_or(1),
            east_kn: need(f, "east_kn")?,
            north_kn: need(f, "north_kn")?,
            vrate_fpm: get(f, "vrate_fpm")?,
            baro: get(f, "baro")?.unwrap_or(true),
            geo_minus_baro_ft: get(f, "geo_minus_baro_ft")?,
        },
        "airspeed" => Make::Airspeed {
            icao,
            subtype: get(f, "subtype")?.unwrap_or(3),
            heading: get(f, "heading")?,
            airspeed_kn: get(f, "airspeed_kn")?,
            tas: get(f, "tas")?.unwrap_or(false),
            vrate_fpm: get(f, "vrate_fpm")?,
            baro: get(f, "baro")?.unwrap_or(true),
        },
        "status" => Make::Status { icao, emergency: get(f, "emergency")?.unwrap_or(0), squawk: need(f, "squawk")? },
        "opstatus" => Make::OpStatus { icao, version: get(f, "version")?.unwrap_or(2), surface: get(f, "surface")?.unwrap_or(false) },
        "allcall" => Make::AllCall { icao, ca: get(f, "ca")?.unwrap_or(5), iid: get(f, "iid")?.unwrap_or(0) },
        "altitude" => Make::Altitude { df: get(f, "df")?.unwrap_or(4), icao, fs: get(f, "fs")?.unwrap_or(0), alt_ft: get(f, "alt_ft")? },
        "identity" => Make::Identity { df: get(f, "df")?.unwrap_or(5), icao, fs: get(f, "fs")?.unwrap_or(0), squawk: need(f, "squawk")? },
        k => return Err(PyValueError::new_err(format!("no message kind {k}"))),
    };
    let b = msg::encode(&mk).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &b))
}

/// One message's fields - {"df", "icao" (hex), "tc", ...} - or None for a
/// format not read. The parity is reported (`remainder`), not judged:
/// whether to believe a message is the decoder's business.
#[pyfunction]
fn adsb_parse<'py>(py: Python<'py>, message: &[u8]) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Some(m) = msg::parse(message) else { return Ok(None) };
    let d = PyDict::new(py);
    d.set_item("df", m.df)?;
    d.set_item("icao", format!("{:06X}", m.icao))?;
    d.set_item("remainder", super::crc::remainder(message))?;
    match &m.body {
        Body::AllCall { ca, .. } => d.set_item("ca", ca)?,
        Body::Altitude { fs, alt_ft } => {
            d.set_item("fs", fs)?;
            d.set_item("altitude", alt_ft)?;
        }
        Body::Identity { fs, squawk } => {
            d.set_item("fs", fs)?;
            d.set_item("squawk", squawk)?;
        }
        Body::Es { tc, es, .. } => {
            d.set_item("tc", tc)?;
            match es {
                Es::Ident { category, callsign } => {
                    d.set_item("callsign", callsign)?;
                    d.set_item("category", category)?;
                }
                Es::Airborne { alt_ft, geo, cpr, .. } => {
                    d.set_item("altitude", alt_ft)?;
                    d.set_item("geo", geo)?;
                    d.set_item("cprOdd", cpr.odd)?;
                }
                Es::Surface { speed_kn, track, cpr } => {
                    d.set_item("gs", speed_kn)?;
                    d.set_item("track", track)?;
                    d.set_item("cprOdd", cpr.odd)?;
                }
                Es::Velocity(v) => {
                    d.set_item("gs", v.gs_kn)?;
                    d.set_item("track", v.track)?;
                    d.set_item("airspeed", v.airspeed_kn)?;
                    d.set_item("heading", v.heading)?;
                    d.set_item("vrate", v.vrate_fpm)?;
                }
                Es::Status { emergency, squawk } => {
                    d.set_item("emergency", emergency)?;
                    d.set_item("squawk", squawk)?;
                }
                Es::OpStatus { version, .. } => d.set_item("version", version)?,
                Es::Other => {}
            }
        }
    }
    Ok(Some(d))
}

/// Complex64 IQ (as bytes) of Mode S replies over noise: `replies` is a
/// list of dicts {"t": s, "payload": bytes (adsb_encode's), "level_db": 0,
/// "offset_hz": 0 (1090 MHz's offset from the IQ's centre plus the
/// transponder's error), "phase": 0 (turns)}. `snr_db` is a 0 dB pulse's
/// power over the noise in the sample bandwidth; `if_bw_hz` a receiver's
/// IF filter, or None.
#[pyfunction]
#[pyo3(signature = (replies, rate=2_400_000.0, duration_s=0.001, snr_db=None, if_bw_hz=None, seed=1))]
fn testsig_adsb<'py>(
    py: Python<'py>,
    replies: &Bound<'py, PyList>,
    rate: f64,
    duration_s: f64,
    snr_db: Option<f64>,
    if_bw_hz: Option<f64>,
    seed: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    if !(rate >= 2e6 && rate <= 20e6 && duration_s > 0.0 && duration_s * rate <= 2e8) {
        return Err(PyValueError::new_err("rate 2 to 20 MS/s, at most 2e8 samples"));
    }
    let mut list = Vec::new();
    for r in replies.iter() {
        let d = r.cast::<PyDict>()?;
        let payload: Vec<u8> = need(d, "payload")?;
        if payload.len() != 7 && payload.len() != 14 {
            return Err(PyValueError::new_err("a payload is 7 or 14 bytes"));
        }
        let mut reply = Reply::mode_s(get(d, "t")?.unwrap_or(0.0), payload);
        reply.level_db = get(d, "level_db")?.unwrap_or(0.0);
        reply.offset_hz = get(d, "offset_hz")?.unwrap_or(0.0);
        reply.phase = get(d, "phase")?.unwrap_or(0.0);
        list.push(reply);
    }
    let scene = Scene { rate, duration_s, replies: list, snr_db, if_bw_hz, seed };
    let bytes: Vec<u8> = py.detach(|| scene_iq(&scene).iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect());
    Ok(PyBytes::new(py, &bytes))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(adsb_encode, m)?)?;
    m.add_function(wrap_pyfunction!(adsb_parse, m)?)?;
    m.add_function(wrap_pyfunction!(testsig_adsb, m)?)?;
    Ok(())
}
