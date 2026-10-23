//! Varicode (G3PLX's PSK31 alphabet) and a soft-decision decoder for it.
//!
//! The table is fldigi's `varicodetab1` (src/psk/pskvaricode.cxx), which
//! for codes 0-127 is the alphabet Peter Martinez, G3PLX, published; 128-255
//! are fldigi's own extension for 8-bit text. It is copied from our own
//! openhamshack-nb `ohscore/modes/psk.py`, which transcribed it from fldigi
//! (GPL-3.0-or-later; the codes are facts of the protocol) and cross-checked
//! it against Wikipedia's "Varicode" article.
//!
//! Every code starts and ends with a 1 and has no "00" in it, so "00" can
//! only mean "end of character", and idle is a run of zeros.
//!
//! **Why a trellis, not "split on 00".** psk.py, like fldigi, cuts the hard
//! bits at each "00". One wrong bit then costs a character or two: a 1 read
//! as 0 beside another 0 splits a character in two, a 0 read as 1 in a
//! separator joins two. Here the bits arrive as log-likelihoods and the
//! decoder finds the most likely sequence of characters (and idle) that
//! could have been sent, by the Viterbi algorithm on the alphabet's own
//! state machine: the states are the prefixes of the codes, so a path can
//! only ever spell valid characters, and a weak bit is settled by what the
//! bits around it make likely. Characters nobody types (controls other than
//! CR, LF and backspace, and fldigi's 128-255) carry a prior against them, so
//! noise that happens to spell one loses to the ordinary character it
//! nearly is. Decisions come out `depth` bits late, when the paths have
//! merged.

use std::sync::OnceLock;

/// fldigi's varicodetab1: index = Latin-1 code point.
pub const TABLE: [&str; 256] = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011", "1101011111", // 0-5
    "1011101111", "1011111101", "1011111111", "11101111", "11101", "1101101111", // 6-11
    "1011011101", "11111", "1101110101", "1110101011", "1011110111", "1011110101", // 12-17
    "1110101101", "1110101111", "1101011011", "1101101011", "1101101101", "1101010111", // 18-23
    "1101111011", "1101111101", "1110110111", "1101010101", "1101011101", "1110111011", // 24-29
    "1011111011", "1101111111", "1", "111111111", "101011111", "111110101", // 30-35
    "111011011", "1011010101", "1010111011", "101111111", "11111011", "11110111", // 36-41
    "101101111", "111011111", "1110101", "110101", "1010111", "110101111", // 42-47
    "10110111", "10111101", "11101101", "11111111", "101110111", "101011011", // 48-53
    "101101011", "110101101", "110101011", "110110111", "11110101", "110111101", // 54-59
    "111101101", "1010101", "111010111", "1010101111", "1010111101", "1111101", // 60-65
    "11101011", "10101101", "10110101", "1110111", "11011011", "11111101", // 66-71
    "101010101", "1111111", "111111101", "101111101", "11010111", "10111011", // 72-77
    "11011101", "10101011", "11010101", "111011101", "10101111", "1101111", // 78-83
    "1101101", "101010111", "110110101", "101011101", "101110101", "101111011", // 84-89
    "1010101101", "111110111", "111101111", "111111011", "1010111111", "101101101", // 90-95
    "1011011111", "1011", "1011111", "101111", "101101", "11", // 96-101
    "111101", "1011011", "101011", "1101", "111101011", "10111111", // 102-107
    "11011", "111011", "1111", "111", "111111", "110111111", // 108-113
    "10101", "10111", "101", "110111", "1111011", "1101011", // 114-119
    "11011111", "1011101", "111010101", "1010110111", "110111011", "1010110101", // 120-125
    "1011010111", "1110110101", "1110111101", "1110111111", "1111010101", "1111010111", // 126-131
    "1111011011", "1111011101", "1111011111", "1111101011", "1111101101", "1111101111", // 132-137
    "1111110101", "1111110111", "1111111011", "1111111101", "1111111111", "10101010101", // 138-143
    "10101010111", "10101011011", "10101011101", "10101011111", "10101101011", "10101101101", // 144-149
    "10101101111", "10101110101", "10101110111", "10101111011", "10101111101", "10101111111", // 150-155
    "10110101011", "10110101101", "10110101111", "10110110101", "10110110111", "10110111011", // 156-161
    "10110111101", "10110111111", "10111010101", "10111010111", "10111011011", "10111011101", // 162-167
    "10111011111", "10111101011", "10111101101", "10111101111", "10111110101", "10111110111", // 168-173
    "10111111011", "10111111101", "10111111111", "11010101011", "11010101101", "11010101111", // 174-179
    "11010110101", "11010110111", "11010111011", "11010111101", "11010111111", "11011010101", // 180-185
    "11011010111", "11011011011", "11011011101", "11011011111", "11011101011", "11011101101", // 186-191
    "11011101111", "11011110101", "11011110111", "11011111011", "11011111101", "11011111111", // 192-197
    "11101010101", "11101010111", "11101011011", "11101011101", "11101011111", "11101101011", // 198-203
    "11101101101", "11101101111", "11101110101", "11101110111", "11101111011", "11101111101", // 204-209
    "11101111111", "11110101011", "11110101101", "11110101111", "11110110101", "11110110111", // 210-215
    "11110111011", "11110111101", "11110111111", "11111010101", "11111010111", "11111011011", // 216-221
    "11111011101", "11111011111", "11111101011", "11111101101", "11111101111", "11111110101", // 222-227
    "11111110111", "11111111011", "11111111101", "11111111111", "101010101011", "101010101101", // 228-233
    "101010101111", "101010110101", "101010110111", "101010111011", "101010111101", "101010111111", // 234-239
    "101011010101", "101011010111", "101011011011", "101011011101", "101011011111", "101011101011", // 240-245
    "101011101101", "101011101111", "101011110101", "101011110111", "101011111011", "101011111101", // 246-251
    "101011111111", "101101010101", "101101010111", "101101011011", // 252-255
];

/// The code for a character, if it has one (Latin-1).
pub fn code(c: char) -> Option<&'static str> {
    let i = c as u32;
    (i < 256).then(|| TABLE[i as usize])
}

/// The bits a transmitter sends for `text`: each character's code and "00".
/// Characters beyond Latin-1 are skipped, as psk.py's `_text_to_bits` does.
pub fn bits(text: &str) -> Vec<u8> {
    let mut out = Vec::new();
    for c in text.chars() {
        if let Some(code) = code(c) {
            out.extend(code.bytes().map(|b| b - b'0'));
            out.extend([0, 0]);
        }
    }
    out
}

/// What a perfect receiver prints for `text`: the characters that have a code.
pub fn truth(text: &str) -> String {
    text.chars().filter(|c| code(*c).is_some()).collect()
}

/// The log prior (nats) of a character: 0 for what people type, less for
/// controls nobody sends and fldigi's 8-bit extension. Small enough that a
/// clean signal's bits always win; large enough that noise rarely spells
/// one. Measured on the sweep (tests/psk.rs).
fn prior(c: u8) -> f32 {
    match c {
        b'\n' | b'\r' | 8 | 32..=126 => 0.0,
        128..=255 => -6.0,
        _ => -5.0,
    }
}

/// The alphabet's state machine, built once.
struct Machine {
    /// next[s][bit]: the state a bit leads to, or NONE.
    next: Vec<[u16; 2]>,
    /// The character a state ending in "0" completes on one more 0.
    done: Vec<Option<u8>>,
}

const NONE: u16 = u16::MAX;
/// State 0: idle, between characters.
const IDLE: u16 = 0;

fn machine() -> &'static Machine {
    static M: OnceLock<Machine> = OnceLock::new();
    M.get_or_init(|| {
        // States are the prefixes of every code and of every code + "0".
        let mut names: Vec<String> = vec![String::new()];
        let mut index = std::collections::HashMap::new();
        index.insert(String::new(), IDLE);
        for code in TABLE {
            let full = format!("{code}0");
            for n in 1..=full.len() {
                let p = &full[..n];
                if !index.contains_key(p) {
                    index.insert(p.to_string(), names.len() as u16);
                    names.push(p.to_string());
                }
            }
        }
        let by_code: std::collections::HashMap<&str, u8> = TABLE.iter().enumerate().map(|(i, c)| (*c, i as u8)).collect();
        let mut next = vec![[NONE; 2]; names.len()];
        let mut done = vec![None; names.len()];
        for (s, name) in names.iter().enumerate() {
            if s == IDLE as usize {
                next[s] = [IDLE, index["1"]];
                continue;
            }
            for b in 0..2 {
                let n = format!("{name}{b}");
                if let Some(&t) = index.get(&n) {
                    next[s][b] = t;
                }
            }
            if let Some(stem) = name.strip_suffix('0') {
                if let Some(&c) = by_code.get(stem) {
                    done[s] = Some(c);
                    next[s][0] = IDLE;
                }
            }
        }
        Machine { next, done }
    })
}

/// A character decided, with the bit number of its first bit.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Decided {
    pub ch: u8,
    pub first_bit: u64,
}

/// The soft-decision Varicode decoder: feed it one log-likelihood per bit
/// (positive: a 1, no phase change), get characters `depth` bits later.
pub struct Viterbi {
    metric: Vec<f32>,
    scratch: Vec<f32>,
    /// Back pointers, `depth` rows of one per state, a ring.
    back: Vec<u16>,
    depth: usize,
    /// Bits fed so far.
    n: u64,
}

impl Viterbi {
    pub fn new(depth: usize) -> Self {
        let s = machine().next.len();
        Viterbi { metric: vec![0.0; s], scratch: vec![0.0; s], back: vec![IDLE; s * depth], depth, n: 0 }
    }

    pub fn reset(&mut self) {
        self.metric.iter_mut().for_each(|m| *m = 0.0);
        self.back.iter_mut().for_each(|b| *b = IDLE);
        self.n = 0;
    }

    pub fn bits_fed(&self) -> u64 {
        self.n
    }

    /// One bit's log-likelihood ratio, ln P(1)/P(0). Returns the character
    /// (if any) whose last bit was `depth` bits ago on the best path.
    pub fn push(&mut self, llr: f32) -> Option<Decided> {
        let m = machine();
        let ns = m.next.len();
        let half = 0.5 * llr.clamp(-40.0, 40.0);
        let row = (self.n % self.depth as u64) as usize * ns;
        self.scratch.iter_mut().for_each(|x| *x = f32::NEG_INFINITY);
        for s in 0..ns {
            let base = self.metric[s];
            if base == f32::NEG_INFINITY {
                continue;
            }
            for (b, gain) in [(0usize, -half), (1, half)] {
                let t = m.next[s][b];
                if t == NONE {
                    continue;
                }
                let mut cand = base + gain;
                if t == IDLE && s != IDLE as usize {
                    cand += prior(m.done[s].unwrap());
                }
                if cand > self.scratch[t as usize] {
                    self.scratch[t as usize] = cand;
                    self.back[row + t as usize] = s as u16;
                }
            }
        }
        let best = self.scratch.iter().copied().fold(f32::NEG_INFINITY, f32::max);
        for (m, s) in self.metric.iter_mut().zip(&self.scratch) {
            *m = s - best;
        }
        self.n += 1;
        if self.n < self.depth as u64 {
            return None;
        }
        // Trace the best path back to the oldest step in the ring.
        let mut state = self.metric.iter().position(|x| *x == 0.0).unwrap() as u16;
        let mut step = self.n - 1;
        let mut prev = state;
        for _ in 0..self.depth {
            let r = (step % self.depth as u64) as usize * ns;
            prev = self.back[r + state as usize];
            if step + 1 == self.n - self.depth as u64 + 1 {
                break;
            }
            state = prev;
            step -= 1;
        }
        // The oldest transition: prev -> state at bit `step`.
        if state == IDLE && prev != IDLE {
            let ch = m.done[prev as usize].unwrap();
            let len = TABLE[ch as usize].len() as u64;
            return Some(Decided { ch, first_bit: step.saturating_sub(len + 1) });
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_table_is_g3plx_s() {
        // Fixed points that psk.py's tests check against two sources.
        for (c, want) in [('e', "11"), (' ', "1"), ('a', "1011"), ('t', "101"), ('E', "1110111"), ('z', "111010101")] {
            assert_eq!(code(c), Some(want), "{c}");
        }
        let mut seen = std::collections::HashSet::new();
        for c in TABLE {
            assert!(!c.contains("00") && c.ends_with('1') && c.starts_with('1'), "{c}");
            assert!(seen.insert(c));
        }
    }

    fn llrs(bits: &[u8], strength: f32) -> Vec<f32> {
        bits.iter().map(|b| if *b == 1 { strength } else { -strength }).collect()
    }

    fn decode(l: &[f32], depth: usize) -> Vec<Decided> {
        let mut v = Viterbi::new(depth);
        l.iter().filter_map(|x| v.push(*x)).collect()
    }

    #[test]
    fn clean_bits_spell_the_text_with_each_first_bit() {
        let text = "CQ de GM4BRI\n";
        let mut b = vec![0u8; 10];
        b.extend(bits(text));
        b.extend(vec![0u8; 40]);
        let got = decode(&llrs(&b, 4.0), 32);
        let s: String = got.iter().map(|d| d.ch as char).collect();
        assert_eq!(s, text);
        // 'C' starts after the ten idle bits; 'Q' after C's code and "00".
        assert_eq!(got[0].first_bit, 10);
        assert_eq!(got[1].first_bit, 10 + code('C').unwrap().len() as u64 + 2);
    }

    #[test]
    fn a_weak_wrong_bit_is_outvoted_by_the_alphabet() {
        // "an": 'a' 1011, "00", 'n' 1111. A separator bit read weakly as 1
        // joins them into 1011101111, a code that exists (ACK) - so cutting
        // at "00" prints one control character for two letters. Nobody
        // sends ACK in PSK31 text: its prior makes the weak bit the likelier
        // mistake. (With every character equally likely the trellis would
        // gain nothing here: nearly every bit pattern spells something.)
        let mut b = vec![0u8; 6];
        b.extend(bits("an "));
        b.extend(vec![0u8; 40]);
        let mut l = llrs(&b, 4.0);
        assert_eq!(b[10], 0);
        l[10] = 0.5;
        let s: String = decode(&l, 32).iter().map(|d| d.ch as char).collect();
        assert_eq!(s, "an ");
        // Cut at "00" as psk.py does, it is the control character.
        let hard: String = l.iter().map(|x| if *x > 0.0 { '1' } else { '0' }).collect();
        let cut: String = hard
            .split("00")
            .map(|p| p.trim_start_matches('0'))
            .filter(|p| !p.is_empty())
            .map(|p| TABLE.iter().position(|c| *c == p).unwrap() as u8 as char)
            .collect();
        assert_eq!(cut, "\u{6} ");
    }
}
