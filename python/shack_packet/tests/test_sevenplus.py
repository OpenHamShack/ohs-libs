"""7PLUS against the real thing: tests/data/sevenplus/ was written by 7PLUS
2.25 built from DG1BBQ's source (7pl225sr.tgz; the 64-bit Linux build,
whose stop-line timestamp is garbage wider than 32 bits, so stop lines
are compared by structure, not bytes). This encoder's output for a
5000-byte file in three parts with a long name, CRLF line ends, was also
decoded by that program, byte for byte, on 20/10/2026."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from shack_packet import sevenplus as sp

DATA = Path(__file__).parent / "data" / "sevenplus"
CASES = [("small.bin", ["small.7pl"], {}), ("text.txt", ["text.7pl"], {}), ("exact.bin", ["exact.7pl"], {}),
         ("multi.bin", [f"multi.p0{i}" for i in range(1, 5)], {"lines_per_part": 10})]


@pytest.mark.parametrize("name,parts,kw", CASES)
def test_the_real_programs_parts_decode(name, parts, kw):
    r = sp.decode(*[(DATA / p).read_bytes() for p in parts])
    assert r.complete and r.data == (DATA / name).read_bytes()
    assert r.name == name and r.parts == len(parts) and r.bad_lines == 0


@pytest.mark.parametrize("name,parts,kw", CASES)
def test_encoding_matches_the_real_program_line_for_line(name, parts, kw):
    mine = sp.encode((DATA / name).read_bytes(), name, **kw)
    assert [fn for fn, _ in mine] == parts
    for (_, text), ref_name in zip(mine, parts):
        ref = (DATA / ref_name).read_bytes().split(b"\n")
        ours = text.split(b"\n")
        assert len(ours) == len(ref)
        assert ours[:-2] == ref[:-2]                         # headers, the long name, every code line
        assert ours[-2].startswith(ref[-2][:ref[-2].index(b"[")])  # " stop_7+. (NAME.P01/04) ["
        assert len(ours[-2]) == 69 and ours[-2][62:66] == b"\xb0\xb1\xb2\xdb"


def test_parts_in_bbs_messages_any_order_with_crs_and_headers():
    data = random.Random(4).randbytes(3000)
    parts = sp.encode(data, "QSL card.png", lines_per_part=12)
    assert len(parts) == 5
    messages = []
    for fn, text in parts:
        body = text.replace(b"\n", b"\r")
        messages.append(b"R:261020/1200Z @:GB7SIM.#78.GBR.EURO\rFrom: G0ABC\rTo: ALL\rSubject: " + fn.encode()
                        + b"\r\r" + body + b"\r/EX\r")
    c = sp.Collector()
    for m in reversed(messages):
        c.feed(m)
    r = c.result()
    assert r.complete and r.data == data and r.name == "QSL card.png"


def test_a_missing_part_and_a_damaged_line_are_reported_not_guessed():
    data = random.Random(5).randbytes(2000)
    parts = sp.encode(data, "log.adi", lines_per_part=10)
    lines = parts[0][1].split(b"\n")
    damaged = bytearray(lines[5])
    damaged[10] = 0x41 if damaged[10] != 0x41 else 0x42
    lines[5] = bytes(damaged)
    r = sp.decode(b"\n".join(lines), parts[1][1], parts[3][1])
    assert not r.complete and r.data is None
    assert r.bad_lines == 1
    assert (1, 3) in r.missing                                # the damaged line (after the two header lines)
    assert [m for m in r.missing if m[0] == 3] == [(3, n) for n in range(10)]


def test_bit_seven_stripped_on_the_way_is_said():
    text = sp.encode(b"hello" * 50, "x.txt")[0][1]
    stripped = bytes(b & 0x7F for b in text)
    r = sp.decode(stripped)
    assert r.stripped and not r.complete


def test_limits():
    with pytest.raises(ValueError):
        sp.encode(b"", "empty")
    with pytest.raises(ValueError):
        sp.encode(bytes(62 * 256), "big.bin", lines_per_part=1)      # 256 parts
    assert sp.dos_name("My Long Photo.JPEG") == ("my_long_", "jpe")
    assert sp.find(sp.encode(b"x", "a")[0][1]) and not sp.find("an ordinary message")
