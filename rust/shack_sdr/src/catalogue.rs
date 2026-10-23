//! Every decoder kind this build has, with a way to make one. The Python
//! face adds the Python-backed kinds (shack_cw, ohsdigi's slot modes) to
//! the same list (python/shack_sdr/catalogue.py), so an app and its clients
//! see one catalogue whichever library decodes.

use crate::decoder::{Decoder, Info};

#[derive(Clone, Copy)]
pub struct Kind {
    pub info: &'static Info,
    pub make: fn() -> Box<dyn Decoder>,
}

fn rtty() -> Box<dyn Decoder> {
    Box::new(crate::rtty::Rtty::new())
}

fn psk() -> Box<dyn Decoder> {
    Box::new(crate::psk::Psk::new())
}

fn dab() -> Box<dyn Decoder> {
    Box::new(crate::dab::Dab::new())
}

fn sstv() -> Box<dyn Decoder> {
    Box::new(crate::sstv::Sstv::new())
}

fn wfm() -> Box<dyn Decoder> {
    Box::new(crate::wfm::decoder::Wfm::new())
}

/// The decoders users can start. Test decoders (host::testing) are added
/// only to a host made for tests.
pub fn kinds() -> Vec<Kind> {
    vec![
        Kind { info: &crate::rtty::INFO, make: rtty },
        Kind { info: &crate::psk::INFO, make: psk },
        Kind { info: &crate::dab::INFO, make: dab },
        Kind { info: &crate::wfm::decoder::INFO, make: wfm },
        Kind { info: &crate::sstv::INFO, make: sstv },
    ]
}

pub fn find(id: &str, testing: bool) -> Option<Kind> {
    kinds()
        .into_iter()
        .chain(if testing { crate::host::testing::kinds() } else { Vec::new() })
        .find(|k| k.info.id == id)
}
