"""The AX.25 frame codec: addresses and paths, the control field for every
frame type at both moduli, PIDs, and rubbish that must not parse."""

from __future__ import annotations

import random

import pytest
from helpers import address, frame_bytes

from shack_packet import ax25
from shack_packet.ax25 import Control, Frame, FrameError, FrameType


def test_addresses_ssids_and_a_digipeater_path():
    frame = ax25.parse(frame_bytes("GM0ABC-9", "APDW17", "!x", via=["GB7AY*", "WIDE2-1"]))
    assert str(frame.source) == "GM0ABC-9" and str(frame.destination) == "APDW17"
    assert frame.path == ["GB7AY*", "WIDE2-1"]
    assert frame.is_ui and frame.pid == ax25.PID_NONE and frame.info == b"!x"
    # Heard from GB7AY's transmitter, not GM0ABC's: what matters for a map.
    assert frame.heard_from == "GB7AY" and frame.digipeated
    assert frame.monitor() == "GM0ABC-9>APDW17,GB7AY*,WIDE2-1:!x"


def test_a_frame_heard_direct_says_so():
    frame = ax25.parse(frame_bytes("GM0ABC-9", "APDW17", ">hello", via=["WIDE1-1", "WIDE2-1"]))
    assert frame.heard_from == "GM0ABC-9" and not frame.digipeated


def test_the_encoder_writes_the_bytes_the_protocol_defines():
    # Checked against the hand-built bytes, not against the parser.
    frame = Frame.from_monitor("GM0ABC-9>APDW17,GB7AY*,WIDE2-1:!x")
    want = bytearray(frame_bytes("GM0ABC-9", "APDW17", "!x", via=["GB7AY*", "WIDE2-1"]))
    want[6] |= 0x80                       # from_monitor builds a command: C bit on the destination
    assert frame.encode() == bytes(want)


def test_rubbish_that_passes_the_check_sequence_is_still_refused():
    good = frame_bytes("GM0ABC", "APDW17", "!x")
    assert ax25.parse(good) is not None
    broken = bytearray(good)
    broken[2] = ord("!") << 1             # a callsign character that cannot be one
    assert ax25.parse(bytes(broken)) is None
    broken = bytearray(good)
    broken[3] |= 1                        # end-of-address bit inside a callsign
    assert ax25.parse(bytes(broken)) is None
    broken = bytearray(good)
    broken[13] &= 0xFE                    # an address list that never ends
    assert ax25.parse(bytes(broken)) is None
    for short in (b"", b"\x00" * 10, b"\x00" * 14):
        assert ax25.parse(short) is None


def test_more_than_eight_digipeaters_is_not_a_frame():
    raw = frame_bytes("GM0ABC", "APDW17", "!x", via=[f"WIDE{i}-1" for i in range(1, 10)])
    assert ax25.parse(raw) is None
    with pytest.raises(FrameError):
        Frame.ui("GM0ABC", "APDW17", "!x", via=[f"WIDE{i}-1" for i in range(1, 10)])


# -- the control field --------------------------------------------------------

# (type, pf, nr, ns, modulo) -> bytes, from AX.25 2.2 §4.3 (figures 4.1-4.6),
# worked by hand: e.g. RR with N(R)=5 and P set is 101 1 00 01 = 0xB1.
KNOWN = [
    (FrameType.SABM, False, None, None, 8, b"\x2f"),
    (FrameType.SABM, True, None, None, 8, b"\x3f"),
    (FrameType.SABME, True, None, None, 8, b"\x7f"),
    (FrameType.UA, True, None, None, 8, b"\x73"),
    (FrameType.DISC, True, None, None, 8, b"\x53"),
    (FrameType.DM, True, None, None, 8, b"\x1f"),
    (FrameType.FRMR, False, None, None, 8, b"\x87"),
    (FrameType.UI, False, None, None, 8, b"\x03"),
    (FrameType.UI, True, None, None, 8, b"\x13"),
    (FrameType.XID, True, None, None, 8, b"\xbf"),
    (FrameType.TEST, False, None, None, 8, b"\xe3"),
    (FrameType.RR, True, 5, None, 8, b"\xb1"),
    (FrameType.RNR, False, 2, None, 8, b"\x45"),
    (FrameType.REJ, False, 7, None, 8, b"\xe9"),
    (FrameType.SREJ, True, 0, None, 8, b"\x1d"),
    (FrameType.I, False, 5, 3, 8, b"\xa6"),
    (FrameType.I, True, 7, 7, 8, b"\xfe"),
    (FrameType.I, False, 100, 37, 128, bytes([37 << 1, 100 << 1])),
    (FrameType.I, True, 127, 0, 128, bytes([0, (127 << 1) | 1])),
    (FrameType.RR, True, 64, None, 128, bytes([0x01, (64 << 1) | 1])),
    (FrameType.RNR, False, 1, None, 128, bytes([0x05, 2])),
    (FrameType.REJ, False, 99, None, 128, bytes([0x09, 99 << 1])),
    (FrameType.SREJ, True, 127, None, 128, bytes([0x0D, 0xFF])),
    (FrameType.SABME, False, None, None, 128, b"\x6f"),
]


@pytest.mark.parametrize("kind,pf,nr,ns,modulo,raw", KNOWN)
def test_every_control_field_encodes_and_decodes(kind, pf, nr, ns, modulo, raw):
    control = Control(kind, pf, nr, ns, modulo)
    assert control.encode() == raw
    assert Control.decode(raw + b"\x99", modulo) == control


def test_every_type_and_sequence_number_round_trips():
    for modulo in (8, 128):
        for kind in FrameType:
            numbered = kind is FrameType.I or kind in (FrameType.RR, FrameType.RNR, FrameType.REJ, FrameType.SREJ)
            for pf in (False, True):
                for nr in (range(modulo) if numbered else [None]):
                    ns = (nr * 3 + 1) % modulo if kind is FrameType.I else None
                    control = Control(kind, pf, nr, ns, modulo)
                    assert Control.decode(control.encode(), modulo) == control


def test_sequence_numbers_outside_the_modulus_are_refused():
    with pytest.raises(FrameError):
        Control(FrameType.RR, nr=8)
    with pytest.raises(FrameError):
        Control(FrameType.I, nr=0, ns=128, modulo=128)
    with pytest.raises(FrameError):
        Control(FrameType.I, nr=0)            # an I frame needs N(S)


def test_a_control_byte_that_names_no_frame_is_refused():
    # U-frame patterns AX.25 does not define (0x07 is SNRM's, never used).
    for byte in (0x07, 0x0B, 0x23, 0x27, 0x9F):
        assert Control.decode(bytes([byte])) is None, hex(byte)
    raw = frame_bytes("G0ABC", "G4XYZ", "", control=0x07, pid=None)
    assert ax25.parse(raw) is None


def test_reserved_bits_in_a_modulo_128_s_frame_are_refused():
    assert Control.decode(bytes([0x11, 0x00]), 128) is None


# -- whole frames -------------------------------------------------------------


def test_connected_mode_frames_build_and_parse():
    cases = [
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.SABM, pf=True)),
        Frame.build("GB7BBS", "G0ABC", Control(FrameType.UA, pf=True), command=False),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.I, False, 0, 0), b"hello\r", pid=ax25.PID_NONE),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.I, True, 3, 6), b"\x45\x00", pid=ax25.PID_IP),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.UI), b"x", pid=ax25.PID_ARP),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.UI), b"\xff", pid=ax25.PID_NETROM),
        Frame.build("GB7BBS", "G0ABC", Control(FrameType.RR, True, 7), command=False),
        Frame.build("GB7BBS", "G0ABC", Control(FrameType.REJ, False, 2), command=False),
        Frame.build("GB7BBS", "G0ABC", Control(FrameType.FRMR, True), b"\x01\x02\x03", command=False),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.XID, True), b"\x82\x80\x00\x00"),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.TEST, True), b"ping"),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.DISC, True), via=["GB7AY", "MB7UZ-1"]),
        Frame.build("GB7BBS", "G0ABC", Control(FrameType.DM, True), command=False),
    ]
    for frame in cases:
        back = ax25.parse(frame.encode())
        assert back is not None, frame
        assert back.control == frame.control and back.pid == frame.pid and back.info == frame.info
        assert back.encode() == frame.encode()
        assert back.command == (frame.destination.flag if frame.destination.flag != frame.source.flag else None)

    frames128 = [
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.SABME, pf=True)),
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.I, True, 100, 120, 128), b"data"),
        Frame.build("GB7BBS", "G0ABC", Control(FrameType.SREJ, True, 77, modulo=128), command=False),
    ]
    for frame in frames128:
        back = ax25.parse(frame.encode(), modulo=128)
        assert back.control == frame.control and back.encode() == frame.encode()


def test_command_and_response_bits():
    command = Frame.build("G0ABC", "GB7BBS", Control(FrameType.SABM, pf=True))
    response = Frame.build("GB7BBS", "G0ABC", Control(FrameType.UA, pf=True), command=False)
    assert command.encode()[6] & 0x80 and not command.encode()[13] & 0x80
    assert response.encode()[13] & 0x80 and not response.encode()[6] & 0x80
    assert ax25.parse(command.encode()).command is True
    assert ax25.parse(response.encode()).command is False
    old = frame_bytes("G0ABC", "APRS", "x")      # both C bits clear: AX.25 1.x
    assert ax25.parse(old).command is None


def test_frames_that_carry_no_pid_refuse_one():
    with pytest.raises(FrameError):
        Frame.build("G0ABC", "GB7BBS", Control(FrameType.SABM), pid=ax25.PID_NONE)


def test_callsigns_are_checked_when_building():
    for bad in ("", "G0ABCDE", "G0-ABC", "G0ABC-16", "G0ABC-X", "G0 AB", "G0ÄBC"):
        with pytest.raises(FrameError):
            Address.from_text(bad)
    assert Address.from_text("g0abc-15") == Address("G0ABC", 15)


def test_a_parsed_frame_re_encodes_to_the_same_bytes():
    # Including the reserved bits, which some networks use: a digipeater
    # re-encodes what it heard.
    raw = bytearray(frame_bytes("GM0ABC-9", "APDW17", "!x", via=["GB7AY*", "WIDE2-1"]))
    raw[6] &= ~0x20 & 0xFF
    raw[13] &= ~0x40 & 0xFF
    frame = ax25.parse(bytes(raw))
    assert frame.destination.reserved == 0b10 and frame.source.reserved == 0b01
    assert frame.encode() == bytes(raw)


def test_random_bytes_never_raise_and_what_parses_round_trips():
    """A stranger's frame - or noise that happened to pass the FCS - must
    never raise, and whatever parses must be exactly what was sent."""
    rng = random.Random(25)
    good = [Frame.from_monitor("GM0ABC-9>APDW17,WIDE1-1:!5537.14N/00420.83W>x").encode(),
            Frame.build("G0ABC", "GB7BBS", Control(FrameType.I, True, 3, 6), b"hello").encode()]
    parsed = 0
    for i in range(20_000):
        if i % 2:
            raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        else:
            raw = bytearray(rng.choice(good))
            for _ in range(rng.randrange(1, 4)):
                raw[rng.randrange(len(raw))] ^= 1 << rng.randrange(8)
            if rng.random() < 0.2:
                raw = raw[:rng.randrange(len(raw))]
            raw = bytes(raw)
        for modulo in (8, 128):
            frame = ax25.parse(raw, modulo)
            if frame is not None:
                parsed += 1
                assert frame.encode() == raw
                frame.monitor()
    assert parsed > 1000, "the mutations never produced a parseable frame, so this proved little"


Address = ax25.Address
