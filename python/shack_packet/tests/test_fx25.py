"""FX.25: the Reed-Solomon codes to their limit and one past it, the tags,
the block layout a plain receiver still decodes, and the receiver fed bits."""

from __future__ import annotations

import random

import numpy as np
import pytest
from helpers import frame_bytes

from shack_packet import fx25, hdlc


@pytest.mark.parametrize("nroots", [16, 32, 64])
def test_rs_corrects_up_to_half_the_check_bytes_and_refuses_one_more(nroots):
    rng = random.Random(nroots)
    rs = fx25.codec(nroots)
    for trial in range(25):
        data = bytes(rng.randrange(256) for _ in range(rs.k))
        block = data + rs.encode(data)
        assert not rs.syndromes(block).any()
        errors = nroots // 2 if trial % 2 else rng.randint(1, nroots // 2)
        where = rng.sample(range(255), errors)
        bad = bytearray(block)
        for at in where:
            bad[at] ^= rng.randrange(1, 256)
        fixed, positions = rs.decode(bytes(bad))
        assert fixed == block and sorted(positions) == sorted(where)
    # One more than the code can carry: refused, never "corrected" into
    # something else (for these seeds; in general RS can miscorrect, which
    # is why the frame's own FCS is checked after).
    bad = bytearray(block)
    for at in rng.sample(range(255), nroots // 2 + 1):
        bad[at] ^= rng.randrange(1, 256)
    assert rs.decode(bytes(bad)) is None


def test_rs_matches_karns_generator():
    # RS(255,239) with Karn's parameters (0x11D, fcr 1, prim 1): the encoder's
    # check bytes are the remainder of data(x) x^16 by g(x), so a codeword
    # evaluates to zero at every root a^1..a^16.
    rs = fx25.codec(16)
    data = bytes(range(239))
    word = data + rs.encode(data)
    for j in range(1, 17):
        root = int(fx25.EXP[j])
        acc = 0
        for byte in word:
            acc = fx25._mul(acc, root) ^ byte
        assert acc == 0, j


def test_any_two_tags_differ_in_exactly_32_bits():
    values = [t.value for t in fx25.TAGS.values()]
    for i, a in enumerate(values):
        for b in values[i + 1:]:
            assert bin(a ^ b).count("1") == 32


@pytest.mark.parametrize("check,length,tag", [
    (16, 32, 4), (16, 64, 3), (16, 128, 2), (16, 239, 1), (16, 240, None),
    (32, 32, 8), (32, 64, 7), (32, 128, 6), (32, 223, 5), (32, 234, None),
    (64, 64, 11), (64, 128, 10), (64, 191, 9), (64, 192, None),
    (None, 32, 4), (None, 33, 3), (None, 64, 3), (None, 65, 6), (None, 128, 6),
    (None, 191, 9), (None, 223, 5), (None, 239, 1), (None, 240, None),
])
def test_the_code_is_chosen_as_direwolf_chooses_it(check, length, tag):
    # direwolf's own assertions (fx25_init.c), so a station here picks the
    # code a direwolf beside it would.
    got = fx25.pick_tag(length, check)
    assert (got.number if got else None) == tag


def bits_with_errors(frame, check=None, byte_errors=0, tag_errors=0, seed=0):
    rng = random.Random(seed)
    bits = fx25.frame_bits(frame, check=check)
    tag_bits, rest = bits[:64], bits[64:]
    for at in rng.sample(range(64), tag_errors):
        tag_bits[at] ^= 1
    block = bytearray(sum(rest[i * 8 + k] << k for k in range(8)) for i in range(len(rest) // 8))
    for at in rng.sample(range(len(block)), byte_errors):
        block[at] ^= rng.randrange(1, 256)
    return tag_bits + hdlc.byte_bits(bytes(block))


@pytest.mark.parametrize("check", [16, 32, 64])
def test_the_receiver_corrects_a_block_to_the_codes_limit(check):
    frame = frame_bytes("GM0ABC-9", "APDW17", "!5537.14N/00420.83W>FX.25 " + "x" * 30, via=["WIDE1-1"])
    noise = np.random.default_rng(check).integers(0, 2, 300)
    for errors in (0, check // 4, check // 2):
        rx = fx25.Fx25Receiver()
        got = rx.feed(np.concatenate([noise, bits_with_errors(frame, check, errors, 8, seed=errors), noise]))
        assert [g.frame for g in got] == [frame]
        assert got[0].corrected == errors
        assert fx25.TAGS[got[0].tag].nroots == check
    rx = fx25.Fx25Receiver()
    assert rx.feed(np.concatenate([noise, bits_with_errors(frame, check, check // 2 + 1, seed=9), noise])) == []
    assert rx.failed == 1


def test_a_tag_with_nine_wrong_bits_is_not_recognised():
    frame = frame_bytes("GM0ABC", "APDW17", ">status")
    rx = fx25.Fx25Receiver()
    assert rx.feed(bits_with_errors(frame, 16, 0, tag_errors=9) + [0] * 64) == []
    assert rx.tags == 0


def test_the_receiver_streams_a_block_across_any_number_of_calls():
    frame = frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>split")
    bits = np.array([1, 0] * 100 + bits_with_errors(frame, 32, 5, 3) + [0, 1] * 50, dtype=np.int8)
    whole = [g.frame for g in fx25.Fx25Receiver().feed(bits)]
    for size in (1, 7, 64, 333):
        rx = fx25.Fx25Receiver()
        pieces = []
        for at in range(0, len(bits), size):
            pieces += [g.frame for g in rx.feed(bits[at:at + size])]
        assert pieces == whole == [frame], size


def test_a_plain_receiver_decodes_the_same_frame_out_of_an_fx25_block():
    """The compatibility FX.25 exists for: a station that knows nothing of it
    sees noise, then an ordinary frame between flags."""
    frame = frame_bytes("GM0ABC-9", "APDW17", ">heard by everyone")
    for check in (16, 32, 64):
        bits = list(hdlc.FLAG_BITS) * 4 + fx25.frame_bits(frame, check) + list(hdlc.FLAG_BITS) * 2
        assert hdlc.Framer().feed(hdlc.nrzi(bits)[0]) == [frame]


def test_a_frame_too_long_for_fx25_has_no_fx25_form():
    frame = frame_bytes("GM0ABC", "APDW17", "", info_bytes=bytes(240))
    assert fx25.frame_bits(frame) is None
    assert fx25.frame_bits(frame_bytes("GM0ABC", "APDW17", "", info_bytes=bytes(180))) is not None


def test_a_correction_in_the_part_never_sent_is_refused():
    """RS can 'correct' a hopeless block into another codeword. The shortened
    bytes are known zeros, so a correction there means exactly that."""
    tag = fx25.TAGS[0x04]                       # RS(48,32): 207 known zeros
    rs = fx25.codec(tag.nroots)
    data = bytes(range(32)) + bytes(rs.k - 32)
    block = bytearray(data + rs.encode(data))
    block[100] ^= 0x55                           # inside the never-sent zeros
    assert rs.decode(bytes(block)) is not None
    assert rs.decode(bytes(block), forbidden=range(32, rs.k)) is None


def test_unstuffing_refuses_what_is_not_a_frame():
    frame = frame_bytes("GM0ABC", "APDW17", ">x")
    good = fx25.data_block_bits(frame) + list(hdlc.FLAG_BITS) * 2
    data = bytes(sum(good[i * 8 + k] << k for k in range(8)) for i in range(len(good) // 8))
    assert fx25.unstuff_block(data) == hdlc.with_fcs(frame)
    assert fx25.unstuff_block(b"\x00" + data[1:]) is None      # no leading flag
    assert fx25.unstuff_block(data[:1] + b"\xff\xff" + data[3:]) is None   # seven ones
    assert fx25.unstuff_block(data[:4]) is None                 # no closing flag
