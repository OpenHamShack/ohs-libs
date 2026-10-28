//! The 264-bit burst and the 24-bit CACH around it (TS 102 361-1 §4-6, §9):
//! where each field sits, the sync patterns, the slot type and the EMB.
//!
//! A burst is 132 symbols: 108 bits of payload, 48 in the middle (a sync
//! pattern, or in a voice burst without sync the EMB's two halves around
//! 32 bits of embedded signalling), and 108 more. A data burst's middle
//! 108 + 108 bits are 98 info bits, the slot type's first 10, (sync), its
//! last 10 and 98 more info bits. A repeater (BS) sends a 12-symbol CACH
//! before every burst, saying which slot comes next; a radio's own
//! transmissions (MS) have a guard time there instead.

use super::fec::{golay20_decode, golay20_encode, qr16_decode, qr16_encode, H7_4};

pub const BURST_BITS: usize = 264;
pub const BURST_SYMBOLS: usize = 132;
pub const CACH_BITS: usize = 24;
pub const CACH_SYMBOLS: usize = 12;
/// A slot is 30 ms: the CACH and the burst, 144 symbols at 4800 Bd.
pub const SLOT_SYMBOLS: usize = 144;
/// The sync's first symbol in the burst.
pub const SYNC_SYMBOL: usize = 54;
pub const SYNC_SYMBOLS: usize = 24;

pub type Burst = [u8; BURST_BITS];

/// What a sync pattern says: who sent the burst, and whether it is voice.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SyncKind {
    /// A repeater's outbound.
    Bs,
    /// A radio's, through a repeater or on a simplex channel.
    Ms,
    /// TDMA direct mode, which names its own timeslot (1 or 2).
    Direct(u8),
}

impl SyncKind {
    pub fn code(self) -> &'static str {
        match self {
            SyncKind::Bs => "bs",
            SyncKind::Ms => "ms",
            SyncKind::Direct(_) => "direct",
        }
    }
}

/// The voice sync of each kind; each data sync is its exact inverse
/// (every dibit 01 <-> 11, symbol +3 <-> -3), so one correlator finds both.
pub const SYNCS: [(SyncKind, u64); 4] = [
    (SyncKind::Bs, 0x755F_D7DF_75F7),
    (SyncKind::Ms, 0x7F7D_5DD5_7DFD),
    (SyncKind::Direct(1), 0x5D57_7F77_57FF),
    (SyncKind::Direct(2), 0x7DFF_D5F5_5D5F),
];

/// A pattern's 24 symbols, +3 or -3.
pub fn sync_symbols(pattern: u64) -> [f32; SYNC_SYMBOLS] {
    let mut s = [0f32; SYNC_SYMBOLS];
    for (i, v) in s.iter_mut().enumerate() {
        let dibit = (pattern >> (46 - 2 * i)) & 3;
        *v = dibit_to_symbol(dibit as u8);
    }
    s
}

/// The voice or data sync pattern of a kind.
pub fn sync_pattern(kind: SyncKind, voice: bool) -> u64 {
    let v = SYNCS.iter().find(|(k, _)| *k == kind).expect("every kind has a sync").1;
    if voice {
        v
    } else {
        // Flip the first bit of every dibit: 01 <-> 11, 00 <-> 10.
        v ^ 0xAAAA_AAAA_AAAA
    }
}

/// 4FSK: dibit 01 is +3 (+1944 Hz), 00 +1, 10 -1, 11 -3.
pub fn dibit_to_symbol(d: u8) -> f32 {
    match d & 3 {
        0b01 => 3.0,
        0b00 => 1.0,
        0b10 => -1.0,
        _ => -3.0,
    }
}

/// The nearest symbol's dibit.
pub fn symbol_to_dibit(s: f32) -> u8 {
    if s >= 2.0 {
        0b01
    } else if s >= 0.0 {
        0b00
    } else if s >= -2.0 {
        0b10
    } else {
        0b11
    }
}

/// A data burst's type (the slot type's low four bits).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DataType {
    PiHeader = 0,
    VoiceLcHeader = 1,
    TerminatorLc = 2,
    Csbk = 3,
    MbcHeader = 4,
    MbcContinuation = 5,
    DataHeader = 6,
    Rate12Data = 7,
    Rate34Data = 8,
    Idle = 9,
    Rate1Data = 10,
}

impl DataType {
    pub fn from(n: u8) -> Option<DataType> {
        use DataType::*;
        Some(match n {
            0 => PiHeader,
            1 => VoiceLcHeader,
            2 => TerminatorLc,
            3 => Csbk,
            4 => MbcHeader,
            5 => MbcContinuation,
            6 => DataHeader,
            7 => Rate12Data,
            8 => Rate34Data,
            9 => Idle,
            10 => Rate1Data,
            _ => return None,
        })
    }
}

fn get(bits: &[u8], range: std::ops::Range<usize>) -> u32 {
    bits[range].iter().fold(0, |a, &b| (a << 1) | (b as u32 & 1))
}

fn put(bits: &mut [u8], at: usize, n: usize, v: u64) {
    for i in 0..n {
        bits[at + i] = ((v >> (n - 1 - i)) & 1) as u8;
    }
}

/// The 48 bits in the middle of a burst as an integer.
pub fn middle(b: &Burst) -> u64 {
    b[108..156].iter().fold(0u64, |a, &x| (a << 1) | x as u64)
}

pub fn put_sync(b: &mut Burst, pattern: u64) {
    put(b, 108, 48, pattern);
}

/// The slot type: (colour code, data type, bits corrected), or None.
pub fn slot_type(b: &Burst) -> Option<(u8, u8, u32)> {
    let w = (get(b, 98..108) << 10) | get(b, 156..166);
    let (d, e) = golay20_decode(w)?;
    Some((d >> 4, d & 0xF, e))
}

pub fn put_slot_type(b: &mut Burst, cc: u8, dt: u8) {
    let w = golay20_encode(((cc & 0xF) << 4) | (dt & 0xF)) as u64;
    put(b, 98, 10, w >> 10);
    put(b, 156, 10, w & 0x3FF);
}

/// A data burst's 196 info bits.
pub fn info(b: &Burst) -> [u8; 196] {
    let mut out = [0u8; 196];
    out[..98].copy_from_slice(&b[..98]);
    out[98..].copy_from_slice(&b[166..264]);
    out
}

pub fn put_info(b: &mut Burst, info: &[u8; 196]) {
    b[..98].copy_from_slice(&info[..98]);
    b[166..264].copy_from_slice(&info[98..]);
}

/// A voice burst's EMB (B to F): colour code, privacy indicator, LCSS.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Emb {
    pub cc: u8,
    pub pi: bool,
    /// Link control start/stop: 0 a single fragment (or none), 1 first, 3
    /// continuation, 2 last.
    pub lcss: u8,
}

pub fn emb(b: &Burst) -> Option<(Emb, u32)> {
    let w = (get(b, 108..116) << 8) | get(b, 148..156);
    let (d, e) = qr16_decode(w as u16)?;
    Some((Emb { cc: d >> 3, pi: d & 4 != 0, lcss: d & 3 }, e))
}

pub fn put_emb(b: &mut Burst, e: Emb) {
    let w = qr16_encode(((e.cc & 0xF) << 3) | ((e.pi as u8) << 2) | (e.lcss & 3)) as u64;
    put(b, 108, 8, w >> 8);
    put(b, 148, 8, w & 0xFF);
}

/// The 32 bits of embedded signalling between the EMB's halves.
pub fn embedded(b: &Burst) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(&b[116..148]);
    out
}

pub fn put_embedded(b: &mut Burst, bits: &[u8; 32]) {
    b[116..148].copy_from_slice(bits);
}

/// A CACH's TACT: (access type, timeslot of the burst that follows: 1 or
/// 2, LCSS). TC 0 announces slot 1.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tact {
    pub at: bool,
    pub slot: u8,
    pub lcss: u8,
}

/// The TACT's bits sit at these places among the CACH's 24; the short LC
/// fragment fills the other 17.
pub const TACT_POS: [usize; 7] = [0, 4, 8, 12, 14, 18, 22];

pub fn tact(cach: &[u8]) -> Option<Tact> {
    let mut w: Vec<u8> = TACT_POS.iter().map(|&p| cach[p]).collect();
    H7_4.decode(&mut w)?;
    Some(Tact { at: w[0] != 0, slot: 1 + w[1], lcss: (w[2] << 1) | w[3] })
}

/// A CACH announcing `t`, its short LC fragment bits zero (an idle
/// repeater's null short LC).
pub fn cach(t: Tact) -> [u8; CACH_BITS] {
    let mut w = [t.at as u8, t.slot.saturating_sub(1) & 1, (t.lcss >> 1) & 1, t.lcss & 1, 0, 0, 0];
    H7_4.encode(&mut w);
    let mut c = [0u8; CACH_BITS];
    for (i, &p) in TACT_POS.iter().enumerate() {
        c[p] = w[i];
    }
    c
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn data_syncs_are_the_voice_syncs_inverted() {
        // TS 102 361-1 Table 9.2's data patterns, as MMDVMHost has them.
        assert_eq!(sync_pattern(SyncKind::Bs, false), 0xDFF5_7D75_DF5D);
        assert_eq!(sync_pattern(SyncKind::Ms, false), 0xD5D7_F77F_D757);
        assert_eq!(sync_pattern(SyncKind::Direct(1), false), 0xF7FD_D5DD_FD55);
        assert_eq!(sync_pattern(SyncKind::Direct(2), false), 0xD755_7F5F_F7F5);
        let v = sync_symbols(sync_pattern(SyncKind::Ms, true));
        let d = sync_symbols(sync_pattern(SyncKind::Ms, false));
        assert!(v.iter().zip(&d).all(|(a, b)| a == &-b));
        // The MS voice sync's first symbols (MMDVM's symbol table).
        assert_eq!(&v[..4], &[3.0, -3.0, -3.0, -3.0]);
    }

    #[test]
    fn cach_tact_matches_the_repeater_firmware_layout() {
        // MMDVM's DMRTX for slot 2 (TC 1), first fragment (LCSS 01), AT 0:
        // h0 = at^tc^ls1 = 1, h1 = tc^ls1^ls0 = 0, h2 = at^tc^ls0 = 0.
        let c = cach(Tact { at: false, slot: 2, lcss: 1 });
        let bytes: Vec<u8> = c.chunks(8).map(|b| b.iter().fold(0, |a, &x| (a << 1) | x)).collect();
        assert_eq!(bytes, vec![0x08, 0x0A, 0x00]);
        assert_eq!(tact(&c), Some(Tact { at: false, slot: 2, lcss: 1 }));
        let mut bad = c;
        bad[12] ^= 1;
        assert_eq!(tact(&bad), Some(Tact { at: false, slot: 2, lcss: 1 }));
    }

    #[test]
    fn slot_type_and_emb_survive_errors() {
        let mut b = [0u8; BURST_BITS];
        put_slot_type(&mut b, 7, 1);
        b[99] ^= 1;
        b[160] ^= 1;
        assert_eq!(slot_type(&b), Some((7, 1, 2)));
        let e = Emb { cc: 12, pi: true, lcss: 3 };
        put_emb(&mut b, e);
        b[110] ^= 1;
        assert_eq!(emb(&b), Some((e, 1)));
    }
}
