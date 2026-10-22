//! shack_sdr: OpenHamShack's SDR decoders and the host that runs them
//! (openhamshack docs/RADIO-SDR-PLAN.md). The crate has no transmit path:
//! nothing here keys, or imports anything that could.
//!
//! - `decoder`: the one interface every decoder implements (§4.1).
//! - `host`: sessions on worker threads, bounded rings, the CPU budget (§3.4-3.5).
//! - `catalogue`: every decoder kind this build has.
//! - `rtty`: the first decoder (§8.1 item 6).
//! - `codecs`: the runtime codec loader (§5).
//! - `testsig`: test signals and channel models, bit-identical everywhere (§7.1).
//! - `dsp`: the small pieces the decoders share.

pub mod catalogue;
pub mod codecs;
pub mod cpu;
pub mod decoder;
pub mod dsp;
pub mod host;
pub mod rtty;
pub mod testsig;

#[cfg(feature = "python")]
mod py;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
