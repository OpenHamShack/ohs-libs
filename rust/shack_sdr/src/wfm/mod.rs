//! Broadcast FM (RADIO-SDR-PLAN §8.5 M3, brought forward for Radio, Brian
//! 23/10/2026): stereo, RDS, and a band scanner's many-carrier RDS.
//!
//! - `receiver`: one station's channel IQ to 48 kHz stereo, the stereo
//!   state and RDS (the composite processor Radio plays through).
//! - `rdsdemod`: the 57 kHz subcarrier to data bits.
//! - `rds`: blocks, sync, correction, groups and stable fields; also the
//!   group encoder the test transmitter uses.
//! - `scan`: a wide capture to every station on the 100 kHz grid with its
//!   level, stereo flag, PI, PS, PTY (and RadioText if time allows).
//! - `tx`: the stereo and RDS test transmitter and channel models.
//! - `filt`: the few filters and the oscillator these need.

pub mod filt;
pub mod rds;
pub mod rdsdemod;
pub mod scan;
pub mod receiver;
pub mod tx;

pub use receiver::{Config, ConfigError, Status, WfmReceiver};
pub use scan::{scan, Found, ScanOptions, ScanReport};
