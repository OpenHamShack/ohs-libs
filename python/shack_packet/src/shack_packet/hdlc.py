"""Bits in, whole frames out, and frames out as bits: NRZI, bit stuffing,
flags and the check sequence.

Copied from the Icom app's APRS receiver (`icom/app/aprs/hdlc.py`, 17/10/2026)
and grown a transmit side; the receive half is the one that has decoded
direwolf's audio packet for packet since it was written. AX.25 puts four
things between the modem and a frame, and each of them exists to solve a
problem the one below it created.

**NRZI.** The data is in the transitions: a zero changes the tone, a one leaves
it alone. So a receiver that is wired backwards, or an audio path that inverts
somewhere, decodes exactly the same - which is why the demodulator never
decides which tone is a one.

**The flag, 0x7E, six ones between two zeros.** It marks both ends of every
frame and fills the gaps. Frames are found by looking for it, not by being told
where to start.

**Bit stuffing.** Because the flag is the only thing allowed to have six ones
in a row, the transmitter inserts a zero after any five ones in the data and
the receiver takes it out again. Seven or more ones is not data at all: it is
an abort, or an idle line, and whatever was being collected is thrown away.

**The frame check sequence**, CRC-16 as ITU-T X.25 defines it: polynomial
0x1021 taken least-significant-bit first (so 0x8408 in the table), started at
all ones, complemented at the end, sent low byte first. A frame whose FCS does
not match is discarded whole. Plain AX.25 has no forward error correction -
that is FX.25's job (fx25.py) - and half a frame is worse than none.

**The subtlety worth writing down.** When the closing flag is recognised, its
first seven bits have already been shifted into the byte being assembled -
recognition happens on the eighth. So a frame that ended on a byte boundary
leaves exactly seven bits pending, and that is checked rather than assumed: it
is a free test that the frame was byte-aligned, and rejecting the ones that are
not costs nothing. Getting this off by one bit produces a decoder that finds
flags, counts frames, and never passes the FCS - which is how it presented.

**Frames here never carry their FCS.** `Framer` checks it and hands over the
bytes before it; `frame_bits` computes it and appends it. Every layer above
works on the frame as AX.25 defines it, without the two bytes that belong to
the link, so nothing above has to remember to strip them (the Icom copy
returned them, and its parser stripped them again).
"""

from __future__ import annotations

FLAG = 0x7E

#: The smallest thing that can be a frame: two addresses and a control byte
#: (15 bytes; an RR or a DISC is exactly that), plus the FCS.
MIN_FRAME = 17

#: Big enough for an AX.25 2.2 frame with a 2048-byte information field (the
#: largest N1 anything negotiates with XID), eight digipeaters and a modulo-128
#: control field; past that a stuck receiver is growing a buffer for nothing.
#: The Icom copy stopped at 350, which is APRS-sized and would have refused a
#: connected-mode station with PACLEN raised.
MAX_FRAME = 2048 + 72 + 3 + 2


def _table():
    out = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        out.append(crc)
    return tuple(out)


_FCS = _table()


def fcs(data: bytes) -> int:
    """The X.25 frame check sequence over `data`."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _FCS[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFF


def fcs_ok(frame: bytes) -> bool:
    """Whether a frame's last two bytes are the check sequence over the rest."""
    if len(frame) < 2:
        return False
    return fcs(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def with_fcs(frame: bytes) -> bytes:
    """The frame as it goes on the air: its FCS appended, low byte first."""
    return bytes(frame) + fcs(frame).to_bytes(2, "little")


# -- transmit ----------------------------------------------------------------


def byte_bits(data: bytes) -> list[int]:
    """Bytes as bits, least significant first, which is AX.25's order."""
    return [(byte >> i) & 1 for byte in data for i in range(8)]


FLAG_BITS = tuple(byte_bits(bytes([FLAG])))


def stuff(data: bytes) -> list[int]:
    """`data`'s bits with a zero after every five ones in a row."""
    out: list[int] = []
    ones = 0
    for byte in data:
        for i in range(8):
            bit = (byte >> i) & 1
            out.append(bit)
            if bit:
                ones += 1
                if ones == 5:
                    out.append(0)
                    ones = 0
            else:
                ones = 0
    return out


def frame_bits(frame: bytes, flags_before: int = 1, flags_after: int = 1) -> list[int]:
    """One frame (without its FCS) as data bits: flags, the stuffed frame and
    its FCS, flags. Data bits, before NRZI; `nrzi` turns them into what the
    modulator sends.

    One flag either side is all a frame needs, and two frames in a row share
    the flag between them, which is what a TNC sends. The preamble that lets a
    receiver's squelch open and its bit clock settle is TXDELAY, a property of
    the transmission rather than of a frame (afsk.Modulator).
    """
    return list(FLAG_BITS) * flags_before + stuff(with_fcs(frame)) + list(FLAG_BITS) * flags_after


def nrzi(bits, level: int = 0) -> tuple[list[int], int]:
    """Data bits to line levels: a zero changes the level, a one keeps it.
    Returns the levels and the level the line was left at, so a transmission
    built in pieces stays continuous across them."""
    out = []
    for bit in bits:
        if not bit:
            level ^= 1
        out.append(level)
    return out, level


def encode(frame: bytes, flags_before: int = 8, flags_after: int = 2) -> list[int]:
    """A frame as the line levels a receiver would see, flags either side.
    The shortcut the tests use to build a framing case bit by bit."""
    return nrzi(frame_bits(frame, flags_before, flags_after))[0]


# -- receive -----------------------------------------------------------------


class Framer:
    """NRZI-decoded bits in, checked frames out.

    Streaming and stateful, like the demodulator above it and for the same
    reason: a frame is thousands of bit times long and the audio arrives in
    fragments a fraction of that. Everything - the NRZI history, the stuffing
    counter, the partial byte - has to survive the join.
    """

    def __init__(self):
        self._last_level = 0
        self._shift = 0            # the last eight bits, for finding a flag
        self._ones = 0
        self._byte = 0
        self._bits = 0
        self._buf = bytearray()
        self._in_frame = False
        #: How many line bits the frame being collected has taken so far,
        #: stuffing included: the receiver dates a frame by where it started.
        self.frame_bits = 0
        #: Counters, because a receiver that hears flags and passes nothing is
        #: a very different fault from one that hears nothing at all, and they
        #: are indistinguishable from the outside without these.
        self.flags = 0
        self.frames = 0
        self.bad_fcs = 0
        self.aborted = 0

    def feed(self, levels) -> list[bytes]:
        """Line levels (before NRZI is undone), one call per batch."""
        out: list[bytes] = []
        for level in levels:
            frame = self.feed_bit(1 if level == self._last_level else 0)
            self._last_level = level
            if frame is not None:
                out.append(frame)
        return out

    def feed_bit(self, bit: int) -> bytes | None:
        """One NRZI-decoded bit. Returns a frame when one has just closed.

        Public because it is the natural seam for a test: a framing bug is a
        bug about a particular bit in a particular place, and being able to
        hand it that bit is the difference between a test that localises a
        fault and one that says "no frames".
        """
        self._shift = ((self._shift >> 1) | (bit << 7)) & 0xFF
        self.frame_bits += 1

        if self._shift == FLAG:
            self.flags += 1
            frame = self._close()
            self._buf = bytearray()
            self._byte = self._bits = self._ones = 0
            self._in_frame = True
            self.frame_bits = 0
            return frame

        if bit:
            self._ones += 1
        else:
            if self._ones == 5:
                # The stuffed zero. It was never data; drop it and do not let
                # it into the byte being assembled.
                self._ones = 0
                return None
            self._ones = 0

        if self._ones >= 7:
            # Seven ones cannot occur inside a stuffed frame, so this is an
            # abort or an idle line. Whatever was collected is not a frame.
            if self._in_frame and self._buf:
                self.aborted += 1
            self._in_frame = False
            self._buf = bytearray()
            self._byte = self._bits = 0
            return None

        if not self._in_frame:
            return None

        self._byte = ((self._byte >> 1) | (bit << 7)) & 0xFF
        self._bits += 1
        if self._bits == 8:
            self._buf.append(self._byte)
            self._byte = self._bits = 0
            if len(self._buf) > MAX_FRAME:
                self._in_frame = False
                self._buf = bytearray()
        return None

    def _close(self) -> bytes | None:
        """What was collected, if it is a frame. See the note in the docstring
        about the seven pending bits."""
        if not self._in_frame:
            return None
        if self._bits != 7:
            # Not byte-aligned against the closing flag, so whatever this is,
            # it is not a frame that was sent - most often the tail of one
            # whose beginning was missed.
            return None
        frame = bytes(self._buf)
        if len(frame) < MIN_FRAME:
            return None
        if not fcs_ok(frame):
            self.bad_fcs += 1
            return None
        self.frames += 1
        return frame[:-2]
