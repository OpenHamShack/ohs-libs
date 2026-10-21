"""The Morse tables and keying envelopes every CW user shares.

Moved from QO-100 NB (openhamshack-nb ohscore/modes/cw.py's tables and
ohscore/dsp/morse.py's envelopes, main 47be138), where the simulator's
beacons, the CW transmit fallback and the decoder each had their own copy
of the alphabet. One table now: the decoder's (letters, figures, the
punctuation operators send, and the prosigns NB operators use).

The text is walked once per character (a few dozen per second at most);
the envelope itself is np.repeat of the run lengths, then shaped with 5 ms
raised-cosine edges so keying never clicks. Every function takes the
sample rate: nothing here assumes 12 kHz.
"""
from __future__ import annotations

import numpy as np

# International Morse, plus the handful of prosigns NB operators actually
# use. Prosigns are entered in text as <XX> and sent as their elements with
# no inter-letter gap (that absence of a gap is what makes a prosign a
# single procedural signal rather than two letters) -- so they get their own
# table rather than being spelled out from the letter table at send time.
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "@": ".--.-.", "(": "-.--.", ")": "-.--.-",
    "'": ".----.", '"': ".-..-.",
}
PROSIGNS = {
    "AR": ".-.-.", "SK": "...-.-", "BT": "-...-", "KN": "-.--.",
    "KA": "-.-.-", "VE": "...-.",
}
# Reverse maps for decode: a code string of dots/dashes -> the character
# printed for it. Prosigns are looked up first (Decoder._lookup), so "SK"
# (...-.-) is printed as <SK>, and AR's code as <AR> rather than "+".
CODE_TO_CHAR = {v: k for k, v in MORSE.items()}
CODE_TO_PROSIGN = {v: f"<{k}>" for k, v in PROSIGNS.items()}


def text_to_codes(text: str) -> list[str]:
    """Turn "CQ <AR>" into ["-.-.", "--.-", " ", ".-.-."] -- a list of Morse
    code strings with " " standing in for a word space. <XX> prosign tags
    are consumed whole; unknown characters are dropped rather than raising,
    because a transmit box should never crash on an operator's typo."""
    out = []
    i = 0
    up = text.upper()
    while i < len(up):
        c = up[i]
        if c == " ":
            out.append(" ")
            i += 1
            continue
        if c == "<":
            end = up.find(">", i)
            if end != -1 and up[i + 1:end] in PROSIGNS:
                out.append(PROSIGNS[up[i + 1:end]])
                i = end + 1
                continue
        if c in MORSE:
            out.append(MORSE[c])
        i += 1
    return out


def runs(text: str, wpm: float) -> list[tuple[int, float]]:
    """(key state, seconds) runs for `text` at `wpm` (PARIS timing).
    Characters with no Morse are dropped; prosign tags are not read here
    (text_to_codes does that), as NB's simulator never sent one."""
    dit = 1.2 / wpm
    out: list[tuple[int, float]] = []
    words = text.upper().split()
    for wi, word in enumerate(words):
        letters = [c for c in word if c in MORSE]
        for li, ch in enumerate(letters):
            for si, sym in enumerate(MORSE[ch]):
                out.append((1, dit if sym == "." else 3 * dit))
                if si < len(MORSE[ch]) - 1:
                    out.append((0, dit))
            if li < len(letters) - 1:
                out.append((0, 3 * dit))
        if wi < len(words) - 1:
            out.append((0, 7 * dit))
    return out


def envelope(text: str, wpm: float, rate: float, edge: float = 0.005, tail: float = 0.0) -> np.ndarray:
    """The keying envelope of `text` at `rate` samples a second, with
    `tail` seconds of key-up after it."""
    r = runs(text, wpm)
    if tail:
        r.append((0, tail))
    return envelope_of(r, rate, edge)


def envelope_of(r: list[tuple[int, float]], rate: float, edge: float = 0.005) -> np.ndarray:
    """The shaped envelope of (key state, seconds) runs."""
    if not r:
        return np.zeros(0, np.float32)
    states = np.array([s for s, _ in r], np.float32)
    lengths = np.array([int(round(d * rate)) for _, d in r])
    env = np.repeat(states, lengths)
    n = max(1, int(edge * rate))
    w = np.sin(np.linspace(0, np.pi / 2, n)) ** 2
    w = np.concatenate([w, w[::-1]])
    w /= w.sum()
    return np.convolve(env, w, mode="same").astype(np.float32)
