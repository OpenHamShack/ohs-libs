//! DMR's part of `shack_sdr._native`: the test transmitter, for the Python
//! tests, the wheel's smoke test and an app's simulator (a DMR repeater in
//! Radio's simulated band). The decoder is a Host session like any other.

use super::tx::{dmr_iq, showcase, DmrCase, Item, Mode, Payload, Sms, VoiceCall};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

fn get<'py, T: FromPyObjectOwned<'py>>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Option<T>> {
    match d.get_item(k)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract::<T>().map_err(Into::into)?)),
        _ => Ok(None),
    }
}

fn item(d: &Bound<'_, PyDict>) -> PyResult<Item> {
    let kind: String = get(d, "kind")?.ok_or_else(|| PyValueError::new_err("each item has a kind: voice, sms or gap"))?;
    let src: u32 = get(d, "src")?.unwrap_or(1);
    let dst: u32 = get(d, "dst")?.unwrap_or(1);
    let group: bool = get(d, "group")?.unwrap_or(true);
    Ok(match kind.as_str() {
        "voice" => {
            let mut c = VoiceCall::new(src, dst, get(d, "superframes")?.unwrap_or(3));
            c.group = group;
            c.alias = get(d, "alias")?;
            c.encrypted = get(d, "encrypted")?.unwrap_or(false);
            c.emergency = get(d, "emergency")?.unwrap_or(false);
            c.no_header = get(d, "noHeader")?.unwrap_or(false);
            c.no_terminator = get(d, "noTerminator")?.unwrap_or(false);
            if get::<bool>(d, "silence")?.unwrap_or(false) {
                c.payload = Payload::Silence;
            } else if let Some(seed) = get::<u64>(d, "seed")? {
                c.payload = Payload::Random(seed);
            }
            Item::Voice(c)
        }
        "sms" => Item::Sms(Sms {
            src,
            dst,
            group,
            text: get(d, "text")?.unwrap_or_default(),
            rate34: get(d, "rate34")?.unwrap_or(false),
        }),
        "gap" => Item::Gap(get(d, "bursts")?.unwrap_or(1)),
        _ => return Err(PyValueError::new_err("kind is voice, sms or gap")),
    })
}

/// A DMR test signal (dmr/tx.rs) as complex64 bytes at `rate`. `slots` is
/// [slot 1's items, slot 2's items], each item a dict with `kind` "voice"
/// (`src`, `dst`, `group`, `superframes`, `alias`, `encrypted`,
/// `emergency`, `noHeader`, `noTerminator`, `silence`, `seed`), "sms"
/// (`src`, `dst`, `group`, `text`, `rate34`) or "gap" (`bursts`); None is
/// the showcase a simulator plays (tx::showcase). `mode` is "bs" (a
/// repeater), "ms" (one radio) or "direct".
#[pyfunction]
#[pyo3(signature = (rate=48_000.0, snr_db=None, offset_hz=0.0, mode="bs", cc=1, slots=None, lead=8, tail=8, seed=1))]
#[allow(clippy::too_many_arguments)]
fn testsig_dmr<'py>(
    py: Python<'py>,
    rate: f64,
    snr_db: Option<f64>,
    offset_hz: f64,
    mode: &str,
    cc: u8,
    slots: Option<Bound<'py, PyList>>,
    lead: usize,
    tail: usize,
    seed: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    let mode = match mode {
        "bs" => Mode::Bs,
        "ms" => Mode::Ms,
        "direct" => Mode::Direct,
        _ => return Err(PyValueError::new_err("mode is bs, ms or direct")),
    };
    if !(16_000.0..=10e6).contains(&rate) || cc > 15 || lead + tail > 100_000 {
        return Err(PyValueError::new_err("rate 16 kS/s to 10 MS/s, cc 0-15"));
    }
    let slots = match slots {
        None => showcase(),
        Some(l) => {
            if l.len() != 2 {
                return Err(PyValueError::new_err("slots is [slot 1's items, slot 2's items]"));
            }
            let mut s: [Vec<Item>; 2] = [Vec::new(), Vec::new()];
            for (i, sl) in l.iter().enumerate() {
                for it in sl.cast::<PyList>()?.iter() {
                    s[i].push(item(it.cast::<PyDict>()?)?);
                }
            }
            s
        }
    };
    let case = DmrCase { mode, cc, slots, lead, tail, rate, snr_db, offset_hz, seed };
    let bytes: Vec<u8> = py.detach(|| dmr_iq(&case).iq.iter().flat_map(|z| z.re.to_le_bytes().into_iter().chain(z.im.to_le_bytes())).collect());
    Ok(PyBytes::new(py, &bytes))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(testsig_dmr, m)?)?;
    Ok(())
}
