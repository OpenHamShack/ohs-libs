"""XID: the AX.25 2.2 parameter negotiation (§4.3.3.7 and §6.3.2).

Two stations that have connected with SABME exchange one XID command and
response saying what each can receive: the window, the longest
information field, the acknowledgement timer and the retry count, and
which optional functions (REJ, SREJ, modulo 128) it does. Each then sends
no more than the other said it could take.

The information field is ISO 8885's: a format indicator (0x82), a group
identifier (0x80), the group's length, then parameters as
identifier/length/value, values most significant byte first. The bit
values here are the ones direwolf 1.7 sends and expects (its xid.c),
because the specification's figures number the bits in a way two
implementations have read differently and direwolf is the 2.2
implementation most stations meet; tests/test_direwolf_link.py checks
them against a live direwolf.

Nothing here is trusted: an XID from a stranger that does not parse is
answered with our own parameters and otherwise ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

FI = 0x82
GI = 0x80

PI_CLASSES = 2
PI_HDLC_OPTIONS = 3
PI_N1_RX = 6
PI_WINDOW_RX = 8
PI_ACK_TIMER = 9
PI_RETRIES = 10
#: OpenHamShack's link adaptation (adapt.py): three bytes, a version (1),
#: the top mode this station offers and the mode it asks the other to send
#: to it (modem.CODES). Outside the parameters 2.2 defines, so another
#: station skips it (direwolf's xid.c and decode below skip any PI they do
#: not know).
PI_OHS_MODES = 0xF0
OHS_VERSION = 1

CLASS_BALANCED_ABM = 0x0100
CLASS_HALF_DUPLEX = 0x2000
CLASS_FULL_DUPLEX = 0x4000

OPT_REJ = 0x020000
OPT_SREJ = 0x040000
OPT_EXTENDED_ADDRESS = 0x800000
OPT_MODULO_8 = 0x000400
OPT_MODULO_128 = 0x000800
OPT_TEST = 0x002000
OPT_FCS_16 = 0x008000
OPT_SYNCHRONOUS_TX = 0x000002
OPT_MULTI_SREJ = 0x000020


@dataclass
class Parameters:
    """What one side can receive. None where the XID did not say."""
    half_duplex: bool | None = True
    rej: bool | None = True
    srej: bool | None = False
    modulo: int | None = 8
    n1_rx: int | None = 256           # bytes (sent in bits)
    window_rx: int | None = 4
    ack_timer_ms: int | None = 3000
    retries: int | None = 10
    #: (top, request) as modem.CODES numbers, or None: link adaptation.
    ohs_modes: tuple[int, int] | None = None

    def encode(self) -> bytes:
        params = bytearray()

        def put(pi, value, length):
            params.extend([pi, length])
            params.extend(value.to_bytes(length, "big"))

        classes = CLASS_BALANCED_ABM | (CLASS_HALF_DUPLEX if self.half_duplex else CLASS_FULL_DUPLEX)
        put(PI_CLASSES, classes, 2)
        options = OPT_EXTENDED_ADDRESS | OPT_TEST | OPT_FCS_16 | OPT_SYNCHRONOUS_TX
        options |= OPT_REJ if self.rej else 0
        options |= OPT_SREJ if self.srej else 0
        options |= OPT_MODULO_128 if self.modulo == 128 else OPT_MODULO_8
        put(PI_HDLC_OPTIONS, options, 3)
        if self.n1_rx is not None:
            put(PI_N1_RX, min(0xFFFF, self.n1_rx * 8), 2)
        if self.window_rx is not None:
            put(PI_WINDOW_RX, self.window_rx, 1)
        if self.ack_timer_ms is not None:
            put(PI_ACK_TIMER, min(0xFFFF, self.ack_timer_ms), 2)
        if self.retries is not None:
            put(PI_RETRIES, self.retries, 1)
        if self.ohs_modes is not None:
            put(PI_OHS_MODES, (OHS_VERSION << 16) | (self.ohs_modes[0] << 8) | self.ohs_modes[1], 3)
        return bytes([FI, GI]) + len(params).to_bytes(2, "big") + bytes(params)


def decode(info: bytes) -> Parameters | None:
    """An XID information field, or None if it is not one. An empty field
    is a legal XID that negotiates nothing: every value None."""
    out = Parameters(None, None, None, None, None, None, None, None, None)
    if not info:
        return out
    if len(info) < 4 or info[0] != FI or info[1] != GI:
        return None
    length = int.from_bytes(info[2:4], "big")
    body = info[4:4 + length]
    if len(body) != length:
        return None
    at = 0
    while at + 2 <= len(body):
        pi, pl = body[at], body[at + 1]
        value_bytes = body[at + 2:at + 2 + pl]
        if len(value_bytes) != pl:
            return None
        value = int.from_bytes(value_bytes, "big") if pl else 0
        at += 2 + pl
        if pi == PI_CLASSES:
            if value & CLASS_HALF_DUPLEX:
                out.half_duplex = True
            elif value & CLASS_FULL_DUPLEX:
                out.half_duplex = False
        elif pi == PI_HDLC_OPTIONS:
            out.rej = bool(value & OPT_REJ)
            out.srej = bool(value & OPT_SREJ)
            out.modulo = 128 if value & OPT_MODULO_128 else 8
        elif pi == PI_N1_RX and pl:
            out.n1_rx = value // 8
        elif pi == PI_WINDOW_RX and pl:
            out.window_rx = value
        elif pi == PI_ACK_TIMER and pl:
            out.ack_timer_ms = value
        elif pi == PI_RETRIES and pl:
            out.retries = value
        elif pi == PI_OHS_MODES and pl == 3 and value >> 16 == OHS_VERSION:
            out.ohs_modes = ((value >> 8) & 0xFF, value & 0xFF)
    return out
