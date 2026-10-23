//! DAB and DAB+ (ETSI EN 300 401, TS 102 563): the `dab` decoder
//! (decoder.rs) and its parts, in the order a signal meets them: ofdm,
//! fic, msc, audio (MP2 through crate::mp2, DAB+ super frames, faad2 at
//! run time); tables, coding and rs underneath; tx, the transmitter model
//! the tests use.

pub use decoder::{survey, Dab, Survey, INFO};

pub mod audio;
pub mod codes;
pub mod coding;
pub mod decoder;
pub mod fic;
pub mod msc;
pub mod mot;
pub mod ofdm;
pub mod packet;
pub mod pad;
pub mod rs;
pub mod tables;
pub mod tii;
pub mod tx;
