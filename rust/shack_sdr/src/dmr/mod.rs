//! DMR Tier II (ETSI TS 102 361): the protocol's coding layers.
//!
//! - `fec`, `bptc`: the channel codes; `burst`: the burst's fields;
//! - `lc`: link control, late entry, talker alias; `data`: packet data;
//! - `ambe`: AMBE+2 frames and their FEC (not the vocoder).

pub mod ambe;
pub mod bptc;
pub mod burst;
pub mod data;
pub mod fec;
pub mod lc;
