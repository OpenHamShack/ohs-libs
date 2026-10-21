"""CI-V, the command language inside the packets.

The IC-9700 on a LAN speaks exactly the same CI-V it speaks on its USB port;
the network side (icomlan.py) is only an envelope. So this module knows nothing
about sockets - it turns frequencies and modes into CI-V frames and back, which
is the half of the protocol that can be tested without a radio in the room.

A frame is FE FE <to> <from> <command...> FD. Replies come back with `to` and
`from` swapped, and the radio also emits frames nobody asked for when the
operator turns the dial - "transceive" - which is why the reader below accepts
any frame rather than only the one matching an outstanding request.
"""

from __future__ import annotations

PREAMBLE = 0xFE
END = 0xFD

#: The IC-9700's factory address, kept as the fallback for callers that do not
#: name a radio. Every model has its own and every one of them is changeable in
#: the radio's menu, which is why the address is a setting rather than a
#: constant - see models.py for the table.
DEFAULT_RADIO_ADDR = 0xA2
#: 0xE0 is the conventional "a computer" address. The radio does not care what
#: it is as long as replies can be addressed back to it.
DEFAULT_CTRL_ADDR = 0xE0

OK = 0xFB
NG = 0xFA

# The mode byte, as Icom numbers them. This is the wire format and is the same
# on every radio; which of these a particular radio actually has is a property
# of the model, not of the protocol, and is checked one layer up.
from .models import ALL_MODES as MODES  # noqa: E402

MODE_CODES = {name: code for code, name in MODES.items()}

#: FIL1/FIL2/FIL3. The radio reports a filter alongside every mode and expects
#: one back when the mode is set, so it is carried through rather than dropped.
FILTERS = {0x01: "FIL1", 0x02: "FIL2", 0x03: "FIL3"}


class CivError(Exception):
    """A frame that is not a frame, or not one we can act on."""


def frame(command: bytes, to: int = DEFAULT_RADIO_ADDR, frm: int = DEFAULT_CTRL_ADDR) -> bytes:
    """Wrap a command in its addressing and terminator."""
    return bytes([PREAMBLE, PREAMBLE, to, frm]) + command + bytes([END])


def split_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Pull whole frames out of a buffer, returning them and what is left over.

    The LAN transport delivers a datagram at a time and a datagram can hold
    more than one frame, or half of one. Anything before the first preamble is
    discarded: it is either a collision or a fragment of a frame whose start we
    never saw, and guessing at it is worse than dropping it.
    """
    frames: list[bytes] = []
    while True:
        start = buf.find(bytes([PREAMBLE, PREAMBLE]))
        if start < 0:
            # Keep a trailing single 0xFE - it may be the first half of the
            # next frame's preamble, arriving in the next datagram.
            return frames, buf[-1:] if buf[-1:] == bytes([PREAMBLE]) else b""
        # A run of more than two FE is a wake preamble (see set_power) - our
        # own power-on echoed back on a serial bus, usually. The frame starts
        # at the last two of them, as the radio reads it.
        while start + 2 < len(buf) and buf[start + 2] == PREAMBLE:
            start += 1
        end = buf.find(bytes([END]), start)
        if end < 0:
            return frames, buf[start:]
        frames.append(buf[start : end + 1])
        buf = buf[end + 1 :]


def parse(f: bytes) -> tuple[int, int, bytes]:
    """Split a frame into (to, from, payload). Raises CivError on a bad one."""
    if len(f) < 5 or f[0] != PREAMBLE or f[1] != PREAMBLE or f[-1] != END:
        raise CivError(f"not a CI-V frame: {f.hex(' ')}")
    return f[2], f[3], f[4:-1]


def encode_freq(hz: int) -> bytes:
    """Frequency as the five little-endian BCD bytes the radio wants.

    Two digits to a byte, least significant pair first, so 144.800 MHz becomes
    00 80 44 01 00. Ten digits is enough for the 1.2 GHz band and then some.
    """
    if hz < 0 or hz > 9_999_999_999:
        raise CivError(f"frequency out of range: {hz}")
    digits = f"{hz:010d}"
    # digits[8:10] is the 1Hz/10Hz pair and goes in the first byte.
    return bytes(
        int(digits[8 - 2 * i : 10 - 2 * i], 16) for i in range(5)
    )


def decode_freq(b: bytes) -> int:
    """The inverse. Rejects a byte that is not BCD rather than returning a lie."""
    if len(b) != 5:
        raise CivError(f"expected 5 frequency bytes, got {len(b)}")
    digits = ""
    for byte in reversed(b):
        hi, lo = byte >> 4, byte & 0x0F
        if hi > 9 or lo > 9:
            raise CivError(f"not BCD: {b.hex(' ')}")
        digits += f"{hi}{lo}"
    return int(digits)


# ---------------------------------------------------------------------------
# The commands this module uses. Kept as builders rather than scattered byte
# literals so the one place to check a command number against the manual is
# here.

def read_freq() -> bytes:
    return b"\x03"


def set_freq(hz: int) -> bytes:
    return b"\x05" + encode_freq(hz)


def read_mode() -> bytes:
    return b"\x04"


def set_mode(mode: str, filt: int = 0x01) -> bytes:
    """Mode and filter, with no way to say anything about the data flag.

    Kept because it is the plain command every Icom has answered since long
    before a data flag existed, and because the reader still has to understand
    the 0x04 answer that pairs with it - the radio volunteers one of those when
    the operator turns the mode knob. Nothing in this module *sends* it any
    more; set_mode_data below is what is sent. See the note there.
    """
    try:
        code = MODE_CODES[mode]
    except KeyError:
        raise CivError(f"no such mode: {mode!r}") from None
    return bytes([0x06, code, filt])


# -- mode, the data flag and the filter, in one frame: command 0x26 --------
#
# DATA is not a mode on an Icom. There is no "USB-D" byte to put in ALL_MODES:
# the radio is in USB, 0x01, *and* has a separate data flag set, and that flag
# is the thing that routes transmit audio from the LAN/USB input instead of the
# microphone and takes the speech processing out of the path. Adding a code to
# the mode table would be inventing wire format the radio has never sent, and
# the first symptom would be a mode byte the radio ignores in silence.
#
# Two commands can carry the flag:
#
#     1A 06 <data> <filter>            "data mode with filter", the older one
#     26 <vfo> <mode> <data> <filter>  mode, data and filter for one VFO
#
# This module sends 0x26, and only 0x26, for four reasons in the order of what
# getting it wrong costs:
#
#   * It is one frame. 0x1A 0x06 cannot set the mode, so "put this VFO into
#     USB-D" becomes 06 01 01 and then 1A 06 01 01 - and between those two
#     frames the radio is sitting in plain USB with the microphone live. The
#     digital side asks for its mode and then arms and keys within a few
#     hundred milliseconds, so that window is not theoretical: it is a
#     transmission from the wrong audio source, which is the whole fault this
#     exists to fix.
#   * It is one read. The poll loop already asks for the mode four times a
#     second, and 0x26 answers mode, data and filter to that single question.
#     With 0x1A 0x06 the flag needs a poll command of its own, for ever, on a
#     link that is also carrying a spectrum ten times a second.
#   * It names the VFO. Under split the flag belongs on the transmit VFO, and
#     0x1A 0x06 has nowhere to say which VFO it means.
#   * Its shape is the same on all three radios in models.py. The 0x1A
#     registers are per-model: the same register number means different things
#     on different rigs, which is exactly what the table in models.py exists to
#     keep out of the protocol.
#
# 0x1A 0x06 is deliberately not implemented. It would be a second way to set
# one flag, and the failure that produces - one path setting it and another
# reading somewhere else - is worse than not having it at all.

#: Which VFO a 0x26 frame is about. 0x00 is whichever VFO is selected, and that
#: is what every other command here acts on (0x03, 0x05 and 0x06 are all "the
#: selected VFO"), so it is the default and the only one this module sends.
#: Main and Sub are named because reading a frame means knowing what the byte
#: says, not because anything has tried them against a radio.
VFO_SELECTED = 0x00
VFO_MAIN = 0x01
VFO_SUB = 0x02

#: DATA_SUFFIX, mode_label and split_mode_label are maker-neutral and live in
#: shack_rig.model; they are re-exported here because this is where the Icom
#: app's code has always looked for them.
from ..model import DATA_SUFFIX, mode_label, split_mode_label  # noqa: E402,F401


def read_mode_data(vfo: int = VFO_SELECTED) -> bytes:
    """Ask for mode, data flag and filter together."""
    return bytes([0x26, vfo])


def set_mode_data(mode: str, data: bool = False, filt: int = 0x01,
                  vfo: int = VFO_SELECTED) -> bytes:
    """Mode, data flag and filter, in the one frame the radio applies at once."""
    try:
        code = MODE_CODES[mode]
    except KeyError:
        raise CivError(f"no such mode: {mode!r}") from None
    if filt not in FILTERS:
        # A filter number outside 1-3 is taken by the radio and ignored, and
        # the mode goes through anyway - so the mode changes, the filter does
        # not, and nothing says why. Caught here instead.
        raise CivError(f"no such filter: {filt!r}")
    return bytes([0x26, vfo, code, 0x01 if data else 0x00, filt])


def parse_mode_data(p: bytes) -> dict:
    """A 0x26 payload as vfo, mode, data, filter.

    The answer to a read and the echo of a set have the same body; the radio
    does not distinguish them and neither does this.
    """
    if len(p) < 5 or p[0] != 0x26:
        raise CivError(f"not a mode/data frame: {p.hex(' ')}")
    return {
        "vfo": p[1],
        "mode": MODES.get(p[2]),
        # Non-zero rather than == 1. An IC-7610 numbers its data sources
        # D1/D2/D3 in this byte and an IC-9700 only ever sends 1, so a test for
        # 1 exactly would read D2 as "data off" and draw USB on a page showing
        # a radio that is plainly in USB-D.
        "data": p[3] != 0,
        "data_value": p[3],
        "filter": p[4],
    }


# -- power, command 0x18 ----------------------------------------------------
#
# 18 01 switches the radio on and 18 00 switches it off - into standby, not
# off at the wall: a radio that is to be woken again must have its "Power OFF
# Setting (for Remote Control)" at Standby/Shutdown, which is also what keeps
# its LAN side answering while the rest of it sleeps.
#
# A sleeping radio's CI-V receiver is not listening at the bit level, so on a
# serial line the frame is sent behind a run of extra FE bytes to give the
# processor time to wake and catch the start of a real frame. The count is the
# number of bytes in roughly 100 ms at the line speed, and the table below is
# wfview's (icomCommander::powerOn), which is the prior art that works.
#
# Over the network there is no preamble at all. The radio's LAN controller is
# awake while the radio sleeps and hands the frame over whole, and wfview
# deliberately sends none when it is on the native LAN link
# (`if (!usingNativeLAN || !rigCaps.hasLan)`). Copying the serial preamble onto
# the LAN would be 150 bytes of padding inside a UDP packet for a radio that
# does not need them - harmless, probably, but not what anybody has tested.

def set_power(on: bool) -> bytes:
    return bytes([0x18, 0x01 if on else 0x00])


#: Extra FE bytes before a power-on, by serial line speed. Anything not listed
#: (115200 and above, or not known) gets the longest run, which costs a few
#: milliseconds and cannot be too short.
_WAKE_PREAMBLE = {4800: 7, 9600: 13, 19200: 25, 38400: 50, 57600: 75}
WAKE_PREAMBLE_MAX = 150


def wake_preamble(baud: int | None) -> bytes:
    """The FE run that goes in front of a power-on on a serial line."""
    return bytes([PREAMBLE]) * _WAKE_PREAMBLE.get(baud or 0, WAKE_PREAMBLE_MAX)


def read_ptt() -> bytes:
    return b"\x1c\x00"


def set_ptt(on: bool) -> bytes:
    return bytes([0x1C, 0x00, 0x01 if on else 0x00])


def set_vfo(which: str) -> bytes:
    """VFOA or VFOB. The IC-9700 has no more than two per band."""
    if which not in ("VFOA", "VFOB"):
        raise CivError(f"no such VFO: {which}")
    return bytes([0x07, 0x00 if which == "VFOA" else 0x01])


def read_split() -> bytes:
    return b"\x0f"


def set_split(on: bool) -> bytes:
    return bytes([0x0F, 0x01 if on else 0x00])


# -- for a packet station: squelch, simplex, tones, and where data audio
# comes from --------------------------------------------------------------
#
# Added for the packet radio links (shack_packet.radios, 18/10/2026). None of
# these is in the IC-9700 CI-V Reference Guide extract this repository
# quotes, so each says where its bytes come from. "hamlib" means hamlib
# 4.6's rigs/icom/icom.c and icom_defs.h, the prior art every Icom program
# leans on; a radio that disagrees answers NG (FA), which the caller sees as
# a write that did not happen rather than as a wrong setting.
#
#     15 01             read squelch status: 00 closed, 01 open (hamlib
#                       icom_get_dcd: C_RD_SQSG 0x15, S_SQL 0x01; it reads
#                       the answer as BCD, one or two bytes)
#     0F 10             duplex off - simplex (hamlib S_DUP_OFF under
#                       C_CTL_SPLT; 0F 11 is DUP-, 0F 12 DUP+)
#     16 42 00          repeater tone (encode) off  (hamlib S_FUNC_TONE)
#     16 43 00          tone squelch off            (hamlib S_FUNC_TSQL)
#     16 4B 00          DTCS off                    (hamlib S_FUNC_CSQL)
#
# The squelch read is what makes the radio's own carrier sense usable: a
# packet station's channel access holds off while it is open (an FM voice
# user our demodulator would never call data). The other four make the
# radio simplex and toneless before a packet station transmits, because a
# 9700 left on a repeater channel would put the frames 600 kHz away with a
# CTCSS tone under them.

def read_squelch() -> bytes:
    return b"\x15\x01"


def decode_squelch(p: bytes) -> bool:
    """Open (True) or closed from a 15 01 answer. Any non-zero BCD is open,
    as hamlib reads it: the guide's 0001 and a one-byte 01 both say open."""
    if len(p) < 3 or p[0] != 0x15 or p[1] != 0x01:
        raise CivError(f"not a squelch answer: {p.hex(' ')}")
    return any(b for b in p[2:])


def set_simplex() -> bytes:
    return b"\x0f\x10"


#: What 0F answers when asked alone: one byte, which is the split state
#: while duplex is off and the duplex direction while it is on (IC-9700
#: CI-V guide p. 4, 0F "Read Split OFF/ON setting, Read DUP-/DUP+
#: operation, Read DD Repeater Simplex mode"). A 9700 on a simplex channel
#: answered `0f 00` on 18/10/2026; the DUP answers are the guide's and have
#: not yet been seen from a real radio. While split is on the answer is 01
#: whatever the duplex is, so the duplex cannot be read then.
DUPLEX_READS = {0x00: "simplex", 0x01: "split", 0x11: "DUP-", 0x12: "DUP+", 0x13: "RPS"}

#: The duplex answers set_duplex can put back, by their own code (0F 11/12).
DUPLEX_SHIFTS = (0x11, 0x12)


def decode_duplex(p: bytes) -> int:
    """The one byte a bare 0F answer carries (a DUPLEX_READS key)."""
    if len(p) < 2 or p[0] != 0x0F:
        raise CivError(f"not a split/duplex answer: {p.hex(' ')}")
    return p[1]


def set_duplex(code: int) -> bytes:
    """Duplex as 0F reads it back: 00 (simplex) is set with 0F 10, a shift
    with its own code. Split (01) and RPS (13) are not set this way: one is
    a different switch and the other belongs to DD mode."""
    if code == 0x00:
        return set_simplex()
    if code in DUPLEX_SHIFTS:
        return bytes([0x0F, code])
    raise CivError(f"not a duplex this library sets: 0x{code:02x}")


#: 16 <sub> 00 switches each of these off; a packet station sends all three.
TONE_FUNCTIONS = {"tone": 0x42, "tsql": 0x43, "dtcs": 0x4B}


def set_tone_function(name: str, on: bool = False) -> bytes:
    try:
        sub = TONE_FUNCTIONS[name]
    except KeyError:
        raise CivError(f"no such tone function: {name!r}") from None
    return bytes([0x16, sub, 0x01 if on else 0x00])


# -- satellites, the other VFO, and DD (Packet Radio 0.3.0) ------------------
#
# From the IC-9700 CI-V Reference Guide, as read for Packet Radio's
# satellite and 23 cm DD work (shack-packet HANDOVER.md, 20/10/2026):
#
#     25 00 / 25 01     read or set the selected / unselected VFO's
#                       frequency (five BCD bytes after the sub-command)
#     16 5A 00 / 01     satellite mode off / on (in it MAIN is the uplink and
#                       SUB the downlink: Basic Manual "Satellite mode")
#     06 22             operating mode DD (128 kbit/s, 23 cm only); no
#                       filter byte, DD has none
#     0F 13             RPS - DD's repeater simplex; 0F 10 is simplex
#     1A 05 0087        TX INHIBIT at power-on (read only here: the operator
#                       sets the live one with the TRANSMIT key)
#     1A 05 0088        DD Packet Output, 00 Normal / 01 All (read only)
#
# None of these has been sent to a real radio by this library yet; the
# satellite and DD hardware tests are supervised (Brian present). A radio
# that disagrees answers NG, which `IcomRig.ask(..., refused=True)` turns
# into a write that visibly did not happen.

VFO_FREQ = 0x25
SATELLITE_MODE = bytes([0x16, 0x5A])
TX_INHIBIT_DEFAULT_ITEM = 87
DD_PACKET_OUTPUT_ITEM = 88
DD_PACKET_OUTPUT = {0x00: "normal", 0x01: "all"}


def read_vfo_freq(unselected: bool = False) -> bytes:
    return bytes([VFO_FREQ, 0x01 if unselected else 0x00])


def set_vfo_freq(hz: int, unselected: bool = False) -> bytes:
    """The selected (False) or the other (True) VFO's frequency, without
    touching which is selected: Doppler correction moves the transmit VFO
    while the receive one stays selected."""
    return read_vfo_freq(unselected) + encode_freq(hz)


def decode_vfo_freq(p: bytes) -> tuple[bool, int]:
    """(unselected, Hz) from a 25 answer."""
    if len(p) < 7 or p[0] != VFO_FREQ or p[1] not in (0x00, 0x01):
        raise CivError(f"not a VFO frequency answer: {p.hex(' ')}")
    return p[1] == 0x01, decode_freq(p[2:7])


def read_satellite_mode() -> bytes:
    return SATELLITE_MODE


def set_satellite_mode(on: bool) -> bytes:
    return SATELLITE_MODE + bytes([0x01 if on else 0x00])


def decode_satellite_mode(p: bytes) -> bool:
    if len(p) < 3 or p[:2] != SATELLITE_MODE or p[2] not in (0x00, 0x01):
        raise CivError(f"not a satellite mode answer: {p.hex(' ')}")
    return p[2] == 0x01


def set_dd_mode() -> bytes:
    return bytes([0x06, MODE_CODES["DD"]])


def set_rps(on: bool) -> bytes:
    """DD's repeater simplex on (0F 13), or plain simplex (0F 10). Kept
    apart from `set_duplex`, which refuses 13: RPS only means anything in
    DD, and only the DD transport switches it."""
    return bytes([0x0F, 0x13]) if on else set_simplex()


def set_data_mod(item: int, value: int) -> bytes:
    """The one menu write this module builds: DATA MOD, at the item and
    with a value a model's `data_mod` table names (models.DataMod).

    Narrow on purpose, as read_menu's docstring explains: there is no
    general menu writer here, because one typo in a menu number on a 9700
    is DC up the coax to a mast-head preamp. The caller (IcomRig.set_data_mod)
    only ever hands over the item and a value from that table.
    """
    if not 0 <= item <= 9999 or not 0 <= value <= 0x0F:
        raise CivError(f"not a DATA MOD write: item {item!r}, value {value!r}")
    return read_menu(item) + bytes([value])


# -- a radio set up for data: one narrow writer per setting ----------------
#
# Added for shack_packet's data set-up (Packet Radio 0.2, 18/10/2026: Brian,
# "build in to the app setting whatever controls are needed for optimum
# performance"). Every number below is from Icom's IC-9700 CI-V Reference
# Guide (~/build/civ9700 on shack), command table p. 5 for 16 xx and p. 7
# for the 1A 05 items; the IC-705 and the HF radios use the same 16 xx
# numbers in hamlib 4.6's icom_defs.h (S_FUNC_NB 0x22, S_FUNC_NR 0x40 ...).
#
#     16 22 00/01   noise blanker          16 44 00/01   speech compressor
#     16 40 00/01   noise reduction        16 45 00/01   monitor [MONI]
#     16 41 00/01   auto notch             16 46 00/01   VOX
#     16 4A 00/01   AFC                    16 42/43/4B   tones (above)
#     14 03 0000-0255  squelch level, the selected band's (read only here)
#
# 16 40 is the one that mattered first: with NR on, 2 m receive of 1200 baud
# AFSK from a Pluto decoded nothing, and 3/3 with it off (shack-packet
# HANDOVER, 18/10 ~11:43): the adaptive filter reshapes the tones.
#
# Each 16 xx is an on/off switch, so a radio without one answers NG and
# nothing changes; they are readers and writers here by name, not by number,
# so a caller cannot reach a 16 xx this file has not named. The 1A 05 items
# are a different risk (read_menu says why) and each writer below accepts
# only the item numbers named for it.

#: The 16 xx switches by name: the tones above, and the rest of the set-up.
FUNCTIONS = {"nb": 0x22, "nr": 0x40, "an": 0x41, "comp": 0x44, "monitor": 0x45,
             "vox": 0x46, "afc": 0x4A, **TONE_FUNCTIONS}


def read_function(name: str) -> bytes:
    try:
        return bytes([0x16, FUNCTIONS[name]])
    except KeyError:
        raise CivError(f"no such function: {name!r}") from None


def _function(name: str, on: bool) -> bytes:
    return read_function(name) + bytes([0x01 if on else 0x00])


def set_noise_blanker(on: bool) -> bytes:
    return _function("nb", on)


def set_noise_reduction(on: bool) -> bytes:
    return _function("nr", on)


def set_auto_notch(on: bool) -> bytes:
    return _function("an", on)


def set_compressor(on: bool) -> bytes:
    return _function("comp", on)


def set_monitor(on: bool) -> bytes:
    return _function("monitor", on)


def set_vox(on: bool) -> bytes:
    return _function("vox", on)


def set_afc(on: bool) -> bytes:
    return _function("afc", on)


#: The writer for each switch, for a caller holding the name.
FUNCTION_WRITERS = {"nb": set_noise_blanker, "nr": set_noise_reduction, "an": set_auto_notch,
                    "comp": set_compressor, "monitor": set_monitor, "vox": set_vox, "afc": set_afc,
                    "tone": lambda on: set_tone_function("tone", on),
                    "tsql": lambda on: set_tone_function("tsql", on),
                    "dtcs": lambda on: set_tone_function("dtcs", on)}


def decode_function(p: bytes) -> tuple[str, bool]:
    """(name, on) from a 16 xx answer for one of FUNCTIONS."""
    names = {v: k for k, v in FUNCTIONS.items()}
    if len(p) < 3 or p[0] != 0x16 or p[1] not in names:
        raise CivError(f"not a function answer: {p.hex(' ')}")
    return names[p[1]], p[2] != 0


SQUELCH_LEVEL = bytes([0x14, 0x03])


def read_squelch_level() -> bytes:
    """14 03: the squelch level of the selected band, 0000-0255. The 9700
    keeps one per band (MAIN and SUB): opening the other band's knob leaves
    this one where it was (18/10/2026)."""
    return SQUELCH_LEVEL


def decode_squelch_level(p: bytes) -> int:
    if len(p) < 4 or p[0] != 0x14 or p[1] != 0x03:
        raise CivError(f"not a squelch level: {p.hex(' ')}")
    return decode_meter(p[2:4])


#: 1A 05 items each writer may touch, from the IC-9700 guide p. 7. Another
#: radio's sourced items are added here, next to their source, never passed
#: in from outside: that is what keeps the External P.AMP items (0093-0095,
#: DC up the coax) out of reach of every writer in this file.
DATA_OFF_MOD_ITEMS = frozenset({115})       # SET > Connectors > MOD Input > DATA OFF MOD
MOD_LEVEL_ITEMS = frozenset({112, 113, 114})  # ... > ACC / USB / LAN MOD Level, 0000-0255
AF_SQL_ITEMS = frozenset({107, 111})        # ... > USB / LAN AF/IF Output > AF SQL


def set_data_off_mod(item: int, value: int) -> bytes:
    """DATA OFF MOD: where transmit audio comes from with the data flag off
    (00 MIC ... 05 LAN, as DATA MOD). A packet station sets it to MIC, so a
    radio that loses the data flag is not modulated by the computer."""
    if item not in DATA_OFF_MOD_ITEMS or not 0 <= value <= 0x05:
        raise CivError(f"not a DATA OFF MOD write: item {item!r}, value {value!r}")
    return read_menu(item) + bytes([value])


def set_mod_level(item: int, level: int) -> bytes:
    """ACC/USB/LAN MOD Level, 0000 (0%) to 0255 (100%), two BCD bytes."""
    if item not in MOD_LEVEL_ITEMS or not 0 <= level <= 255:
        raise CivError(f"not a MOD level write: item {item!r}, level {level!r}")
    d = f"{level:04d}"
    return read_menu(item) + bytes([int(d[0:2], 16), int(d[2:4], 16)])


def set_af_sql(item: int, on: bool) -> bytes:
    """USB/LAN AF SQL: 00 OFF (audio out whatever the squelch), 01 ON
    (audio only while it is open). A packet station wants it off: the
    demodulator does its own carrier detect, and a squelch that opens late
    cuts the start of a frame."""
    if item not in AF_SQL_ITEMS:
        raise CivError(f"not an AF SQL item: {item!r}")
    return read_menu(item) + bytes([0x01 if on else 0x00])


def decode_menu_level(p: bytes) -> tuple[int, int]:
    """(item, level) from a 1A 05 answer whose value is 0000-0255."""
    item, _first = decode_menu(p)
    if len(p) < 6:
        raise CivError(f"menu level answer too short: {p.hex(' ')}")
    return item, decode_meter(bytes(p[4:6]))


# -- the receive front end: preamp 16 02, attenuator 11 --------------------
#
# Both are send/read on every Icom in models.py (IC-9700 CI-V Reference Guide
# pp. 4-5; the HF radios by wfview's rig files). What the value *means* is per
# model - the 9700 packs its external preamp into the preamp byte, the 7610's
# attenuator has fifteen steps - so this file only builds and reads the bytes,
# and models.py says which values a radio has. Neither command is sent
# unsolicited when the operator presses P.AMP/ATT on the panel (transceive is
# frequency and mode only), so the poll loop reads them.

def read_preamp() -> bytes:
    return b"\x16\x02"


def set_preamp(value: int) -> bytes:
    """The raw 16 02 byte; models.IcomModel.preamp_byte makes it from a level."""
    if not 0 <= value <= 0x03:
        raise CivError(f"no such preamp setting: {value!r}")
    return bytes([0x16, 0x02, value])


# -- the CW pitch: 14 09 ------------------------------------------------------
#
# Send/read on every Icom in models.py: "0000 ~ 0255 Send/read CW pitch (5 Hz
# steps) (0000=300 Hz, 0128=600 Hz, 0255=900 Hz)" (IC-9700 CI-V Reference
# Guide, command table 14). It is the audio tone a CW signal is heard at, in
# CW and CW-R alike, so it is where a CW decoder listens. Read only: this
# library has no reason to change an operator's sidetone.

def read_cw_pitch() -> bytes:
    return b"\x14\x09"


def cw_pitch_hz(level: int) -> int:
    """The 0-255 level as Hz: 300 to 900 in Icom's 5 Hz steps. The guide's
    three points (0 = 300, 128 = 600, 255 = 900) are a straight line, and
    rounding to the step lands 128 on 600 rather than 601."""
    if not 0 <= level <= 255:
        raise CivError(f"CW pitch level out of range: {level!r}")
    return int(5 * round((300 + level * 600 / 255) / 5))


# -- RF POWER: 14 0A ---------------------------------------------------------
#
# Send/read on every Icom in models.py: "0000 ~ 0255 Send/read RF POWER"
# (IC-9700 CI-V Reference Guide, command table 14; the same two-BCD-byte
# shape as the CW pitch above and every meter below). Added for
# shack_packet's real-radio dummy-load test (18/10/2026): before that test's
# first key, the link reads this, sets it to 0000 and reads it back, and
# refuses to key unless the radio confirms 0 - see
# shack_packet.radios.icom._ensure_minimum_power.
#
# 0000 is not nought watts. On Brian's 9700 (see docs/ic9700-research.md and
# GUIDE.md's ALC/Po note) the Po meter never truly reaches zero; Icom's own
# manual puts the floor at roughly 0.5 W. A dummy load is required at every
# level this command can set, including 0000.

RF_POWER = bytes([0x14, 0x0A])


def read_rf_power() -> bytes:
    return RF_POWER


def set_rf_power(level: int) -> bytes:
    """RF POWER, 0000 (minimum, not zero watts - see the note above) to
    0255 (maximum), as the two BCD bytes every 0x14/0x15 meter uses."""
    if not 0 <= level <= 255:
        raise CivError(f"RF power level out of range: {level!r}")
    d = f"{level:04d}"
    return RF_POWER + bytes([int(d[0:2], 16), int(d[2:4], 16)])


def decode_rf_power(p: bytes) -> int:
    """The level (0-255) from a 14 0A answer."""
    if len(p) < 4 or p[0] != 0x14 or p[1] != 0x0A:
        raise CivError(f"not an RF power frame: {p.hex(' ')}")
    return decode_meter(p[2:4])


def read_menu(item: int) -> bytes:
    """Read one SET-menu item: 1A 05 <item as four BCD digits>.

    Read only, on purpose - there is no `set_menu`. The one family of items
    this module reads is the IC-9700's External P.AMP enable (0093-0095),
    which on the real radio decides whether DC is sent up the coax to a
    mast-head preamp; see models.IC9700_EXT_PREAMP_MENU. A writer here would
    be one typo away from powering somebody's antenna.
    """
    if not 0 <= item <= 9999:
        raise CivError(f"no such menu item: {item!r}")
    d = f"{item:04d}"
    return bytes([0x1A, 0x05, int(d[:2], 16), int(d[2:], 16)])


def decode_menu(p: bytes) -> tuple[int, int]:
    """(item, value) from a 1A 05 answer; the value's first byte only, which
    is all an on/off item has."""
    if len(p) < 5 or p[0] != 0x1A or p[1] != 0x05:
        raise CivError(f"not a menu answer: {p.hex(' ')}")
    item = f"{p[2]:02x}{p[3]:02x}"
    if not item.isdigit():
        raise CivError(f"menu item is not BCD: {p.hex(' ')}")
    return int(item), p[4]


def read_att() -> bytes:
    return b"\x11"


def set_att(db: int) -> bytes:
    """Attenuator in dB, sent as BCD: 10 dB is 0x10, not 0x0A.

    The 9700's guide lists 00 and 10 and nothing else, and the HF radios'
    steps (3, 6 ... 45) are all two decimal digits, so one BCD byte is the
    whole format. A binary 10 (0x0A) is not BCD and the radio answers NG.
    """
    if not 0 <= db <= 99:
        raise CivError(f"attenuator out of range: {db!r}")
    return bytes([0x11, int(f"{db:02d}", 16)])


def decode_att(p: bytes) -> int:
    """dB from an 11 payload (11 <bcd>)."""
    if len(p) < 2 or p[0] != 0x11:
        raise CivError(f"not an attenuator frame: {p.hex(' ')}")
    return bcd_byte(p[1])


# Meters. All of them answer 0000-0255 as two BCD bytes; what that number means
# differs per meter, which is what the scaling functions below are for.
def read_smeter() -> bytes:
    return b"\x15\x02"


def read_power() -> bytes:
    return b"\x15\x11"


def read_swr() -> bytes:
    return b"\x15\x12"


def decode_meter(b: bytes) -> int:
    """The raw 0-255 a meter reports."""
    if len(b) != 2:
        raise CivError(f"expected 2 meter bytes, got {len(b)}")
    hi, lo = b[0], b[1]
    for nib in (hi >> 4, hi & 0xF, lo >> 4, lo & 0xF):
        if nib > 9:
            raise CivError(f"meter reading is not BCD: {b.hex(' ')}")
    return (hi >> 4) * 1000 + (hi & 0xF) * 100 + (lo >> 4) * 10 + (lo & 0xF)


def smeter_db(raw: int) -> float:
    """Raw S-meter to dB relative to S9, by Icom's two published points.

    0 is S0, 120 is S9 and 241 is S9+60. The scale is not one straight line -
    it bends at S9 - so interpolating across the whole range in one go reads
    about half an S-point high in the middle of the scale, which is the sort of
    error that makes a signal report wrong rather than approximate.
    """
    raw = max(0, min(255, raw))
    if raw <= 120:
        # S0..S9 spans 54 dB in 6 dB steps.
        return (raw / 120.0) * 54.0 - 54.0
    return ((raw - 120) / 121.0) * 60.0


def swr(raw: int) -> float:
    """Raw SWR meter to a ratio, by the three points Icom publishes.

    0 is 1.0:1, 48 is 1.5:1, 80 is 2.0:1, 120 is 3.0:1. Beyond that the meter
    is only saying "high", so it is reported as such rather than extrapolated
    into a precise-looking number nobody should trust.
    """
    raw = max(0, min(255, raw))
    points = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (255, 99.0)]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if raw <= x1:
            span = x1 - x0
            return y0 + (raw - x0) * (y1 - y0) / span if span else y0
    return 99.0


# -- the spectrum scope, command 0x27 -------------------------------------
#
# Taken from wfview's own IC-9700 rig definition rather than from a manual:
#
#     Scope Wave Data      0x27 0x00     the data itself, streamed
#     Scope On/Off         0x27 0x10     0 or 1
#     Scope Data Output    0x27 0x11     0 or 1
#     Scope Main/Sub       0x27 0x12
#     Scope Single/Dual    0x27 0x13
#     Scope Mode           0x27 0x14     centre or fixed
#     Scope Span           0x27 0x15
#
# Two commands are needed to make a waterfall appear: switch the scope on, and
# then switch its *data output* on. The second is the one that is easy to miss,
# because the radio's own screen draws a scope perfectly well without it - the
# setting controls whether the waveform is also sent over CI-V.
#
# The wave data then arrives unasked, as a series of 0x27 0x00 frames carrying
# the spectrum in pieces, and wfview reassembles them in parseSpectrum().
SCOPE_WAVE_DATA = bytes([0x27, 0x00])
SCOPE_ON_OFF = bytes([0x27, 0x10])
SCOPE_DATA_OUTPUT = bytes([0x27, 0x11])
SCOPE_MAIN_SUB = bytes([0x27, 0x12])
SCOPE_MODE = bytes([0x27, 0x14])


def scope_on(on: bool = True) -> bytes:
    return SCOPE_ON_OFF + bytes([0x01 if on else 0x00])


def scope_data_output(on: bool = True) -> bytes:
    """Whether the waveform is sent over CI-V, as opposed to merely drawn."""
    return SCOPE_DATA_OUTPUT + bytes([0x01 if on else 0x00])


# -- the scope's settings: zoom, reference level, speed --------------------
#
# From Icom's IC-9700 CI-V Reference Guide (command table p. 12, formats
# p. 24-25), not from wfview as the commands above were. Each takes the
# receiver first (00 main, 01 sub) and is read by sending the command and the
# receiver alone; the answer repeats both and adds the value:
#
#     27 14 <rx> <00 centre | 01 fixed>
#     27 15 <rx> <5 bytes>      the half-span in Hz, coded as a frequency:
#                               +-2.5 kHz is 00 25 00 00 00
#     27 16 <rx> <01..03>       which fixed-mode edge set
#     27 19 <rx> <dd> <d0> <s>  reference level: two BCD bytes, 10 and 1 dB,
#                               then 0.1 and (always 0) 0.01 dB; sign 00 +, 01 -
#     27 1A <rx> <00 fast | 01 mid | 02 slow>
#     27 1D <rx> <00 narrow | 01 wide>   video bandwidth
#
# Averaging is not a 27 command but the SCOPE > Averaging menu entry,
# 1A 05 0192 (00 off, 01 2, 02 3, 03 4 sweeps).
SCOPE_SPAN = bytes([0x27, 0x15])
SCOPE_EDGE = bytes([0x27, 0x16])
SCOPE_REF = bytes([0x27, 0x19])
SCOPE_SPEED = bytes([0x27, 0x1A])
SCOPE_VBW = bytes([0x27, 0x1D])

#: The settings' wire values, both ways.
SCOPE_SPEEDS = {0x00: "fast", 0x01: "mid", 0x02: "slow"}
SCOPE_VBWS = {0x00: "narrow", 0x01: "wide"}
SCOPE_SET_MODES = {0x00: "center", 0x01: "fixed"}
#: 1A 05 0192's values, as sweeps averaged (0 = off).
SCOPE_AVERAGING = {0x00: 0, 0x01: 2, 0x02: 3, 0x03: 4}
SCOPE_AVERAGING_ITEM = 192
#: The commands whose answers carry a scope setting, for the reader.
SCOPE_SETTING_COMMANDS = (0x14, 0x15, 0x16, 0x19, 0x1A, 0x1D)


def read_scope_setting(sub: int, receiver: int = 0) -> bytes:
    """Ask for one of SCOPE_SETTING_COMMANDS for a receiver."""
    if sub not in SCOPE_SETTING_COMMANDS:
        raise CivError(f"not a scope setting: 27 {sub:02x}")
    return bytes([0x27, sub, receiver & 0x01])


def set_scope_mode(mode: str, receiver: int = 0) -> bytes:
    codes = {v: k for k, v in SCOPE_SET_MODES.items()}
    if mode not in codes:
        raise CivError(f"no such scope mode: {mode!r}")
    return SCOPE_MODE + bytes([receiver & 0x01, codes[mode]])


def set_scope_span(half_span_hz: int, receiver: int = 0) -> bytes:
    """Centre-mode span, as the radio counts it: the half-span (+-) in Hz."""
    return SCOPE_SPAN + bytes([receiver & 0x01]) + encode_freq(int(half_span_hz))


def set_scope_edge(edge: int, receiver: int = 0) -> bytes:
    if not 1 <= int(edge) <= 9:
        raise CivError(f"no such scope edge: {edge!r}")
    return SCOPE_EDGE + bytes([receiver & 0x01, int(edge)])


def encode_ref_db(db: float) -> bytes:
    """A reference level, -20.0 to +20.0 dB in 0.5 dB steps, as 27 19's three
    bytes: BCD tens and units, BCD tenths and a fixed 0, then the sign."""
    tenths = int(round(abs(float(db)) * 10))
    if tenths > 200:
        raise CivError(f"reference level out of range: {db!r}")
    whole, tenth = divmod(tenths, 10)
    return bytes([int(f"{whole:02d}", 16), tenth << 4, 0x01 if db < 0 and tenths else 0x00])


def decode_ref_db(b: bytes) -> float:
    if len(b) < 3:
        raise CivError(f"reference level is {len(b)} bytes, not 3")
    whole = bcd_byte(b[0])
    tenth = b[1] >> 4
    if tenth > 9:
        raise CivError(f"not BCD: {b.hex(' ')}")
    value = whole + tenth / 10
    return -value if b[2] == 0x01 else value


def set_scope_ref(db: float, receiver: int = 0) -> bytes:
    return SCOPE_REF + bytes([receiver & 0x01]) + encode_ref_db(db)


def set_scope_speed(speed: str, receiver: int = 0) -> bytes:
    codes = {v: k for k, v in SCOPE_SPEEDS.items()}
    if speed not in codes:
        raise CivError(f"no such sweep speed: {speed!r}")
    return SCOPE_SPEED + bytes([receiver & 0x01, codes[speed]])


def set_scope_vbw(vbw: str, receiver: int = 0) -> bytes:
    codes = {v: k for k, v in SCOPE_VBWS.items()}
    if vbw not in codes:
        raise CivError(f"no such VBW: {vbw!r}")
    return SCOPE_VBW + bytes([receiver & 0x01, codes[vbw]])


def set_scope_averaging(sweeps: int) -> bytes:
    """SCOPE > Averaging, 1A 05 0192: 0 (off), 2, 3 or 4 sweeps.

    The one menu write in this module, and deliberately not a general
    `set_menu` (see read_menu): the item number is fixed here, so no caller
    can reach the External P.AMP items through it.
    """
    codes = {v: k for k, v in SCOPE_AVERAGING.items()}
    if sweeps not in codes:
        raise CivError(f"averaging must be 0, 2, 3 or 4, not {sweeps!r}")
    return read_menu(SCOPE_AVERAGING_ITEM) + bytes([codes[sweeps]])


def parse_scope_setting(p: bytes) -> tuple[int, str, object] | None:
    """(receiver, key, value) from a 27 14/15/16/19/1A/1D answer, the key a
    `scope_settings` field name; None for anything else (wave data, the
    on/off and output answers, an acknowledgement)."""
    if len(p) < 4 or p[0] != 0x27 or p[1] not in SCOPE_SETTING_COMMANDS:
        return None
    sub, rx, data = p[1], p[2], p[3:]
    if sub == 0x14:
        return rx, "mode", SCOPE_SET_MODES.get(data[0])
    if sub == 0x15:
        if len(data) < 5:
            raise CivError(f"span answer too short: {p.hex(' ')}")
        return rx, "span_hz", 2 * decode_freq(bytes(data[:5]))
    if sub == 0x16:
        return rx, "edge", int(data[0])
    if sub == 0x19:
        return rx, "ref_db", decode_ref_db(bytes(data[:3]))
    if sub == 0x1A:
        return rx, "speed", SCOPE_SPEEDS.get(data[0])
    return rx, "vbw", SCOPE_VBWS.get(data[0])


# -- D-STAR (DV): who is calling, command 0x20 ------------------------------
#
# IC-9700 CI-V Reference Guide p. 12 (table) and p. 20 (formats). Each block
# has the same three sub-commands: 00 switches the radio's automatic output
# of it on or off (a radio setting, and so not touched here), 01 is the
# block as the radio sends it unasked once that is on, and 02 reads it. This
# module only reads (02), and the reader accepts 01 as well in case the
# operator has switched the automatic output on themselves.
#
#     20 00 <01|02> <38 bytes>  RX call signs: 2 header-flag bytes, caller
#                               (8), the caller's note (4, the "/suffix"),
#                               called station UR (8), RPT1 (8), RPT2 (8)
#     20 01 <01|02> <32 bytes>  RX message: message (20), caller (8), note (4)
#     20 02 <01|02> <1 byte>    RX status bits
#
# A block that is one FF byte means nothing has been received since the
# radio was switched on. Characters are the guide's "codes for character
# entries" (p. 15), which are ASCII.
DV_CALLSIGNS = 0x00
DV_MESSAGE = 0x01
DV_STATUS = 0x02


def read_dv(block: int) -> bytes:
    if block not in (DV_CALLSIGNS, DV_MESSAGE, DV_STATUS):
        raise CivError(f"no such DV block: {block!r}")
    return bytes([0x20, block, 0x02])


def _dv_text(b: bytes) -> str:
    return bytes(c if 0x20 <= c < 0x7F else 0x20 for c in b).decode("ascii").strip()


def parse_dv(p: bytes) -> tuple[str, dict | None] | None:
    """("callsigns" | "message" | "status", fields) from a 20 0x 01/02 frame;
    fields None when the radio says it has heard nothing since power-on.
    None for any other payload (including the 20 0x 00 setting's answer)."""
    if len(p) < 4 or p[0] != 0x20 or p[1] not in (0, 1, 2) or p[2] not in (0x01, 0x02):
        return None
    block, data = p[1], bytes(p[3:])
    if data[:1] == b"\xff" and len(data) <= 2:
        return ({0: "callsigns", 1: "message", 2: "status"}[block], None)
    if block == DV_CALLSIGNS:
        if len(data) < 38:
            raise CivError(f"DV call sign block is {len(data)} bytes, not 38")
        flags = data[0]
        return "callsigns", {
            "caller": _dv_text(data[2:10]),
            "suffix": _dv_text(data[10:14]),
            "ur": _dv_text(data[14:22]),
            "rpt1": _dv_text(data[22:30]),
            "rpt2": _dv_text(data[30:38]),
            "data": bool(flags & 0x10),
            "via_repeater": bool(flags & 0x08),
            "break_in": bool(flags & 0x04),
            "emr": bool(flags & 0x01),
        }
    if block == DV_MESSAGE:
        if len(data) < 20:
            raise CivError(f"DV message block is {len(data)} bytes, not 32")
        return "message", {
            "message": _dv_text(data[:20]),
            "caller": _dv_text(data[20:28]) if len(data) >= 28 else "",
            "suffix": _dv_text(data[28:32]) if len(data) >= 32 else "",
        }
    s = data[0]
    return "status", {
        "voice": bool(s & 0x40),
        "last_call_mine": bool(s & 0x20),
        "signal": bool(s & 0x10),
        "break_in": bool(s & 0x08),
        "emr": bool(s & 0x04),
        "not_dv": bool(s & 0x02),
        "packet_loss": bool(s & 0x01),
    }


# -- reading the wave data back -------------------------------------------
#
# A wave-data frame is
#
#     27 00 <receiver> <seq> <max> [<mode> <start 5> <end 5> <oor>] <levels...>
#
# with the bracketed part present only in sequence 1. `seq` and `max` are BCD
# in one byte each, so the eleventh of eleven pieces is 0x11 0x11, not 0x0b.
#
# Over a serial link the spectrum arrives as `max` frames and has to be
# stitched back together; sequence 1 carries the frequency span and no levels,
# and the rest carry levels only. Over the LAN it arrives whole - one frame
# with seq == max == 1, the span and all 475 levels together - which is why the
# assembler below has to handle "first and last at once" as its own case rather
# than as the end of a loop that never ran. wfview does the same, in
# parseSpectrum(); its `splitWaterfall` option is the opposite conversion, LAN
# frames chopped up to look like serial ones for the benefit of code that only
# knows the serial shape.
#
# The numbers are wfview's IC-9700 rig definition: SpectrumLenMax=475,
# SpectrumAmpMax=160, SpectrumSeqMax=11.

#: How many points across a full spectrum.
SCOPE_LEN_MAX = 475
#: The top of the amplitude scale. A level is 0x00..0xA0, and it is relative to
#: whatever the scope's reference is set to - the radio sends no calibration and
#: none is invented here.
SCOPE_AMP_MAX = 160
#: Frames per spectrum on a serial link. One, on the LAN.
SCOPE_SEQ_MAX = 11

SCOPE_MODES = {0x00: "center", 0x01: "fixed", 0x02: "scroll-C", 0x03: "scroll-F"}


def bcd_byte(b: int) -> int:
    """One byte of BCD as a number: 0x11 is eleven, not seventeen."""
    hi, lo = b >> 4, b & 0x0F
    if hi > 9 or lo > 9:
        raise CivError(f"not BCD: {b:02x}")
    return hi * 10 + lo


def is_scope_data(p: bytes) -> bool:
    """Whether a payload is a wave-data frame rather than a scope setting."""
    return len(p) >= 5 and p[0] == 0x27 and p[1] == 0x00


class ScopeAssembler:
    """Wave-data frames in, whole spectra out.

    One instance per receiver is not needed: the receiver byte is carried in
    every frame and a spectrum for the sub receiver is kept apart from one for
    the main. `add` returns a spectrum when one is complete and None while it
    is still being collected, so a caller can hand it every frame and act on
    what comes back.
    """

    #: A spectrum longer than this is not a spectrum. Without the cap, a stream
    #: of continuation frames whose sequence 1 was lost grows a buffer for as
    #: long as the module runs.
    MAX_POINTS = SCOPE_LEN_MAX * 2

    def __init__(self) -> None:
        self._partial: dict[int, dict] = {}
        self.spectra = 0
        self.dropped = 0

    def add(self, p: bytes) -> dict | None:
        if not is_scope_data(p):
            return None
        receiver = p[2]
        seq = bcd_byte(p[3])
        seq_max = bcd_byte(p[4])
        if seq < 1 or seq_max < 1 or seq > seq_max:
            self.dropped += 1
            raise CivError(f"wave data sequence {seq} of {seq_max} makes no sense")

        if seq == 1:
            # 5 bytes of frequency each end, then one byte saying whether the
            # radio can show this span at all.
            if len(p) < 17:
                self.dropped += 1
                raise CivError(f"wave data header is {len(p)} bytes, too short")
            mode = p[5]
            start = decode_freq(p[6:11])
            end = decode_freq(p[11:16])
            if mode == 0x00:
                # Centre mode reports the centre and the half-span rather than
                # the two edges. Reporting it as sent would draw a waterfall
                # centred on the wrong frequency and a span far too narrow,
                # which looks like a plausible picture of nothing.
                start, end = start - end, start + end
            out_of_range = bool(p[16])
            spec = {
                "receiver": receiver,
                "mode": SCOPE_MODES.get(mode, f"unknown-{mode:02x}"),
                "start": start,
                "end": end,
                "out_of_range": out_of_range,
                "max": SCOPE_AMP_MAX,
                "data": bytearray(),
            }
            if out_of_range:
                # The radio is telling us it has nothing to draw. A flat floor
                # of the right width says that honestly; the alternative is the
                # last good spectrum staying on screen as though it were live.
                spec["data"] = bytearray(SCOPE_LEN_MAX)
                self._partial.pop(receiver, None)
                return self._finish(spec)
            spec["data"] += p[17:]
            if seq == seq_max:
                self._partial.pop(receiver, None)
                return self._finish(spec)
            self._partial[receiver] = spec
            return None

        spec = self._partial.get(receiver)
        if spec is None:
            # A continuation whose beginning we never saw - the first frames
            # after the scope is switched on, usually. Nothing to add it to.
            self.dropped += 1
            return None
        spec["data"] += p[5:]
        if len(spec["data"]) > self.MAX_POINTS:
            del self._partial[receiver]
            self.dropped += 1
            raise CivError("wave data ran past the length of a spectrum")
        if seq < seq_max:
            return None
        del self._partial[receiver]
        return self._finish(spec)

    def _finish(self, spec: dict) -> dict:
        spec["data"] = list(spec["data"])
        spec["points"] = len(spec["data"])
        self.spectra += 1
        return spec

    def reset(self) -> None:
        """Forget anything half-collected, as after a reconnection."""
        self._partial.clear()
