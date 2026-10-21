"""The framer and the transmit side's bits: the check sequence, stuffing,
flags and NRZI, one bit at a time where that is the point."""

from __future__ import annotations

from helpers import frame_bytes, on_air

from shack_packet import hdlc


def test_the_fcs_leaves_the_residue_x25_specifies():
    # Running the CRC over a frame *including* its own check sequence leaves
    # 0xF0B8. If the polynomial, the direction, the seed or the final
    # complement were wrong, this would not hold for any input at all.
    for payload in (b"", b"A", b"\x00" * 20, bytes(range(256)), b"the quick brown fox"):
        whole = hdlc.with_fcs(payload)
        crc = 0xFFFF
        for byte in whole:
            crc = (crc >> 8) ^ hdlc._FCS[(crc ^ byte) & 0xFF]
        assert crc == 0xF0B8, payload
        assert hdlc.fcs_ok(whole)


def test_the_fcs_matches_a_published_value():
    # CRC-16/X-25 of "123456789" is 0x906E (the catalogue's check value).
    assert hdlc.fcs(b"123456789") == 0x906E


def test_one_wrong_bit_anywhere_fails_the_check():
    frame = on_air(frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>x"))
    for index in range(len(frame)):
        for bit in range(8):
            broken = bytearray(frame)
            broken[index] ^= 1 << bit
            assert not hdlc.fcs_ok(bytes(broken)), (index, bit)


def test_a_frame_survives_flags_stuffing_and_nrzi():
    frame = frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>hello", via=["WIDE1-1", "WIDE2-1"])
    framer = hdlc.Framer()
    assert framer.feed(hdlc.encode(frame)) == [frame]
    assert framer.frames == 1 and framer.bad_fcs == 0


def test_payloads_full_of_ones_are_stuffed_and_unstuffed():
    # 0xFF is eight ones, 0x7E is the flag itself, 0x7F is the pattern the
    # stuffer exists to prevent.
    for payload in (b"\xff" * 12, b"\x7e" * 12, b"\x7f\xff\x7f\xff", bytes(range(200))):
        frame = frame_bytes("GM0ABC", "APDW17", "", info_bytes=payload)
        bits = hdlc.frame_bits(frame)
        run = longest = 0
        for bit in bits[8:-8]:
            run = run + 1 if bit else 0
            longest = max(longest, run)
        assert longest <= 5, "six ones inside a frame would read as a flag"
        assert hdlc.Framer().feed(hdlc.encode(frame)) == [frame]


def test_a_corrupted_frame_is_discarded_not_half_parsed():
    frame = frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>hello")
    good = hdlc.encode(frame)
    framer = hdlc.Framer()
    # One line level inverted in the middle of the information field.
    broken = list(good)
    broken[64 + 200] ^= 1
    assert framer.feed(broken) == []
    assert framer.flags > 0, "no flags were even seen, so this proved nothing"
    assert framer.bad_fcs == 1 and framer.frames == 0


def test_an_abort_in_the_middle_throws_the_frame_away():
    levels = hdlc.encode(frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>hello"))
    framer = hdlc.Framer()
    half = 64 + 60
    framer.feed(levels[:half])
    framer.feed([levels[half - 1]] * 15)           # fifteen ones: a steady tone
    framer.feed(levels[half:])
    assert framer.frames == 0
    assert framer.aborted >= 1


def test_a_frame_that_is_not_a_whole_number_of_bytes_is_refused():
    levels = hdlc.encode(frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>hello"))
    opening, body, closing = levels[:64], levels[64:-16], levels[-16:]
    framer = hdlc.Framer()
    framer.feed(opening)
    framer.feed(body)
    framer.feed([body[-1] ^ 1])                     # one bit too many
    framer.feed(closing)
    assert framer.frames == 0


def test_back_to_back_flags_produce_nothing():
    framer = hdlc.Framer()
    assert framer.feed(hdlc.nrzi(list(hdlc.FLAG_BITS) * 40)[0]) == []
    assert framer.flags >= 39
    assert framer.frames == 0 and framer.bad_fcs == 0


def test_two_frames_sharing_one_flag_both_come_out():
    first = frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>one")
    second = frame_bytes("MM0XYZ", "APDW17", ">two")
    bits = list(hdlc.FLAG_BITS) * 4 + hdlc.frame_bits(first, 0, 1) + hdlc.frame_bits(second, 0, 2)
    assert hdlc.Framer().feed(hdlc.nrzi(bits)[0]) == [first, second]


def test_bit_at_a_time_gives_the_same_answer():
    frame = frame_bytes("GM0ABC", "APDW17", "!5537.14N/00420.83W>hello", via=["WIDE2-1"])
    levels = hdlc.encode(frame)
    framer = hdlc.Framer()
    piecemeal = []
    for level in levels:
        piecemeal += framer.feed([level])
    assert piecemeal == hdlc.Framer().feed(levels) == [frame]


def test_nrzi_continues_across_pieces():
    bits = [0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 1, 0]
    whole, _ = hdlc.nrzi(bits)
    first, level = hdlc.nrzi(bits[:5])
    second, _ = hdlc.nrzi(bits[5:], level)
    assert first + second == whole


def test_the_largest_frame_comes_through_and_one_byte_more_does_not():
    biggest = frame_bytes("GM0ABC", "APDW17", "", info_bytes=bytes(hdlc.MAX_FRAME - 2 - 16))
    assert len(biggest) + 2 == hdlc.MAX_FRAME
    assert hdlc.Framer().feed(hdlc.encode(biggest)) == [biggest]
    too_big = biggest + b"\x00"
    assert hdlc.Framer().feed(hdlc.encode(too_big)) == []
