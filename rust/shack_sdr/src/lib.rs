//! shack_sdr: OpenHamShack's SDR decoders and the host that runs them
//! (openhamshack docs/RADIO-SDR-PLAN.md). The crate has no transmit path:
//! nothing here keys, or imports anything that could.
//!
//! - `decoder`: the one interface every decoder implements (§4.1).
//! - `host`: sessions on worker threads, bounded rings, the CPU budget (§3.4-3.5).
//! - `catalogue`: every decoder kind this build has.
//! - `rtty`: the first decoder (§8.1 item 6).
//! - `psk`: PSK31, PSK63 and PSK125 (§8.5 M2).
//! - `codecs`: the runtime codec loader (§5).
//! - `testsig`: test signals and channel models, bit-identical everywhere (§7.1).
//! - `dsp`: the small pieces the decoders share.
//! - `wfm`: broadcast FM: stereo, RDS and the band scanner's many-carrier RDS.
//! - `sstv`: SSTV pictures, every common mode, with the test transmitter.
//! - `ais`: ships' AIS, both channels, with the test transmitter.
//! - `dmr`: DMR Tier II, both slots; voice only through the user's mbelib.
//! - `adsb`: aircraft on 1090 MHz (ADS-B and Mode S), with the test transmitter.

pub mod adsb;
pub mod ais;
pub mod catalogue;
pub mod dab;
pub mod dmr;
pub mod codecs;
pub mod cpu;
pub mod decoder;
pub mod dsp;
pub mod host;
pub mod mp2;
pub mod psk;
pub mod rtty;
pub mod sstv;
pub mod testsig;
pub mod wfm;

#[cfg(feature = "python")]
mod py;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
