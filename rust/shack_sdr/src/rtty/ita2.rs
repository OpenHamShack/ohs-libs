//! ITA2 (CCITT No. 2, "Baudot-Murray") with the US-TTY figures case that
//! amateur RTTY uses in place of ITA2's own (ITU-T S.1): D=$, F=!, G=&,
//! H=#, J=', S=BELL, V=;, Z=". That is fldigi's and MMTTY's default table,
//! and exactly openhamshack-nb `ohscore/modes/rtty.py`'s, so the Rust
//! decoder prints what the Python one did and what the stations we work
//! print.
//!
//! A code is five bits, b1 first in transmission order, held here as
//! b1<<4 | b2<<3 | ... | b5 (so "11000" is 0b11000 = A), 1 = mark.

pub const LTRS: u8 = 0b11111;
pub const FIGS: u8 = 0b11011;
pub const SPACE: u8 = 0b00100;
pub const CR: u8 = 0b00010;
pub const LF: u8 = 0b01000;
pub const BLANK: u8 = 0b00000;

/// (code, letter, figure). Codes with no figure meaning repeat the letter
/// meaning. '\0' marks blank (00000), which prints nothing; LTRS and FIGS
/// are shifts, handled before lookup.
const ROWS: [(u8, char, char); 30] = [
    (0b00000, '\0', '\0'),
    (0b00100, ' ', ' '),
    (0b00010, '\r', '\r'),
    (0b01000, '\n', '\n'),
    (0b11000, 'A', '-'),
    (0b10011, 'B', '?'),
    (0b01110, 'C', ':'),
    (0b10010, 'D', '$'),
    (0b10000, 'E', '3'),
    (0b10110, 'F', '!'),
    (0b01011, 'G', '&'),
    (0b00101, 'H', '#'),
    (0b01100, 'I', '8'),
    (0b11010, 'J', '\''),
    (0b11110, 'K', '('),
    (0b01001, 'L', ')'),
    (0b00111, 'M', '.'),
    (0b00110, 'N', ','),
    (0b00011, 'O', '9'),
    (0b01101, 'P', '0'),
    (0b11101, 'Q', '1'),
    (0b01010, 'R', '4'),
    (0b10100, 'S', '\u{7}'),
    (0b00001, 'T', '5'),
    (0b11100, 'U', '7'),
    (0b01111, 'V', ';'),
    (0b11001, 'W', '2'),
    (0b10111, 'X', '/'),
    (0b10101, 'Y', '6'),
    (0b10001, 'Z', '"'),
];

/// The character a code prints in the given case, or None for a shift.
pub fn lookup(code: u8, figs: bool) -> Option<char> {
    if code == LTRS || code == FIGS {
        return None;
    }
    ROWS.iter().find(|r| r.0 == code).map(|r| if figs { r.2 } else { r.1 })
}

/// What a teleprinter would have put on the paper: CR LF is one line break
/// (LF is the newline, CR prints nothing), and BELL and blank print nothing.
/// Handed to a terminal as raw control codes they drew as empty boxes
/// (rtty.py `_SILENT`).
pub fn printable(ch: char) -> bool {
    !matches!(ch, '\r' | '\u{7}' | '\0')
}

fn letter_code(ch: char) -> Option<u8> {
    ROWS.iter().find(|r| r.1 == ch && ch != '\0').map(|r| r.0)
}

fn figure_code(ch: char) -> Option<u8> {
    ROWS.iter().find(|r| r.2 == ch && ch != '\0').map(|r| r.0)
}

/// Text to codes, inserting a shift whenever the next character is not in
/// the case the line is in, as a teleprinter did; unshift-on-space (a space
/// returns the receiver to letters, so a figure after a space needs FIGS
/// again); a newline goes as CR LF; anything unmappable is dropped. The
/// same rules as rtty.py's `_text_to_codes`, so the test signals are what
/// our own transmitter sends.
pub fn encode(text: &str) -> Vec<u8> {
    let mut out = Vec::new();
    let mut figs = false;
    for ch in text.chars().flat_map(|c| c.to_uppercase()) {
        if ch == '\r' {
            continue;
        }
        if ch == '\n' {
            out.push(CR);
            out.push(LF);
            continue;
        }
        let letter = letter_code(ch);
        let figure = figure_code(ch);
        if let Some(c) = letter.filter(|_| figure.is_none() || !figs) {
            if figs {
                out.push(LTRS);
                figs = false;
            }
            out.push(c);
            if ch == ' ' {
                figs = false;
            }
        } else if let Some(c) = figure {
            if !figs {
                out.push(FIGS);
                figs = true;
            }
            out.push(c);
            if ch == ' ' {
                figs = false;
            }
        }
    }
    out
}

/// The receiving side's shift state and printing rules.
#[derive(Clone, Debug, Default)]
pub struct Printer {
    pub figs: bool,
    /// Unshift-on-space: fldigi's and MMTTY's default, and what our encoder
    /// assumes. A lost LTRS then garbles one group of figures, not the rest
    /// of the over.
    pub usos: bool,
}

impl Printer {
    pub fn new(usos: bool) -> Self {
        Printer { figs: false, usos }
    }

    /// The printable character for `code`, updating the shift state.
    pub fn code(&mut self, code: u8) -> Option<char> {
        match code {
            LTRS => {
                self.figs = false;
                None
            }
            FIGS => {
                self.figs = true;
                None
            }
            _ => {
                let ch = lookup(code, self.figs);
                if code == SPACE && self.usos {
                    self.figs = false;
                }
                ch.filter(|c| printable(*c))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn letters_and_figures_round_trip_through_the_printer() {
        let text = "CQ CQ DE GM4BRI 599 NR 001 $!&#'\";\nRST?";
        let mut p = Printer::new(true);
        let got: String = encode(text).into_iter().filter_map(|c| p.code(c)).collect();
        assert_eq!(got, text);
    }

    #[test]
    fn a_space_unshifts_so_figures_after_it_need_figs_again() {
        let codes = encode("1 2");
        assert_eq!(codes, vec![FIGS, 0b11101, SPACE, FIGS, 0b11001]);
        let mut p = Printer::new(true);
        p.code(FIGS);
        p.code(SPACE);
        assert!(!p.figs);
        let mut q = Printer::new(false);
        q.code(FIGS);
        q.code(SPACE);
        assert!(q.figs);
    }

    #[test]
    fn cr_bell_and_blank_print_nothing() {
        let mut p = Printer::new(true);
        assert_eq!(p.code(CR), None);
        assert_eq!(p.code(LF), Some('\n'));
        assert_eq!(p.code(BLANK), None);
        p.code(FIGS);
        assert_eq!(p.code(0b10100), None); // BELL
    }
}
