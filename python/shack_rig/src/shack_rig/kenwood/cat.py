"""Kenwood TM-V71 PC commands: two letters, a space, comma-separated
parameters, a carriage return.

Kenwood has never published the TM-V71's PC command set. Everything here
is from the three sources that agree on it, cited where each is used:

- **LA3QMA**, "TM-V71 and TM-D710(G) Kenwood - PC RIG control commands"
  (github.com/LA3QMA/TM-V71_TM-D710-Kenwood, "based on rev.11 02 april
  2009"): the reverse-engineered command list, one page per command.
- **hamlib** rigs/kenwood/tmd710.c (the TM-D710 backend, whose
  `tmv71_caps` the TM-V71(A) shares): the formats it writes and parses -
  FO's and MU's field widths above all, which LA3QMA does not give.
- **CHIRP** chirp/drivers/kenwood_live.py (`TMV71Radio`, a TMD710Radio):
  that `ID` answers "ID TM-V71" and the CR terminator.

Like icom/civ.py and yaesu/cat.py this knows nothing about ports: commands
out, answers in, the half that can be tested without a radio.

    read      FO 0\\r          the radio answers with the set's shape
    answer    FO 0,0144800000,0,0,0,0,0,0,08,08,000,00600000,0\\r
    set       PC 0,2\\r        the radio answers with the new value
    refused   ?\\r             a command it does not know or cannot parse
    not now   N\\r             known, but not possible now (MR on a VFO)

CHIRP's `iserr` treats both "?" and "N" as errors; hamlib's
kenwood_transaction does the same.

**There is a CAT transmit command, and this module has no builder for it.**
LA3QMA lists `TX` ("set the radio in TX mode") and `RX`, and hamlib's
tmd710_set_ptt sends exactly those. Nothing documents which audio a CAT
`TX` takes - the microphone or the DATA jack's PKD - and a packet station
keys through the DATA jack's PKS pin from its interface, which is
documented (the TM-V71 instruction manual's DATA terminal table, mirrored
in the RC-D710 manual p. 17). So OpenHamShack never sends TX: a key that
cannot happen by CAT is one less way to transmit by mistake, and the fake
records any TX it is sent so a test can say none was.

**No S-meter command is known.** LA3QMA's list has none and hamlib's
TM-D710 backend reads only SQL and RF power levels. Busy (`BY`, the
squelch) is the signal reading this radio gives a computer.
"""

from __future__ import annotations

from dataclasses import dataclass

TERMINATOR = "\r"
#: Answers that mean the command was not done (CHIRP kenwood_live.iserr).
REFUSALS = ("?", "N")

BAND_A, BAND_B = 0, 1
BANDS = (BAND_A, BAND_B)

#: FO/ME P13 (LA3QMA tables/mode.md; hamlib tmd710_mode_table).
MODES = {0: "FM", 1: "NFM", 2: "AM"}

#: VM's P2 (LA3QMA VM.md; hamlib TMD710_BAND_MODE_*).
BAND_MODES = {0: "VFO", 1: "memory", 2: "call", 3: "WX"}

#: PC's level. hamlib (tmd710_get_level, "RF power needs to be inverted")
#: reads 0-2 with 0 the most power, which is the radio's H/M/L; LA3QMA's
#: PC.md names them "high, low, EL" - the TM-D710's words for the same three
#: steps as its display shows them. H/M/L is what a TM-V71 says (its power
#: key cycles H, M, L).
POWER_LEVELS = {0: "H", 1: "M", 2: "L"}

#: MU P38, EXT. DATA BAND (menu 517; LA3QMA MU.md, hamlib
#: TMD710_EXT_DATA_BAND_*): which band the DATA jack's PKD/PR1/PR9/PKS are on.
DATA_BANDS = {0: "A", 1: "B", 2: "TX A / RX B", 3: "TX B / RX A"}
#: MU P39, EXT. DATA SPEED (menu 518): 0 1200, 1 9600. Besides the name it
#: sets PKD's input sensitivity - 40 mVp-p at 1200 and 2 Vp-p at 9600 (the
#: RC-D710 manual's DATA terminal table, p. 17, which drives the TM-V71's
#: PKD; hamvoip.org's TM-V71 note says the same of menu 518).
DATA_SPEEDS = {0: 1200, 1: 9600}
#: MU P40, SQC SOURCE (menu 520), what the DATA jack's SQC pin follows.
SQC_SOURCES = {0: "OFF", 1: "BUSY", 2: "SQL", 3: "TX", 4: "BUSY or TX", 5: "SQL or TX"}

#: MU's fields are 1-based in LA3QMA and hamlib (P1..P42); these are the
#: 0-based indexes of the three this library touches.
MU_DATA_BAND, MU_DATA_SPEED, MU_SQC = 37, 38, 39
#: hamlib's tmd710_scan_mu expects exactly 42 (and writes 42); LA3QMA's set
#: line shows 41 parameters but its table runs to P42. A menu answer of any
#: other length is not written back (see `Menu`).
MU_FIELDS = 42


class CatError(Exception):
    """An answer that is not what its command's table says. For a developer:
    in English, like a log line (as yaesu.cat.CatError)."""


def split_answers(buf: str) -> tuple[list[str], str]:
    """Whole answers (without their CR) out of a buffer, and what is left -
    a serial port splits an answer across reads as a matter of course. A
    stray LF (a terminal program's habit) is dropped."""
    out = []
    while True:
        end = buf.find(TERMINATOR)
        if end < 0:
            return out, buf
        line = buf[:end].strip("\n").strip()
        if line:
            out.append(line)
        buf = buf[end + 1:]


def refused(answer: str) -> bool:
    return answer.strip() in REFUSALS


def command(name: str, *params) -> str:
    """One command line: "FO 0" + CR, "PC 0,2" + CR, "ID" + CR."""
    if not params:
        return f"{name}{TERMINATOR}"
    return f"{name} {','.join(str(p) for p in params)}{TERMINATOR}"


def fields(answer: str, name: str) -> list[str]:
    """The comma-separated parameters of an answer to `name`, as the radio
    wrote them. CatError for anything else (a refusal included)."""
    a = answer.strip()
    if a == name:
        return []
    if not a.startswith(name + " "):
        raise CatError(f"not a {name} answer: {answer!r}")
    return [p.strip() for p in a[len(name) + 1:].split(",")]


def _band(b: int) -> int:
    if b not in BANDS:
        raise CatError(f"no band {b!r}: 0 is A, 1 is B")
    return b


# -- reads and the narrow writes ----------------------------------------------------

def read_id() -> str:
    return command("ID")


def parse_id(answer: str) -> str:
    """ "ID TM-V71" -> "TM-V71" (CHIRP compares exactly this word)."""
    got = fields(answer, "ID")
    if len(got) != 1 or not got[0]:
        raise CatError(f"not an ID answer: {answer!r}")
    return got[0]


def read_type() -> str:
    """TY (LA3QMA TY.md): P1 M for the EU (E) version, K for the US (A)."""
    return command("TY")


def read_firmware(unit: int = 0) -> str:
    """FV 0 (LA3QMA FV.md): 0 the main unit, 1 the control panel."""
    return command("FV", unit)


def read_vfo(band: int) -> str:
    return command("FO", _band(band))


def read_band_mode(band: int) -> str:
    return command("VM", _band(band))


def set_band_mode(band: int, mode: int) -> str:
    if mode not in BAND_MODES:
        raise CatError(f"no VM mode {mode!r}")
    return command("VM", _band(band), mode)


def parse_band_mode(answer: str) -> tuple[int, int]:
    got = fields(answer, "VM")
    if len(got) != 2 or not all(g.isdigit() for g in got):
        raise CatError(f"not a VM answer: {answer!r}")
    return int(got[0]), int(got[1])


def read_control_band() -> str:
    """BC: (CTRL band, PTT band) - the front panel's, not the DATA jack's."""
    return command("BC")


def parse_control_band(answer: str) -> tuple[int, int]:
    got = fields(answer, "BC")
    if len(got) != 2 or not all(g.isdigit() for g in got):
        raise CatError(f"not a BC answer: {answer!r}")
    return int(got[0]), int(got[1])


def read_busy(band: int) -> str:
    """BY n: the squelch on band n, 0 closed, 1 open (LA3QMA BY.md; hamlib
    tmd710_get_dcd parses "BY %d,%d")."""
    return command("BY", _band(band))


def parse_busy(answer: str) -> tuple[int, bool]:
    got = fields(answer, "BY")
    if len(got) != 2 or got[1] not in ("0", "1") or not got[0].isdigit():
        raise CatError(f"not a BY answer: {answer!r}")
    return int(got[0]), got[1] == "1"


def read_power(band: int) -> str:
    return command("PC", _band(band))


def set_power(band: int, level: int) -> str:
    if level not in POWER_LEVELS:
        raise CatError(f"no power level {level!r}: 0 H, 1 M, 2 L")
    return command("PC", _band(band), level)


def parse_power(answer: str) -> tuple[int, int]:
    """(band, level) from "PC 0,2" (hamlib parses "PC %d,%d")."""
    got = fields(answer, "PC")
    if len(got) != 2 or not all(g.isdigit() for g in got) or int(got[1]) not in POWER_LEVELS:
        raise CatError(f"not a PC answer: {answer!r}")
    return int(got[0]), int(got[1])


def read_squelch(band: int) -> str:
    return command("SQ", _band(band))


def parse_squelch(answer: str) -> int:
    """The squelch level, 00-1F hex (LA3QMA SQ.md). The answer's shape is
    not settled: LA3QMA writes the set as "SQ p1,p2" and says the read
    "returns x"; hamlib parses the read as "SQ %X". Both are accepted - the
    level is the last field - and it is only ever shown, never written."""
    got = fields(answer, "SQ")
    if not got or len(got) > 2:
        raise CatError(f"not an SQ answer: {answer!r}")
    try:
        level = int(got[-1], 16)
    except ValueError:
        raise CatError(f"not an SQ answer: {answer!r}") from None
    if not 0 <= level <= 0x1F:
        raise CatError(f"squelch {level} is outside 00-1F: {answer!r}")
    return level


def read_menu() -> str:
    return command("MU")


def read_dual() -> str:
    """DL: 0 dual band, 1 single band (LA3QMA DL.md)."""
    return command("DL")


# -- FO: one band's VFO, every field at once ---------------------------------------------

#: FO's thirteen fields (LA3QMA FO.md) with the widths hamlib's tmd710_push_fo
#: writes them at: "FO %1d,%010.0f,%1d,%1d,%1d,%1d,%1d,%1d,%02d,%02d,%03d,%08d,%1d".
FO_NAMES = ("band", "freq", "step", "shift", "reverse", "tone", "ctcss", "dcs", "tone_freq", "ctcss_freq",
            "dcs_code", "offset", "mode")
FO_WIDTHS = (1, 10, 1, 1, 1, 1, 1, 1, 2, 2, 3, 8, 1)


@dataclass(frozen=True)
class Vfo:
    """A band's VFO as FO reports it. `raw` is the radio's own thirteen
    fields; a write sends them back with only the named ones replaced, so a
    field this library does not understand (the step's hex digit, the tone
    numbers) goes back exactly as it came."""

    raw: tuple

    def get(self, name: str) -> str:
        return self.raw[FO_NAMES.index(name)]

    def number(self, name: str) -> int:
        return int(self.get(name), 16) if name in ("step", "shift", "reverse", "tone", "ctcss", "dcs") \
            else int(self.get(name))

    @property
    def band(self) -> int:
        return int(self.get("band"))

    @property
    def freq(self) -> int:
        return int(self.get("freq"))

    @property
    def mode(self) -> int:
        return int(self.get("mode"))

    @property
    def tones(self) -> list[int]:
        """[tone, CTCSS, DCS] switches: [0, 0, 0] is no tone at all."""
        return [self.number("tone"), self.number("ctcss"), self.number("dcs")]

    def replace(self, **changes) -> Vfo:
        raw = list(self.raw)
        for name, value in changes.items():
            i = FO_NAMES.index(name)
            width = FO_WIDTHS[i]
            text = f"{int(value):0{width}d}"
            if len(text) != width or int(value) < 0:
                raise CatError(f"FO {name} {value!r} does not fit {width} digit(s)")
            raw[i] = text
        return Vfo(tuple(raw))

    def command(self) -> str:
        return command("FO", *self.raw)


def parse_vfo(answer: str) -> Vfo:
    got = fields(answer, "FO")
    if len(got) != len(FO_NAMES):
        raise CatError(f"FO answer with {len(got)} fields, not {len(FO_NAMES)}: {answer!r}")
    if not got[0].isdigit() or not got[1].isdigit() or not got[12].isdigit():
        raise CatError(f"FO answer with a field out of shape: {answer!r}")
    return Vfo(tuple(got))


# -- MU: the whole menu at once --------------------------------------------------------------

@dataclass(frozen=True)
class Menu:
    """The radio's menu as MU reports it. There is no command for one menu
    item; MU writes all of them (hamlib's tmd710_push_mu). So a write here
    is the radio's own answer with one field changed and every other field
    as the radio wrote it - never a menu assembled from this library's idea
    of the defaults - and only from an answer of the length hamlib reads."""

    raw: tuple

    @property
    def complete(self) -> bool:
        return len(self.raw) == MU_FIELDS

    def value(self, index: int) -> int | None:
        if index >= len(self.raw) or not self.raw[index].isdigit():
            return None
        return int(self.raw[index])

    @property
    def data_band(self) -> int | None:
        return self.value(MU_DATA_BAND)

    @property
    def data_speed(self) -> int | None:
        return self.value(MU_DATA_SPEED)

    @property
    def sqc_source(self) -> int | None:
        return self.value(MU_SQC)

    def replace(self, index: int, value: int) -> Menu:
        if not self.complete:
            raise CatError(f"a menu of {len(self.raw)} fields is not written back")
        old = self.raw[index]
        text = f"{int(value):0{len(old)}d}"
        if len(text) != len(old):
            raise CatError(f"menu field {index + 1} {value!r} does not fit {len(old)} digit(s)")
        raw = list(self.raw)
        raw[index] = text
        return Menu(tuple(raw))

    def command(self) -> str:
        return command("MU", *self.raw)


def parse_menu(answer: str) -> Menu:
    got = fields(answer, "MU")
    if not got:
        raise CatError(f"an empty MU answer: {answer!r}")
    return Menu(tuple(got))
