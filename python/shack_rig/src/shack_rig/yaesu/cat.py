"""Yaesu CAT: two letters, parameters of fixed width, a semicolon.

Written from Yaesu's FTX-1 CAT Operation Reference Manual (2508-C), page
numbers being its printed footers; the copy is outside the repository
(~/vendor/manuals/yaesu on shack). Like icom/civ.py this knows nothing
about ports - it turns frequencies and modes into commands and answers
back into values, which is the half that can be tested without a radio.

    set      FA014250000;      the radio acts, and answers nothing
    read     FA;               the radio answers with the set's shape
    answer   FA014250000;
    refused  ?;                a command it did not take (wrong digits, a
                               value out of range, a mode that forbids it)

Parameters are exactly as wide as the table says (p. 4): "IS00+100;" is
three digits short and refused, so every builder here formats to the
width. With Auto Information on (AI1;) the radio also volunteers the
answer of any AI-marked command when its state changes - the dial, the
mode, the squelch (RI) - which arrive on the same stream as answers and
are read the same way.

What this module sends for a packet station, all of it from the manual:

    FAnnnnnnnnn;   MAIN frequency, 9 digits of Hz (p. 16)
    MD0x;          MAIN mode; x from MODE_CODES, DATA-FM is A (p. 19)
    TX1; TX0;      CAT transmit on / off; TX; reads 0/1/2 (p. 25)
    RI0;           radio information: P4 0 RX 1 TX 2 TX inhibit, P8 squelch
                   0 closed 1 open (BUSY) (p. 22)
    SM0;           MAIN S-meter 000-255 (p. 24)
    RM5;           read the PO (output power) meter, 000-255 (p. 23)
    CT00;          MAIN CTCSS/DCS off (p. 9)
    OS00;          MAIN simplex (FM modes only) (p. 21)
    AI0; AI1;      auto information off/on, per CAT port, off at power-off (p. 6)
    IF;            the MAIN-side 30-character status (p. 17), `parse_if`
    EXaabbcc;      menu read; EXaabbcc<value>; menu write (pp. 9-15)
    ID;            0840 on an FTX-1 (p. 17)
"""

from __future__ import annotations

from dataclasses import dataclass

TERMINATOR = ";"
REFUSED = "?;"

#: MD's P2, the mode byte, as the FTX-1 manual lists it (p. 19); IF's P6 is
#: the same table (p. 17). G and J are unused.
MODE_CODES = {
    "1": "LSB", "2": "USB", "3": "CW-U", "4": "FM", "5": "AM", "6": "RTTY-L", "7": "CW-L",
    "8": "DATA-L", "9": "RTTY-U", "A": "DATA-FM", "B": "FM-N", "C": "DATA-U", "D": "AM-N",
    "E": "PSK", "F": "DATA-FM-N", "H": "C4FM-DN", "I": "C4FM-VW",
}

#: Yaesu's names in this library's vocabulary (shack_rig.model): the mode,
#: the data flag, and whether the narrow filter is part of the mode. DATA is
#: a mode on a Yaesu and a flag here (model.py says why), so DATA-FM is
#: ("FM", True). RTTY-L is what this library and hamlib call RTTY, RTTY-U
#: RTTYR; CW-U is CW and CW-L CWR, as hamlib maps them. C4FM has no name in
#: the vocabulary, so it is reported as no mode rather than a wrong one.
VOCABULARY = {
    "LSB": ("LSB", False, False), "USB": ("USB", False, False), "CW-U": ("CW", False, False),
    "CW-L": ("CWR", False, False), "FM": ("FM", False, False), "FM-N": ("FM", False, True),
    "AM": ("AM", False, False), "AM-N": ("AM", False, True), "RTTY-L": ("RTTY", False, False),
    "RTTY-U": ("RTTYR", False, False), "DATA-L": ("LSB", True, False), "DATA-U": ("USB", True, False),
    "DATA-FM": ("FM", True, False), "DATA-FM-N": ("FM", True, True), "PSK": ("PSK", False, False),
}


class CatError(Exception):
    """An answer that is not what its command's table says. For a developer:
    in English, like a log line (as icom.civ.CivError)."""


def split_answers(buf: str) -> tuple[list[str], str]:
    """Whole answers (each with its semicolon) out of a buffer, and what is
    left - a serial port splits an answer across reads as a matter of course."""
    out = []
    while True:
        end = buf.find(TERMINATOR)
        if end < 0:
            return out, buf
        out.append(buf[:end + 1])
        buf = buf[end + 1:]


def code_for(mode: str, data: bool = False, narrow: bool = False) -> str | None:
    """The MD code for a mode in this library's vocabulary, or None if the
    radio has no such thing (CW with data, say)."""
    for yaesu, (m, d, n) in VOCABULARY.items():
        if (m, d, n) == (mode, data, narrow):
            for code, name in MODE_CODES.items():
                if name == yaesu:
                    return code
    return None


def vocabulary(code: str) -> tuple[str | None, bool, bool]:
    """(mode, data, narrow) for an MD/IF mode code; (None, False, False) for
    one with no name here (C4FM)."""
    return VOCABULARY.get(MODE_CODES.get(code, ""), (None, False, False))


# -- builders -------------------------------------------------------------------

def set_freq(hz: int) -> str:
    if not 0 <= hz <= 999_999_999:
        raise CatError(f"frequency out of range: {hz}")
    return f"FA{hz:09d};"


def read_freq() -> str:
    return "FA;"


def set_mode(code: str, side: int = 0) -> str:
    if code not in MODE_CODES:
        raise CatError(f"no such mode code: {code!r}")
    return f"MD{side}{code};"


def read_mode(side: int = 0) -> str:
    return f"MD{side};"


def set_tx(on: bool) -> str:
    """TX1; keys by CAT (P1 1: "RADIO TX OFF, CAT TX ON"), TX0; unkeys."""
    return "TX1;" if on else "TX0;"


def read_tx() -> str:
    return "TX;"


def read_radio_info() -> str:
    return "RI0;"


def read_smeter(side: int = 0) -> str:
    return f"SM{side};"


#: RM's meters (p. 23).
METERS = {"s": 1, "s-sub": 2, "comp": 3, "alc": 4, "po": 5, "swr": 6, "idd": 7, "vdd": 8}


def read_meter(which: str) -> str:
    return f"RM{METERS[which]};"


def tones_off(side: int = 0) -> str:
    return f"CT{side}0;"


def simplex(side: int = 0) -> str:
    return f"OS{side}0;"


def read_tones(side: int = 0) -> str:
    return f"CT{side};"


def set_tones(value: int, side: int = 0) -> str:
    """CT's P2 (p. 9): 0 off, 1 CTCSS encode, 2 encode and decode, 3 DCS,
    4 PR FREQ, 5 REV TONE - for putting back what was found."""
    if not 0 <= value <= 5:
        raise CatError(f"no such tone setting: {value!r}")
    return f"CT{side}{value};"


def read_shift(side: int = 0) -> str:
    return f"OS{side};"


def set_shift(value: int, side: int = 0) -> str:
    """OS's P2 (p. 21): 0 simplex, 1 plus, 2 minus, 3 ARS. FM modes only."""
    if not 0 <= value <= 3:
        raise CatError(f"no such shift: {value!r}")
    return f"OS{side}{value};"


# -- a radio set up for data: one narrow writer per setting ----------------
#
# Added for the data set-up (shack_rig.yaesu.datasetup, Packet Radio 0.2),
# each from the FTX-1 CAT manual 2508-C, page as printed. Each is a switch or
# a level on the receive or transmit audio path; none is a menu item, so
# none can reach anything but itself.
#
#     BC0 0/1;      auto notch (DNF) off/on, MAIN (p. 7)
#     RL0 00-10;    noise reduction (DNR) level, 00 off, MAIN (p. 23)
#     NL0 000-010;  noise blanker level, 000 off, MAIN (p. 21)
#     VX 0/1;       VOX (p. 26)
#     ML0 000/001;  monitor off/on (p. 19)
#     PR0 1/2;      speech processor off/on - 1 is OFF, 2 is ON (p. 22)

def read_auto_notch(side: int = 0) -> str:
    return f"BC{side};"


def set_auto_notch(on: bool, side: int = 0) -> str:
    return f"BC{side}{1 if on else 0};"


def read_noise_reduction(side: int = 0) -> str:
    return f"RL{side};"


def set_noise_reduction(level: int, side: int = 0) -> str:
    """0 switches DNR off; 1-10 is on at that level."""
    if not 0 <= level <= 10:
        raise CatError(f"DNR level out of range: {level!r}")
    return f"RL{side}{level:02d};"


def read_noise_blanker(side: int = 0) -> str:
    return f"NL{side};"


def set_noise_blanker(level: int, side: int = 0) -> str:
    """0 switches the noise blanker off; 1-10 is on at that level."""
    if not 0 <= level <= 10:
        raise CatError(f"NB level out of range: {level!r}")
    return f"NL{side}{level:03d};"


def read_vox() -> str:
    return "VX;"


def set_vox(on: bool) -> str:
    return f"VX{1 if on else 0};"


def read_monitor() -> str:
    return "ML0;"


def set_monitor(on: bool) -> str:
    return f"ML0{1 if on else 0:03d};"


def read_processor() -> str:
    return "PR0;"


def set_processor(on: bool) -> str:
    return f"PR0{2 if on else 1};"


def parse_value(a: str, prefix: str) -> int:
    """The number after `prefix` in an answer (BC0 1; -> 1, RL0 05; -> 5)."""
    if not a.startswith(prefix) or not a.endswith(TERMINATOR) or not a[len(prefix):-1].isdigit():
        raise CatError(f"not a {prefix} answer: {a!r}")
    return int(a[len(prefix):-1])


def auto_info(on: bool) -> str:
    return "AI1;" if on else "AI0;"


def read_info() -> str:
    return "IF;"


def read_id() -> str:
    return "ID;"


def read_menu(p1: int, p2: int, p3: int) -> str:
    return f"EX{p1:02d}{p2:02d}{p3:02d};"


def set_menu(p1: int, p2: int, p3: int, value: int, digits: int) -> str:
    """One menu write. The caller is a model table's row, never free input:
    shack_rig writes exactly the rows models.py names (MOD SOURCE), as the
    Icom side writes only DATA MOD."""
    if not 0 <= value < 10 ** digits:
        raise CatError(f"menu value {value} does not fit {digits} digit(s)")
    return f"EX{p1:02d}{p2:02d}{p3:02d}{value:0{digits}d};"


# -- answers --------------------------------------------------------------------

def parse_freq(a: str) -> int:
    if not (a.startswith("FA") or a.startswith("FB")) or len(a) != 12 or not a[2:11].isdigit():
        raise CatError(f"not a frequency answer: {a!r}")
    return int(a[2:11])


def parse_mode(a: str) -> tuple[int, str]:
    """(side, code) from an MD answer."""
    if not a.startswith("MD") or len(a) != 5 or a[2] not in "01":
        raise CatError(f"not a mode answer: {a!r}")
    return int(a[2]), a[3]


def parse_tx(a: str) -> int:
    """0 not transmitting, 1 keyed by CAT, 2 keyed by the radio itself (its
    microphone, its front panel, the other CAT port's RTS/DTR)."""
    if not a.startswith("TX") or len(a) != 4 or a[2] not in "012":
        raise CatError(f"not a TX answer: {a!r}")
    return int(a[2])


@dataclass(frozen=True)
class RadioInfo:
    """RI's eight fields (p. 22)."""
    hi_swr: bool
    recorder: int        #: 0 stop, 1 recording, 2 playing
    tx: int              #: 0 RX, 1 TX, 2 TX inhibit
    tuning: bool
    scan: int            #: 0 stopped, 1 scanning, 2 paused
    busy: bool           #: squelch open


def parse_radio_info(a: str) -> RadioInfo:
    if not a.startswith("RI") or len(a) != 11 or not a[2:10].isdigit():
        raise CatError(f"not a radio-information answer: {a!r}")
    p = a[2:10]
    return RadioInfo(hi_swr=p[1] == "1", recorder=int(p[2]), tx=int(p[3]), tuning=p[5] == "1",
                     scan=int(p[6]), busy=p[7] == "1")


def parse_smeter(a: str) -> int:
    if not a.startswith("SM") or len(a) != 7 or not a[3:6].isdigit():
        raise CatError(f"not an S-meter answer: {a!r}")
    return int(a[3:6])


def parse_meter(a: str) -> tuple[int, int]:
    """(meter number, 000-255) from an RM answer: RM P1 P2P2P2 P3P3P3 ;"""
    if not a.startswith("RM") or len(a) != 10 or not a[2:9].isdigit():
        raise CatError(f"not a meter answer: {a!r}")
    return int(a[2]), int(a[3:6])


def parse_menu(a: str) -> tuple[tuple[int, int, int], str]:
    """((p1, p2, p3), value as the radio wrote it) from an EX answer."""
    if not a.startswith("EX") or len(a) < 10 or not a[2:8].isdigit():
        raise CatError(f"not a menu answer: {a!r}")
    return (int(a[2:4]), int(a[4:6]), int(a[6:8])), a[8:-1]


@dataclass(frozen=True)
class Info:
    """IF's thirty characters (p. 17), the FTX-1's own layout.

        IF ccccc fffffffff ±oooo r t m v q 00 s ;
           1     2         3     4 5 6 7 8 9  10

    ccccc  P1  memory channel, 00000 on the VFO (or P-01L, EMGCH... as text)
    f      P2  the frequency, 9 digits of Hz
    ±oooo  P3  clarifier direction and offset, Hz
    r t    P4 P5 RX / TX clarifier on
    m      P6  the mode code (MODE_CODES)
    v      P7  0 VFO, 1 memory, 2 memory tune, 3 QMB, 5 PMS
    q      P8  0 off, 1 CTCSS enc/dec, 2 CTCSS enc, 3 DCS, 4 PR FREQ, 5 REV TONE
    00     P9  fixed
    s      P10 0 simplex, 1 plus shift, 2 minus shift
    """
    channel: str
    freq: int
    clarifier_hz: int
    rx_clarifier: bool
    tx_clarifier: bool
    mode_code: str
    vfo_memory: int
    tone: int
    shift: int

    @property
    def mode(self) -> str | None:
        return vocabulary(self.mode_code)[0]

    @property
    def data(self) -> bool:
        return vocabulary(self.mode_code)[1]


IF_LENGTH = 30


def parse_if(a: str) -> Info:
    """The FTX-1's IF answer. Other Yaesus lay IF out differently, which is
    why the model table names the layout rather than this assuming one."""
    if not a.startswith("IF") or len(a) != IF_LENGTH or a[-1] != TERMINATOR:
        raise CatError(f"not an FTX-1 IF answer ({len(a)} characters): {a!r}")
    f = a[7:16]
    clar = a[16:21]
    if not f.isdigit() or clar[0] not in "+-" or not clar[1:].isdigit() or not a[26:28].isdigit():
        raise CatError(f"IF answer with a field out of shape: {a!r}")
    if a[23] not in MODE_CODES:
        raise CatError(f"IF answer with mode {a[23]!r}: {a!r}")
    return Info(channel=a[2:7], freq=int(f), clarifier_hz=int(clar[1:]) * (-1 if clar[0] == "-" else 1),
                rx_clarifier=a[21] == "1", tx_clarifier=a[22] == "1", mode_code=a[23],
                vfo_memory=int(a[24]) if a[24].isdigit() else -1, tone=int(a[25]) if a[25].isdigit() else -1,
                shift=int(a[28]) if a[28].isdigit() else -1)
