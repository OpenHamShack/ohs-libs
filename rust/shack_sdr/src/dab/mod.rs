//! DAB and DAB+ (ETSI EN 300 401, TS 102 563): the `dab` decoder
//! (decoder.rs) and its parts, in the order a signal meets them: ofdm,
//! fic (with codes, the service information's code tables), msc, audio
//! (MP2 through crate::mp2, DAB+ super frames, faad2 at run time), pad
//! (the dynamic label, DL Plus and X-PAD data groups); for data, packet
//! (packet mode), mot (MOT objects: SlideShow's pictures, SPI's carousel)
//! and spi (logos and the programme guide); tables, coding and rs
//! underneath; tx, the transmitter model the tests use.

pub use decoder::{survey, survey_details, Dab, Survey, INFO};

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
pub mod spi;
pub mod tables;
pub mod tii;
pub mod tx;
