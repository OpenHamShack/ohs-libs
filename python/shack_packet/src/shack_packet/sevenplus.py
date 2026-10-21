"""7PLUS: binary files as text, for posting to and through packet BBSs.

7PLUS (Axel Bauda DG1BBQ, version 2.2 format, still what BBS users post)
turns a file into lines of 8-bit text that a BBS will store and forward as
an ordinary message, cut into parts small enough to post one per message.
This is a port of the format as 7PLUS 2.25's own source writes it
(encode.c, decode.c, utils.c), checked byte for byte against that program
built from its source (tests/data/sevenplus/ was made by it).

**The code.** 216 printable 8-bit characters (0x21-0xFC without '*',
DEL, 0x91 and 0x93). Each code line carries 62 bytes as two groups of 31
bytes; each group is eight 31-bit numbers, each written as four base-216
digits, least significant first: 64 characters. Then three characters for
the line number (9 bits) and a 14-bit CRC of those 64, and two for a
15-bit CRC of all 67 - 69 characters, LF-terminated. The CRC is the CCITT
polynomial (0x1021) started at 0. The last line of a file is padded with
zero bytes; the header's size says how many are real.

**A part** starts with a header line (" go_7+. 001 of 003 NAME.EXT
0000123 ..." with the 8.3 name, the file's size, the part's size in code
characters and the lines per part, a mini-CRC and the line's own CRC), on
part 1 a line holding the long name between slashes, the code lines, and
a stop line (" stop_7+. (NAME.P01/03) [timestamp]"). There is no
whole-file checksum in the format: every line has its own two CRCs, and a
decoded file has the size the header promised.

The format needs an 8-bit path: a BBS or mail system that strips bit 7
destroys it (the decoder says so, as 7PLUS does, by the header's three
0xB0 0xB1 0xB2 marker bytes).

`encode(data, name)` -> parts (file name, text). `Collector` takes any
text - messages read from a BBS, a capture file, the parts themselves -
finds the 7PLUS lines in it however they were wrapped in mail headers,
and `result()` gives the file once every line is there, or which lines
are missing. Correction (.COR/.ERR) files are not made or read here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

CODE = bytes([*range(0x21, 0x2A), *range(0x2B, 0x7F), *range(0x80, 0x91), 0x92, *range(0x94, 0xFD)])
assert len(CODE) == 216
DECODE = [255] * 256
for _i, _c in enumerate(CODE):
    DECODE[_c] = _i

MARK = b"\xb0\xb1\xb2"
#: The long-name flag 7PLUS 2.25's Unix and Windows builds write ('*'); a
#: line with the full name follows part 1's header.
EXTENDED = 0x2A
MAX_LINES = 512
MAX_PARTS = 255
BYTES_PER_LINE = 62


def _crctab() -> list[int]:
    rem = (0x9188, 0x48C4, 0x2462, 0x1231, 0x8108, 0x4084, 0x2042, 0x1021)
    tab = []
    for n in range(256):
        r = 0
        mask = 0x80
        for m in range(8):
            if n & mask:
                r ^= rem[m]
            mask >>= 1
        tab.append(r)
    return tab


CRCTAB = _crctab()


def _crc(data, crc: int = 0) -> int:
    for y in data:
        crc = CRCTAB[crc >> 8] ^ (((crc & 255) << 8) | y)
    return crc


def _add_crc2(line: bytearray) -> bytearray:
    crc = _crc(reversed(line[:67])) & 0x7FFF
    return line[:67] + bytes([CODE[crc % 216], CODE[crc // 216]])


def _mcrc(line: bytearray) -> bytearray:
    j = line.index(MARK[:2]) + 4
    crc = _crc(line[:j]) % 216
    line[j:j + 1] = bytes([CODE[crc]])
    return line


def _header_line(text: bytes, flag: int) -> bytes:
    line = bytearray(text + MARK + bytes([flag]) + b" ")
    return bytes(_add_crc2(_mcrc(line)))


def dos_name(name: str) -> tuple[str, str]:
    """7PLUS's build_DOS_name: lower case, unsafe characters to '_', 8.3.
    (stem, ext) without the dot."""
    def strip(s: str) -> str:
        s = s.lower()
        return "".join("_" if (ord(c) < 32 or ord(c) > 126 or c in " <>=,';:*?&[]{}|^()/.\\\"~+@") else c
                       for c in s)
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    return strip(stem)[:8], strip(ext)[:3]


def _code_line(chunk: bytes, number: int) -> bytes:
    chunk = chunk + bytes(BYTES_PER_LINE - len(chunk))
    out = bytearray()
    for g in range(2):
        grp = chunk[g * 31:(g + 1) * 31]
        af = [int.from_bytes(grp[j * 4:j * 4 + 4], "big") for j in range(7)] + [int.from_bytes(grp[28:31], "big")]
        af[7] = af[7] | ((af[6] & 127) << 24)
        af[6] = (af[6] >> 7) | ((af[5] & 63) << 25)
        af[5] = (af[5] >> 6) | ((af[4] & 31) << 26)
        af[4] = (af[4] >> 5) | ((af[3] & 15) << 27)
        af[3] = (af[3] >> 4) | ((af[2] & 7) << 28)
        af[2] = (af[2] >> 3) | ((af[1] & 3) << 29)
        af[1] = (af[1] >> 2) | ((af[0] & 1) << 30)
        af[0] = af[0] >> 1
        for v in af:
            out += bytes([CODE[v % 216], CODE[(v // 216) % 216], CODE[(v // 46656) % 216], CODE[v // 10077696]])
    crc = _crc(out) & 0x3FFF
    v = ((number & 0x1FF) << 14) | crc
    out += bytes([CODE[v % 216], CODE[(v // 216) % 216], CODE[v // 46656]])
    return bytes(_add_crc2(out))


def _decode_line(line: bytes) -> tuple[int, bytes] | None:
    """(line number, 62 bytes) for a code line whose CRCs hold, else None."""
    if len(line) < 69 or any(DECODE[c] == 255 for c in line[:69]):
        return None
    crc = _crc(line[:64]) & 0x3FFF
    cs = 46656 * DECODE[line[66]] + 216 * DECODE[line[65]] + DECODE[line[64]]
    if cs & 0x3FFF != crc:
        return None
    if (_crc(reversed(line[:67])) & 0x7FFF) != 216 * DECODE[line[68]] + DECODE[line[67]]:
        return None
    out = bytearray()
    vals = []
    for i in range(16):
        d = line[i * 4:i * 4 + 4]
        vals.append(((DECODE[d[3]] * 216 + DECODE[d[2]]) * 216 + DECODE[d[1]]) * 216 + DECODE[d[0]])
    for g in range(2):
        af = vals[g * 8:(g + 1) * 8]
        w = [((af[0] << 1) | (af[1] >> 30)), ((af[1] << 2) | (af[2] >> 29)), ((af[2] << 3) | (af[3] >> 28)),
             ((af[3] << 4) | (af[4] >> 27)), ((af[4] << 5) | (af[5] >> 26)), ((af[5] << 6) | (af[6] >> 25)),
             ((af[6] << 7) | (af[7] >> 24)), (af[7] << 8)]
        for j, x in enumerate(w):
            x &= 0xFFFFFFFF
            out += x.to_bytes(4, "big")[:3 if j == 7 else 4]
    return cs >> 14, bytes(out)


def encode(data: bytes, name: str, *, lines_per_part: int = MAX_LINES, mtime: float | None = None) \
        -> list[tuple[str, bytes]]:
    """The parts of a 7PLUS encoding: [(file name, text)], NAME.7PL for a
    single part or NAME.P01... for several, as 7PLUS names them. Lines end
    in LF; a BBS message would carry them with CR, which decodes the same."""
    if not data:
        raise ValueError("7PLUS cannot encode an empty file")
    if len(data) > 9_999_999:
        raise ValueError("7PLUS's header holds sizes up to 9999999 bytes")
    lines_per_part = max(1, min(MAX_LINES, int(lines_per_part)))
    size = len(data)
    block = min(lines_per_part * BYTES_PER_LINE, size)
    blocklines = (block + 61) // 62
    parts = (size + block - 1) // block
    if parts > MAX_PARTS:
        raise ValueError("more than 255 parts: choose more lines per part")
    stem, ext = dos_name(name)
    hdrname = (stem + ("." if ext else "") + ext).upper()
    longname = name.replace("\\", "/").rsplit("/", 1)[-1][:60]
    stamp = int(mtime if mtime is not None else time.time()) & 0xFFFFFFFF
    out = []
    for part in range(1, parts + 1):
        start = (part - 1) * block
        chunk = data[start:start + block]
        this = ((len(chunk) + 61) // 62) * 62
        lines = [_header_line(b" go_7+. %03d of %03d %-12s %07d %04X %03X (7PLUS v2.2) "
                              % (part, parts, hdrname.encode("latin-1"), size, ((this + 61) // 62) * 64, blocklines),
                              EXTENDED)[:69]]
        if part == 1:
            slash = bytearray(b"/" * 62)
            nm = longname.encode("latin-1", "replace")
            slash[1:1 + len(nm)] = nm
            lines.append(_header_line(bytes(slash), EXTENDED)[:69])
        for n in range(0, len(chunk), BYTES_PER_LINE):
            lines.append(_code_line(chunk[n:n + BYTES_PER_LINE], n // BYTES_PER_LINE))
        if parts > 1:
            stop = b" stop_7+. (%s.P%02X/%02X) [%X]" % (stem.upper().encode(), part, parts, stamp)
        else:
            stop = b" stop_7+. (%s.7PL) [%X]" % (stem.upper().encode(), stamp)
        line = bytearray(b" " * 62 + MARK + b"\xdb ")
        line[:len(stop)] = stop
        lines.append(bytes(_add_crc2(_mcrc(line))))
        fname = f"{stem}.7pl" if parts == 1 else f"{stem}.p{part:02x}"
        out.append((fname, b"\n".join(lines) + b"\n"))
    return out


@dataclass
class Result:
    name: str                       # the long name if one was sent, else the 8.3 one
    size: int
    parts: int
    data: bytes | None              # None until complete
    missing: list[tuple[int, int]] = field(default_factory=list)   # (part, line)
    bad_lines: int = 0
    stripped: bool = False          # bit 7 was lost on the way: nothing can be decoded

    @property
    def complete(self) -> bool:
        return self.data is not None


_HEAD = re.compile(rb" go_7\+\. (\d{3}) of (\d{3}) (\S+)\s+(\d{7}) ([0-9A-Fa-f]{4}) ([0-9A-Fa-f]{3}) ")


class Collector:
    """7PLUS lines from any text, in any order, from any number of feeds.
    Lines are matched to their part by the header that came before them;
    a code line is kept only when both its CRCs hold."""

    def __init__(self):
        self.name = ""
        self.long_name = ""
        self.size: int | None = None
        self.parts: int | None = None
        self.blocklines: int | None = None
        self.lines: dict[tuple[int, int], bytes] = {}
        self.bad = 0
        self.stripped = False

    def feed(self, text: bytes | str) -> None:
        if isinstance(text, str):
            text = text.encode("latin-1", "replace")
        part = None
        for raw in re.split(rb"\r\n|\r|\n", text):
            line = raw.lstrip(b">") if raw.startswith(b"> go_7+") else raw
            m = _HEAD.match(line)
            if m:
                if MARK[:2] not in line:
                    self.stripped = True
                    part = None
                    continue
                part = int(m.group(1))
                parts, size, blocklines = int(m.group(2)), int(m.group(4)), int(m.group(6), 16)
                if self.size is None:
                    self.name, self.size, self.parts, self.blocklines = m.group(3).decode("latin-1"), size, parts, \
                        blocklines
                elif (size, parts, blocklines) != (self.size, self.parts, self.blocklines):
                    part = None                                 # another file's part: not ours
                continue
            if part is None:
                continue
            if line.startswith(b" stop_7+."):
                part = None
                continue
            if line.startswith(b"/") and line[62:65] == MARK and not self.lines.get((part, 0)):
                self.long_name = line[1:line.index(b"/", 1)].decode("latin-1") if b"/" in line[1:] else ""
                continue
            if len(line) >= 69:
                got = _decode_line(line[:69])
                if got is None:
                    self.bad += 1
                    continue
                self.lines[(part, got[0])] = got[1]

    def result(self) -> Result:
        if self.size is None:
            return Result("", 0, 0, None, stripped=self.stripped)
        total_lines = (self.size + 61) // 62
        missing = []
        out = bytearray()
        for n in range(total_lines):
            part, line = divmod(n, self.blocklines)
            got = self.lines.get((part + 1, line))
            if got is None:
                missing.append((part + 1, line))
            else:
                out += got
        name = self.long_name or self.name.lower()
        data = bytes(out[:self.size]) if not missing else None
        return Result(name, self.size, self.parts, data, missing, self.bad, self.stripped)


def decode(*texts) -> Result:
    c = Collector()
    for t in texts:
        c.feed(t)
    return c.result()


def find(text: bytes | str) -> bool:
    """True if the text holds a 7PLUS part header."""
    if isinstance(text, str):
        text = text.encode("latin-1", "replace")
    return b" go_7+. " in text
