"""AX.25 frames, both ways: addresses, the control field, the PID, the
information field.

The parsing half came from the Icom app's APRS receiver, which only ever met
UI frames; this module is the frame codec for everything AX.25 2.2 defines,
so the connected-mode engine (step 2 of the Packet Radio app) has a codec to
stand on and nothing here has to be rewritten for it. Only the codec: what an
RR means to a link, and when to send one, belongs to that engine.

**Addresses.** Each is seven bytes: six characters of callsign shifted left
one bit, then a byte carrying the SSID in the middle four bits, a flag in the
bottom bit saying whether this is the last address, two reserved bits, and a
flag in the top bit whose meaning depends on where the address is - on the
first two it is half of the command/response pair, and on a digipeater it
means "this one has already repeated it", which is the whole of how a path is
read. The reserved bits are kept as they arrived, not normalised: a
digipeater re-encodes the frame it heard, and some networks (DAMA, a few
modulo-128 implementations) put meaning in them.

The shift by one is there so the bottom bit of every byte is free for the
end-of-address marker. It also means a byte with its bottom bit set in the
middle of a callsign is a frame that has gone wrong, and a byte that is not a
letter, a digit or a space after shifting is the same - both are checked,
because the alternative is a map with a station called "Q\\x03\\x1f" on it.

Order is destination, then source, then up to eight digipeaters. That is the
opposite way round from how it is written - `G0ABC>APRS` puts the sender first
- and this returns them named rather than positional so nobody has to remember
which way it was.

**The control field** says what kind of frame this is. Three families: I
frames carry numbered data (N(S), N(R)); S frames acknowledge or refuse it (RR,
RNR, REJ, SREJ, with N(R)); U frames set the link up and down and carry
unnumbered data (SABM, SABME, UA, DM, DISC, FRMR, UI, XID, TEST). Every one
has the poll/final bit. With modulo-8 sequence numbers it is one byte; with
modulo 128 (after SABME) I and S frames take two, low byte first, and U frames
still take one. **Nothing in the frame says which modulus is in use** - the
link knows, because it was there for the SABME - so `parse` takes it as an
argument and defaults to 8, which is everything APRS and nearly every BBS uses.

**Nothing here is trusted.** Every field in every frame arrived over the air
from a stranger. Callsigns are checked against the character set the protocol
allows, the information field is returned as bytes and decoded latin-1 - which
cannot fail - and anything that does not parse returns None rather than a
half-built object. A frame that half-parses is worse than one that is thrown
away, because it reaches the screen. A control byte that names no frame type
AX.25 defines is one of those.

Frames here never carry their FCS: hdlc.Framer checks it and drops it, and
hdlc.frame_bits adds it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .errors import PacketError

#: What a callsign may contain once the shift is undone. Not a regular
#: expression for a valid amateur callsign: APRS destinations are not
#: callsigns at all (APDW17 is direwolf saying which software sent it) and
#: aliases like WIDE2-2 and TRACE are not either.
CALL_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")

#: AX.25 2.2 allows two digipeaters fewer than 2.0's eight; receivers still
#: meet eight, so eight is what is accepted.
MAX_DIGIS = 8

# -- PIDs: what protocol the information field carries (AX.25 2.2 §6.2.4) ---

PID_ISO_8208 = 0x01
PID_TCP_COMPRESSED = 0x06
PID_TCP_UNCOMPRESSED = 0x07
PID_SEGMENT = 0x08
PID_TEXNET = 0xC3
PID_LQP = 0xC4
PID_APPLETALK = 0xCA
PID_APPLETALK_ARP = 0xCB
PID_IP = 0xCC
PID_ARP = 0xCD
PID_FLEXNET = 0xCE
PID_NETROM = 0xCF
#: No layer three: plain text to a BBS, and every APRS packet.
PID_NONE = 0xF0
PID_ESCAPE = 0xFF

#: The kept UI control byte values, for the APRS layer: 0x13 is UI with the
#: poll bit set, which some implementations send.
UI = 0x03
UI_POLL = 0x13


class FrameType(enum.Enum):
    I = "I"            # noqa: E741 - the protocol's own name
    RR = "RR"
    RNR = "RNR"
    REJ = "REJ"
    SREJ = "SREJ"
    SABM = "SABM"
    SABME = "SABME"
    UA = "UA"
    DM = "DM"
    DISC = "DISC"
    FRMR = "FRMR"
    UI = "UI"
    XID = "XID"
    TEST = "TEST"


#: The S frames' two type bits (bits 3 and 4 of the first control byte).
_S_BITS = {FrameType.RR: 0, FrameType.RNR: 1, FrameType.REJ: 2, FrameType.SREJ: 3}
_S_FROM_BITS = {v: k for k, v in _S_BITS.items()}

#: The U frames' control byte with the P/F bit (0x10) clear.
_U_BYTES = {
    FrameType.SABME: 0x6F, FrameType.SABM: 0x2F, FrameType.DISC: 0x43,
    FrameType.DM: 0x0F, FrameType.UA: 0x63, FrameType.FRMR: 0x87,
    FrameType.UI: 0x03, FrameType.XID: 0xAF, FrameType.TEST: 0xE3,
}
_U_FROM_BYTE = {v: k for k, v in _U_BYTES.items()}

#: Which frames carry a PID byte before their information field. Only these
#: two: an XID's or a TEST's information starts straight after the control
#: field, and an FRMR's is its fixed three (or five) bytes.
_HAS_PID = (FrameType.I, FrameType.UI)


class FrameError(PacketError):
    """A frame that cannot be built as asked: a callsign with a character
    AX.25 cannot carry, a sequence number outside the modulus. A bug in the
    caller, for the developer; an app checks a callsign before it gets here
    and says so in its own words."""


@dataclass(frozen=True)
class Control:
    """The control field, decoded. `nr` is set on I and S frames, `ns` on I
    frames only; `pf` is the poll bit on a command and the final bit on a
    response."""
    type: FrameType
    pf: bool = False
    nr: int | None = None
    ns: int | None = None
    modulo: int = 8

    def __post_init__(self):
        if self.modulo not in (8, 128):
            raise FrameError(f"modulo {self.modulo}: AX.25 has 8 and 128")
        numbered = self.type is FrameType.I or self.type in _S_BITS
        if not numbered:
            # A U frame is one byte at either modulus and has no sequence
            # numbers, so its modulus is meaningless: kept at 8 so that two
            # equal U frames compare equal whichever link they came from.
            object.__setattr__(self, "modulo", 8)
        for name in ("nr", "ns") if self.type is FrameType.I else ("nr",):
            value = getattr(self, name)
            if numbered and (value is None or not 0 <= value < self.modulo):
                raise FrameError(f"{self.type.value} needs {name} in 0..{self.modulo - 1}, not {value}")

    @property
    def size(self) -> int:
        return 2 if self.modulo == 128 and (self.type is FrameType.I or self.type in _S_BITS) else 1

    def encode(self) -> bytes:
        pf = 1 if self.pf else 0
        if self.type is FrameType.I:
            if self.modulo == 8:
                return bytes([(self.nr << 5) | (pf << 4) | (self.ns << 1)])
            return bytes([self.ns << 1, (self.nr << 1) | pf])
        if self.type in _S_BITS:
            s = (_S_BITS[self.type] << 2) | 0x01
            if self.modulo == 8:
                return bytes([(self.nr << 5) | (pf << 4) | s])
            return bytes([s, (self.nr << 1) | pf])
        return bytes([_U_BYTES[self.type] | (pf << 4)])

    @classmethod
    def decode(cls, data: bytes, modulo: int = 8) -> Control | None:
        """The control field at the start of `data`, or None if it names no
        frame AX.25 defines (or is cut short)."""
        if not data:
            return None
        first = data[0]
        if (first & 0x01) == 0 or (first & 0x03) == 0x01:
            # I (xxxxxxx0) or S (xxxxxx01): one byte or two by the modulus.
            if modulo == 128:
                if len(data) < 2:
                    return None
                second = data[1]
                pf, nr = bool(second & 0x01), second >> 1
                if (first & 0x01) == 0:
                    return cls(FrameType.I, pf, nr, first >> 1, 128)
                if first & 0xF0:
                    # Bits 5-8 of a modulo-128 S frame's first byte are
                    # reserved zeros; set, this is not one.
                    return None
                return cls(_S_FROM_BITS[(first >> 2) & 0x03], pf, nr, None, 128)
            pf, nr = bool(first & 0x10), first >> 5
            if (first & 0x01) == 0:
                return cls(FrameType.I, pf, nr, (first >> 1) & 0x07, 8)
            return cls(_S_FROM_BITS[(first >> 2) & 0x03], pf, nr, None, 8)
        kind = _U_FROM_BYTE.get(first & ~0x10)
        if kind is None:
            return None
        return cls(kind, bool(first & 0x10), modulo=modulo)


@dataclass(frozen=True)
class Address:
    call: str
    ssid: int = 0
    #: The top bit. On the destination and source it is the command/response
    #: bit; on a digipeater it means this station has already repeated the
    #: frame, which is what the asterisk means when a path is printed.
    flag: bool = False
    last: bool = False
    #: The two reserved bits as they arrived, 0b11 unless agreed otherwise.
    reserved: int = 0b11

    def __str__(self) -> str:
        return f"{self.call}-{self.ssid}" if self.ssid else self.call

    @property
    def repeated(self) -> bool:
        return self.flag

    @classmethod
    def from_text(cls, text: str, flag: bool | None = None) -> Address:
        """`G0ABC-9`, or `GB7AY*` for a digipeater that has repeated the
        frame. Upper-cases, because AX.25 has no lower case."""
        star = text.endswith("*")
        call, _, ssid = text.rstrip("*").strip().upper().partition("-")
        if not call or len(call) > 6 or any(ch not in CALL_CHARS or ch == " " for ch in call):
            raise FrameError(f"{text!r} is not an AX.25 callsign: 1-6 letters and digits")
        if ssid and (not ssid.isdigit() or not 0 <= int(ssid) <= 15):
            raise FrameError(f"{text!r}: the SSID is 0 to 15")
        return cls(call, int(ssid or 0), star if flag is None else flag)

    def encode(self, last: bool) -> bytes:
        if not self.call or len(self.call) > 6 or any(ch not in CALL_CHARS or ch == " " for ch in self.call):
            raise FrameError(f"{self.call!r} is not an AX.25 callsign")
        if not 0 <= self.ssid <= 15:
            raise FrameError(f"SSID {self.ssid} is not 0 to 15")
        out = bytearray(ord(ch) << 1 for ch in self.call.ljust(6))
        out.append((0x80 if self.flag else 0) | ((self.reserved & 0x03) << 5)
                   | (self.ssid << 1) | (1 if last else 0))
        return bytes(out)


def decode_address(raw: bytes) -> Address | None:
    """Seven bytes to an address, or None if they are not one."""
    if len(raw) != 7:
        return None
    chars = []
    for byte in raw[:6]:
        if byte & 1:
            # The end-of-address bit set inside the callsign: this is not an
            # address, it is the middle of something else being read as one.
            return None
        ch = chr(byte >> 1)
        if ch not in CALL_CHARS:
            return None
        chars.append(ch)
    call = "".join(chars).rstrip()
    if not call or " " in call:
        # Trailing spaces pad a short callsign; an embedded one does not
        # happen in a frame that was sent correctly.
        return None
    ssid = (raw[6] >> 1) & 0x0F
    return Address(call=call, ssid=ssid, flag=bool(raw[6] & 0x80), last=bool(raw[6] & 0x01),
                   reserved=(raw[6] >> 5) & 0x03)


@dataclass
class Frame:
    source: Address
    destination: Address
    via: list[Address] = field(default_factory=list)
    control: Control = field(default_factory=lambda: Control(FrameType.UI))
    pid: int | None = PID_NONE
    info: bytes = b""

    # -- building ------------------------------------------------------------

    @classmethod
    def build(cls, source: str | Address, destination: str | Address, control: Control,
              info: bytes = b"", via=(), pid: int | None = None, command: bool | None = True) -> Frame:
        """A frame from its parts. `command` sets the AX.25 2.x
        command/response bits (destination C=1, source C=0 for a command; the
        other way round for a response; None leaves them as given). `pid`
        defaults to "no layer 3" on I and UI frames and is refused on the rest,
        which carry none."""
        src = source if isinstance(source, Address) else Address.from_text(source)
        dst = destination if isinstance(destination, Address) else Address.from_text(destination)
        if command is not None:
            dst = Address(dst.call, dst.ssid, command, reserved=dst.reserved)
            src = Address(src.call, src.ssid, not command, reserved=src.reserved)
        hops = [h if isinstance(h, Address) else Address.from_text(h) for h in via]
        if len(hops) > MAX_DIGIS:
            raise FrameError(f"{len(hops)} digipeaters; AX.25 carries at most {MAX_DIGIS}")
        if control.type in _HAS_PID:
            pid = PID_NONE if pid is None else pid
            if not 0 <= pid <= 0xFF:
                raise FrameError(f"PID {pid} is not a byte")
        elif pid is not None:
            raise FrameError(f"{control.type.value} frames carry no PID")
        return cls(source=src, destination=dst, via=hops, control=control, pid=pid, info=bytes(info))

    @classmethod
    def ui(cls, source, destination, info: bytes | str, via=(), pid: int = PID_NONE) -> Frame:
        """A UI frame, which is every APRS packet. Text is encoded latin-1,
        as APRS reads it."""
        if isinstance(info, str):
            info = info.encode("latin-1")
        return cls.build(source, destination, Control(FrameType.UI), info, via, pid)

    @classmethod
    def from_monitor(cls, line: str) -> Frame:
        """A monitor line - `G0ABC-9>APRS,WIDE1-1*:text` - as a UI frame:
        the form beacons are written in and the tests build packets from."""
        head, sep, info = line.partition(":")
        route, _, via = head.partition(",")
        source, arrow, destination = route.partition(">")
        if not sep or not arrow:
            raise FrameError(f"{line!r} is not SOURCE>DEST[,VIA...]:INFO")
        return cls.ui(source, destination, info, via=[v for v in via.split(",") if v])

    def encode(self) -> bytes:
        """The frame's bytes, without the FCS (hdlc adds it)."""
        out = bytearray(self.destination.encode(last=False))
        out += self.source.encode(last=not self.via)
        for i, hop in enumerate(self.via):
            out += hop.encode(last=i == len(self.via) - 1)
        out += self.control.encode()
        if self.control.type in _HAS_PID:
            out.append(PID_NONE if self.pid is None else self.pid)
        out += self.info
        return bytes(out)

    # -- reading -------------------------------------------------------------

    @property
    def type(self) -> FrameType:
        return self.control.type

    @property
    def is_ui(self) -> bool:
        return self.control.type is FrameType.UI

    @property
    def command(self) -> bool | None:
        """True for a command, False for a response, None for an AX.25 1.x
        frame whose two C bits are equal and so say neither."""
        if self.destination.flag == self.source.flag:
            return None
        return self.destination.flag

    @property
    def path(self) -> list[str]:
        return [str(a) + ("*" if a.repeated else "") for a in self.via]

    @property
    def heard_from(self) -> str:
        """Which station's transmitter this frame actually came out of.

        The last digipeater that has set its "repeated" bit, or the source if
        none has. This is the one that matters for anything about propagation:
        a tracker two hundred miles away whose packet came through the local
        digipeater was not heard here, and drawing a line to it as though it
        was is the most misleading thing an APRS map can do.
        """
        for address in reversed(self.via):
            if address.repeated:
                return str(address)
        return str(self.source)

    @property
    def digipeated(self) -> bool:
        return self.heard_from != str(self.source)

    def text(self) -> str:
        """The information field as characters.

        latin-1 because it cannot fail and because APRS is bytes: the
        specification says printable ASCII, mic-E packs binary into the same
        field, and a station in Europe will put an accented character in a
        comment sooner or later. Decoding strictly would throw away the frame
        over a byte in a comment.
        """
        return self.info.decode("latin-1")

    def monitor(self) -> str:
        """The frame in the one-line form every packet program prints.

        `G0ABC-9>APDW17,WIDE1-1*,WIDE2-1:!5537.14N/00420.83W>`. Worth having
        exactly right: it is what direwolf prints, so a decode from here and a
        decode from there can be compared character for character, which is how
        this decoder was checked.
        """
        head = f"{self.source}>{self.destination}"
        if self.via:
            head += "," + ",".join(self.path)
        return head + ":" + self.text()


def parse(frame: bytes, modulo: int = 8) -> Frame | None:
    """A frame (without its FCS, which hdlc.Framer has checked) taken apart.

    The length and the structure are checked, because a frame can have a
    correct FCS over rubbish if the rubbish is what was sent. `modulo` is the
    link's: see the module docstring for why the frame cannot say.
    """
    body = bytes(frame)
    if len(body) < 15:
        return None
    destination = decode_address(body[0:7])
    source = decode_address(body[7:14])
    if destination is None or source is None:
        return None
    if destination.last:
        # The destination is never the last address; the source always follows
        # it. A frame saying otherwise has been read at the wrong offset.
        return None

    via: list[Address] = []
    at = 14
    last = source.last
    while not last:
        if len(via) >= MAX_DIGIS or at + 7 > len(body):
            # More than eight digipeaters is not legal, and running off the end
            # means the end-of-address bit was never set - either way what
            # follows cannot be trusted to be a control field.
            return None
        address = decode_address(body[at:at + 7])
        if address is None:
            return None
        via.append(address)
        last = address.last
        at += 7

    control = Control.decode(body[at:at + 2], modulo)
    if control is None:
        return None
    at += control.size
    pid = None
    if control.type in _HAS_PID:
        if at >= len(body):
            return None
        pid = body[at]
        at += 1
    return Frame(source=source, destination=destination, via=via,
                 control=control, pid=pid, info=body[at:])
