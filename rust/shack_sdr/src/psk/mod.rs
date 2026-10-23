//! PSK31, PSK63 and PSK125 (RADIO-SDR-PLAN §8.5 M2): differential BPSK
//! with Varicode, ported from openhamshack-nb `ohscore/modes/psk.py`.
//!
//! - `varicode`: the alphabet and its soft-decision decoder.
//! - `tx`: the test transmitter and channel the decoder is measured on.

pub mod tx;
pub mod varicode;

#[cfg(feature = "python")]
pub(crate) mod py;
