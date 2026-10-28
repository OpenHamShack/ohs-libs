//! Mode S parity (ICAO Annex 10 Vol IV §3.1.2.3.3): a 24-bit CRC, generator
//! 0x1FFF409, over every bit before the last 24, which carry it.
//!
//! What the last 24 bits hold differs by format, and so does what "good"
//! means (msg.rs reads it):
//!
//! - **DF11, DF17, DF18 (PI):** the parity itself, XOR an interrogator code
//!   for DF11 (zero in a squitter). The remainder of the whole message is 0
//!   for a good DF17/18, and the interrogator code for a DF11.
//! - **DF0/4/5/16/20/21 (AP):** the parity XOR the aircraft's address, so
//!   the remainder *is* the address. Any 56 or 112 bits "decode" to some
//!   address: such a message is believed only when that address is an
//!   aircraft already heard on DF17 (mod.rs, track.rs).
//!
//! `syndrome(i)` is the remainder a single wrong bit `i` leaves in an
//! otherwise good 112-bit message, which is how one bit is corrected: a
//! DF17 whose remainder equals bit i's syndrome is good with bit i turned
//! over (mod.rs says when that is allowed).

pub const POLY: u32 = 0xFFF409;

/// The byte-at-a-time table: CRC of each byte value, top-aligned.
const TABLE: [u32; 256] = {
    let mut t = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = (i as u32) << 16;
        let mut k = 0;
        while k < 8 {
            c = if c & 0x80_0000 != 0 { (c << 1) ^ POLY } else { c << 1 };
            k += 1;
        }
        t[i] = c & 0xFF_FFFF;
        i += 1;
    }
    t
};

/// The parity of `data` (every byte before the parity field).
pub fn parity(data: &[u8]) -> u32 {
    let mut c = 0u32;
    for &b in data {
        c = ((c << 8) & 0xFF_FFFF) ^ TABLE[(((c >> 16) as u8) ^ b) as usize];
    }
    c
}

/// The remainder of a whole message (7 or 14 bytes): its parity XOR its
/// last 24 bits. 0 for a good DF17/18; the address for an AP format.
pub fn remainder(msg: &[u8]) -> u32 {
    let n = msg.len();
    let tail = ((msg[n - 3] as u32) << 16) | ((msg[n - 2] as u32) << 8) | msg[n - 1] as u32;
    parity(&msg[..n - 3]) ^ tail
}

/// The remainder bit `bit` (0 = the first sent) alone leaves in a
/// 112-bit message.
pub fn syndrome(bit: usize) -> u32 {
    let mut m = [0u8; 14];
    m[bit / 8] = 0x80 >> (bit % 8);
    remainder(&m)
}

/// Every single-bit syndrome of a 112-bit message, by bit.
pub fn syndromes() -> [u32; 112] {
    let mut s = [0u32; 112];
    for (i, v) in s.iter_mut().enumerate() {
        *v = syndrome(i);
    }
    s
}
