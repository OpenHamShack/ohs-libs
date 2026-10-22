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

/// The decoders users can start. Test decoders (host::testing) are added
/// only to a host made for tests.
pub fn kinds() -> Vec<Kind> {
    vec![Kind { info: &crate::rtty::INFO, make: rtty }]
}

pub fn find(id: &str, testing: bool) -> Option<Kind> {
    kinds()
        .into_iter()
        .chain(if testing { crate::host::testing::kinds() } else { Vec::new() })
        .find(|k| k.info.id == id)
}
