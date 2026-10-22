"""Winlink's B2F: email through a Radio Message Server (RMS) packet
gateway, over one AX.25 connected-mode session.

    sess = b2f.call(engine, "GB7XYZ-10", mailroom, mycall="GM4BRI",
                    password=lambda: secret, locator="IO75RL", on_done=done)

B2F is FBB forwarding (fbb.py, FORWARDING.md) with three Winlink
changes, and this module is those changes; the turn-taking is the same:

1. **Proposals are `FC EM <MID> <size> <compressed size> 0`**, and the
   block's `F> HH` checksum is required.
2. **Messages travel compressed and binary**: SOH, a header (title and
   offset), STX blocks of at most 255 bytes, EOT and a checksum byte. The
   payload is LZHUF with a CRC (lzhuf.py), and what it decompresses to is
   a Winlink message: RFC 822-style header lines (`Mid`, `Date`, `From`,
   `To`, `Subject`, `Body: n`, `File: n name`...), a blank line, the body,
   then each attachment.
3. **The gateway challenges the caller** (`;PQ: 12345678`) and the
   caller answers `;PR: nnnnnnnn`, an MD5 of the challenge, the account
   password and a fixed salt. **The password itself never crosses the
   air**, and nothing is encrypted: anyone can read every message. (Ofcom:
   no encryption to obscure meaning. LZHUF is a published compression,
   which Winlink has always used on UK amateur bands.)

**References.** Pat's library wl2k-go (github.com/la5nta/wl2k-go, MIT,
Martin Hebnes Pedersen LA5NTA): fbb/handshake.go, b2f.go, proposal.go,
message.go and secure.go were read and followed; the salt is from there
(it came from paclink-unix). Winlink's own "B2F" protocol notes agree.
Things that are guesses are marked **[guess]**; nothing here has met a
real gateway yet - the tests run against `Gateway` below, which follows
the same notes, so a misreading would be made twice. Real-gateway checks
are a 0.3.1 job.

`Session` is the caller ("client") or the gateway ("master"); it knows
nothing of radios: bytes in (`feed`), bytes out (`send`). `call()` runs a
client over an engine connection; `Gateway` answers on an SSID, for the
simulator and the tests (it is not a Winlink server: it keeps mail in
memory and delivers nothing to the internet).
"""
from __future__ import annotations

import base64
import calendar
import email.header
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import lzhuf
from .i18n import _

log = logging.getLogger(__name__)

SOFTWARE = "OHS"
#: Ours: B2 compressed, FBB basic, hierarchical, MIDs, BIDs (Pat's set).
FEATURES = "B2FHM$"
MAX_BLOCK = 5
#: Bytes per STX block. The protocol allows 255; Pat sends 125 so that one
#: block fits an AX.25 PACLEN of 128 with its two framing bytes.
CHUNK = 125
IDLE_S = 120.0
HELLO_S = 60.0
#: Largest message we will accept or offer, compressed. Winlink's own
#: limit is larger [guess: 120 KB in Winlink Express' defaults]; a
#: 1200-baud link moves about 100 bytes a second, so the apps set less.
MAX_BYTES = 120_000

SOH, STX, EOT = 0x01, 0x02, 0x04

#: The salt for the secure login, from wl2k-go fbb/secure.go (which took it
#: from paclink-unix).
_SALT = bytes([
    77, 197, 101, 206, 190, 249, 93, 200, 51, 243, 93, 237, 71, 94, 239, 138, 68, 108, 70, 185, 225, 137,
    217, 16, 51, 122, 193, 48, 194, 195, 198, 175, 172, 169, 70, 84, 61, 62, 104, 186, 114, 52, 61, 168, 66,
    129, 192, 208, 187, 249, 232, 193, 41, 113, 41, 45, 240, 16, 29, 228, 208, 228, 61, 20])

DATE_FORMAT = "%Y/%m/%d %H:%M"
CHARSET = "ISO-8859-1"


def login_response(challenge: str, password: str) -> str:
    """The `;PR:` answer to a `;PQ:` challenge: eight digits from the
    first four bytes of MD5(challenge + password + salt), the top two bits
    dropped (wl2k-go secureLoginResponse). The password is case-sensitive."""
    digest = hashlib.md5(challenge.encode("latin-1") + password.encode("latin-1", "replace") + _SALT).digest()
    value = ((digest[3] & 0x3F) << 24) | (digest[2] << 16) | (digest[1] << 8) | digest[0]
    return f"{value:08d}"[-8:]


def sid(version: str) -> str:
    return f"[{SOFTWARE}-{version}-{FEATURES}]"


def sid_features(line: str) -> str | None:
    """The features of a SID line (the text after its last dash), upper
    case, or None for a line that is not a SID."""
    m = re.fullmatch(r"\[.*-([^-\]]*)\]", line.strip())
    return m.group(1).upper() if m else None


def new_mid(call: str, now: float | None = None) -> str:
    """A twelve-character message id, unique for ever from this station:
    base-32 of an MD5 of the time, the call and some randomness (wl2k-go
    GenerateMid hashes the time and the call)."""
    seed = f"{now if now is not None else time.time()!r}-{call.upper()}-{os.urandom(8).hex()}".encode()
    return base64.b32encode(hashlib.md5(seed).digest()).decode("ascii")[:12]


# ------------------------------------------------------------------ addresses
def address(text: str) -> str:
    """An address as Winlink's headers write it: a callsign upper case
    (`G0ABC`, and `g0abc@winlink.org` is the same), anything else with an
    @ as `SMTP:user@host` (wl2k-go AddressFromString)."""
    text = text.strip()
    if ":" in text:
        proto, _sep, rest = text.partition(":")
        return f"{proto.upper()}:{rest}" if proto.upper() == "SMTP" else text.upper()
    if "@" not in text:
        return text.upper()
    user, _sep, host = text.rpartition("@")
    if host.lower() == "winlink.org":
        return user.upper()
    return "SMTP:" + text


def is_internet(addr: str) -> bool:
    return address(addr).startswith("SMTP:")


def plain(addr: str) -> str:
    """For people: `SMTP:` taken off."""
    return addr[5:] if addr.upper().startswith("SMTP:") else addr


# ------------------------------------------------------------------ messages
@dataclass
class Attachment:
    name: str
    data: bytes


@dataclass
class WinlinkMessage:
    mid: str
    sender: str
    to: list[str]
    subject: str
    body: str
    date: float = 0.0
    cc: list[str] = field(default_factory=list)
    files: list[Attachment] = field(default_factory=list)
    type: str = "Private"
    mbo: str = ""
    #: Other header lines as they came (lower-cased names), kept on a read.
    extra: dict = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        """The message as a gateway expects it decompressed: `Mid` first,
        the rest in name order (as wl2k-go writes them), CR LF throughout,
        the body in ISO-8859-1 (characters it lacks become "?")."""
        body = self.body.replace("\r\n", "\n").replace("\n", "\r\n").encode("latin-1", "replace")
        headers: list[tuple[str, str]] = [
            ("Body", str(len(body))),
            ("Content-Transfer-Encoding", "8bit"),
            ("Content-Type", f"text/plain; charset={CHARSET}"),
            ("Date", time.strftime(DATE_FORMAT, time.gmtime(self.date or time.time()))),
        ]
        headers += [("File", f"{len(f.data)} {_encode_word(f.name)}") for f in self.files]
        headers.append(("From", address(self.sender)))
        headers.append(("Mbo", (self.mbo or self.sender).upper()))
        headers.append(("Subject", _encode_word(self.subject)))
        headers += [("To", address(a)) for a in self.to]
        headers += [("Cc", address(a)) for a in self.cc]
        headers.append(("Type", self.type))
        headers.sort(key=lambda kv: kv[0])
        out = bytearray(f"Mid: {self.mid}\r\n".encode("ascii"))
        for name, value in headers:
            out += f"{name}: {value}\r\n".encode("latin-1", "replace")
        out += b"\r\n" + body
        if self.files:
            out += b"\r\n"
        for f in self.files:
            out += f.data + b"\r\n"
        return bytes(out)

    @classmethod
    def from_bytes(cls, data: bytes) -> WinlinkMessage:
        """A message back from its bytes; ValueError when it does not
        parse (no Mid, a section shorter than its header says)."""
        data = bytes(data).lstrip(b" \t\r\n")
        end = data.find(b"\r\n\r\n")
        if end < 0:
            raise ValueError(_("no end of headers"))
        heads: list[tuple[str, str]] = []
        for raw in data[:end].split(b"\r\n"):
            line = raw.decode("latin-1")
            if line[:1] in (" ", "\t") and heads:
                heads[-1] = (heads[-1][0], heads[-1][1] + " " + line.strip())
                continue
            name, sep, value = line.partition(":")
            if sep:
                heads.append((name.strip().lower(), value.strip()))
        get = {}
        for name, value in heads:
            get.setdefault(name, []).append(value)
        mid = (get.get("mid") or [""])[0]
        if not mid:
            raise ValueError(_("no Mid"))
        at = end + 4
        try:
            size = int((get.get("body") or ["0"])[0])
        except ValueError:
            raise ValueError(_("bad Body size")) from None
        body_bytes = data[at:at + size]
        if len(body_bytes) != size:
            raise ValueError(_("body shorter than its header"))
        at += size
        charset = CHARSET
        m = re.search(r"charset=\"?([A-Za-z0-9_-]+)", (get.get("content-type") or [""])[0])
        if m:
            charset = m.group(1)
        try:
            body = body_bytes.decode(charset, "replace")
        except LookupError:
            body = body_bytes.decode("latin-1")
        files = []
        for spec in get.get("file", []):
            n, _sep, name = spec.partition(" ")
            if data[at:at + 2] == b"\r\n":
                at += 2
            try:
                n_bytes = int(n)
            except ValueError:
                raise ValueError(_("bad File size")) from None
            chunk = data[at:at + n_bytes]
            if len(chunk) != n_bytes:
                raise ValueError(_("attachment shorter than its header"))
            files.append(Attachment(_decode_word(name), chunk))
            at += n_bytes
        when = 0.0
        date = (get.get("date") or [""])[0]
        for fmt in (DATE_FORMAT, "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S"):
            try:
                when = float(calendar.timegm(time.strptime(date, fmt)))
                break
            except ValueError:
                continue
        known = {"mid", "body", "content-type", "content-transfer-encoding", "date", "file", "from", "mbo",
                 "subject", "to", "cc", "type"}
        return cls(mid=mid, sender=(get.get("from") or [""])[0], to=get.get("to", []), cc=get.get("cc", []),
                   subject=_decode_word((get.get("subject") or [""])[0]), body=body.replace("\r\n", "\n"),
                   date=when, files=files, type=(get.get("type") or ["Private"])[0],
                   mbo=(get.get("mbo") or [""])[0],
                   extra={k: v for k, v in get.items() if k not in known})


def _encode_word(text: str) -> str:
    """ASCII stays as it is; anything else is an RFC 2047 Q-word in
    ISO-8859-1, as Winlink's headers must be ASCII."""
    if all(32 <= ord(c) < 127 for c in text):
        return text
    return email.header.Header(text, "iso-8859-1").encode().replace("\n", "")


def _decode_word(text: str) -> str:
    if "=?" not in text:
        return text
    try:
        return str(email.header.make_header(email.header.decode_header(text)))
    except (ValueError, LookupError):
        return text


# ------------------------------------------------------------------ the exchange
class Mailroom(Protocol):
    def outgoing(self) -> list[WinlinkMessage]: ...

    def answer(self, mid: str, title: str, compressed_size: int) -> str: ...     # "+", "-" or "="

    def received(self, message: WinlinkMessage) -> None: ...

    def delivered(self, mid: str, how: str) -> None: ...      # "sent" | "had-it"


@dataclass
class _Offer:
    message: WinlinkMessage
    data: bytes          # compressed
    size: int            # uncompressed

    def line(self) -> str:
        return f"FC EM {self.message.mid} {self.size} {len(self.data)} 0"


def offer(msg: WinlinkMessage) -> _Offer:
    raw = msg.to_bytes()
    return _Offer(msg, lzhuf.compress(raw, crc=True), len(raw))


def checksum(lines: list[str]) -> str:
    total = sum(sum((line + "\r").encode("latin-1", "replace")) for line in lines)
    return f"{(-total) & 0xFF:02X}"


@dataclass
class Result:
    sent: int = 0
    received: int = 0
    refused: int = 0
    deferred: int = 0


class SessionError(Exception):
    pass


class Session:
    """One side of one B2F session. `role` is "client" (it connected) or
    "master" (the gateway). Feed it the bytes that arrive (`feed`); it
    calls `send(bytes)` and, when it is over, `close()`.

    Ends in `state` "done" or "failed" (`error` is a key: "no-b2",
    "login-failed", "protocol", "remote-error", "timeout", "lost").
    `remote_error` keeps the text of a `***` line from the gateway."""

    def __init__(self, role: str, mailroom: Mailroom, send: Callable[[bytes], None], close: Callable[[], None],
                 clock: Callable[[], float], *, mycall: str = "", target: str = "",
                 password: Callable[[], str] | None = None, locator: str = "", version: str = "0.3.0",
                 max_bytes: int = MAX_BYTES, check_password: Callable[[str, str], bool] | None = None,
                 challenge: str | None = None, on_done: Callable[[Session], None] | None = None):
        if role not in ("client", "master"):
            raise ValueError(role)
        self.role = role
        self.mailroom = mailroom
        self._send = send
        self._close = close
        self.clock = clock
        self.mycall = mycall.upper()
        self.target = target.upper()
        self.password = password
        self.locator = locator
        self.version = version
        self.max_bytes = max_bytes
        self.check_password = check_password
        self.on_done = on_done
        self.result = Result()
        self.error = ""
        self.remote_error = ""
        self.their_features = ""
        self.their_calls: list[str] = []      # the master: who the client collects for (;FW:)
        self._challenge = challenge
        self._response = ""
        self._buf = bytearray()
        self._remote_no_msgs = False
        self._offer: list[_Offer] = []
        self._in_flight: list[_Offer] = []
        self._block: list[tuple[str, int, int]] = []      # theirs: (mid, size, compressed)
        self._block_lines: list[str] = []
        self._accepted: list[tuple[str, int, int]] = []
        self._rx: dict | None = None
        self.deadline = clock() + (HELLO_S if role == "client" else IDLE_S)
        self.state = "hello" if role == "client" else "wait-handshake"
        #: Something saying how far a transfer has got: (direction, mid, bytes, total).
        self.progress: tuple[str, str, int, int] | None = None

    # ------------------------------------------------------------------ master start
    def greet(self, motd: list[str] = ()) -> None:
        """The gateway's opening (master only): SID, challenge, prompt."""
        for line in motd:
            self._line(line)
        self._line("[WL2K-5.0-B2FWIHJM$]")
        if self._challenge:
            self._line(f";PQ: {self._challenge}")
        self._line(f"CMS via {self.mycall} >")

    # ------------------------------------------------------------------ input
    def feed(self, data: bytes) -> None:
        if self.state in ("done", "failed"):
            return
        self._buf += data
        self.deadline = self.clock() + IDLE_S
        try:
            while self.state not in ("done", "failed"):
                if self.state == "receiving":
                    if not self._binary():
                        return
                    continue
                line = self._take_line()
                if line is None:
                    return
                getattr(self, "_in_" + self.state.replace("-", "_"))(line)
        except SessionError as e:
            self._fail("protocol", str(e))

    def _take_line(self) -> str | None:
        for i, b in enumerate(self._buf):
            if b in (0x0D, 0x0A):
                raw = bytes(self._buf[:i])
                cut = i + 1
                if b == 0x0D and cut < len(self._buf) and self._buf[cut] == 0x0A:
                    cut += 1
                del self._buf[:cut]
                return raw.decode("latin-1")
        if len(self._buf) > 4096:
            raise SessionError("a line too long")
        return None

    def _remote_says(self, line: str) -> bool:
        """A `***` line: the other side's error. Login failures read
        "*** Secure login failed ..." (wl2k-go IsLoginFailure)."""
        if not line.startswith("***"):
            return False
        self.remote_error = line.strip("* ").strip()
        key = "login-failed" if "secure login failed" in line.lower() else "remote-error"
        self._finish("failed", key)
        self._close()
        return True

    # client: the gateway's greeting
    def _in_hello(self, line: str) -> None:
        if self._remote_says(line):
            return
        feats = sid_features(line) if line.startswith("[") else None
        if feats is not None:
            self.their_features = feats
            return
        if line.startswith(";PQ:"):
            self._challenge = line[4:].strip()
            return
        if line.rstrip().endswith(">"):
            if "B2" not in self.their_features:
                self._line("FQ")
                self._finish("failed", "no-b2")
                self._close()
                return
            self._line(f";FW: {self.mycall.split('-')[0]}")
            self._line(sid(self.version))
            if self._challenge:
                pw = self.password() if self.password is not None else ""
                if not pw:
                    self._line("FQ")
                    self._finish("failed", "no-password")
                    self._close()
                    return
                self._line(f";PR: {login_response(self._challenge, pw)}")
            self._line(f"; {self.target} DE {self.mycall}" + (f" ({self.locator})" if self.locator else ""))
            self._my_turn()

    # master: the client's handshake, up to its first protocol line
    def _in_wait_handshake(self, line: str) -> None:
        if not line.strip():
            return
        if line.startswith(";FW:"):
            self.their_calls = [c.split("|")[0].upper() for c in line[4:].split()]
            return
        if line.startswith(";PR:"):
            self._response = line[4:].strip()
            return
        if line.startswith("["):
            feats = sid_features(line)
            if feats is None or "B2" not in feats:
                raise SessionError("a caller without B2")
            self.their_features = feats
            return
        if line.startswith(";"):
            return
        if not self.their_features:
            raise SessionError("no SID")
        if self._challenge and self.check_password is not None:
            who = self.their_calls[0] if self.their_calls else ""
            if not self.check_password(who, self._response):
                self._line("*** Secure login failed - account password does not match. - Disconnecting")
                self._finish("failed", "login-failed")
                self._close()
                return
        self.state = "their-turn"
        self._in_their_turn(line)

    def _in_wait_fs(self, line: str) -> None:
        s = line.strip()
        if not s or s.startswith(";"):
            return              # ;PM: lines (pending mail) and comments
        if self._remote_says(s):
            return
        if not s.upper().startswith("FS"):
            raise SessionError(f"expected FS, got {s[:40]!r}")
        answers = s[2:].strip().upper()
        if len(answers) != len(self._offer):
            raise SessionError("FS does not answer every proposal")
        offers, self._offer = self._offer, []
        for ch, o in zip(answers, offers):
            if ch in "+Y":
                self._send_message(o)
                self._in_flight.append(o)
            elif ch in "-NR":
                self.result.refused += 1
                self.mailroom.delivered(o.message.mid, "had-it")
            elif ch in "=LH":
                self.result.deferred += 1
            else:
                raise SessionError(f"FS answer {ch!r}")
        self.state = "their-turn"

    def _in_their_turn(self, line: str) -> None:
        s = line.strip()
        if not s:
            return
        if self._remote_says(s):
            return
        if s.startswith(";"):
            self._acknowledge()
            return
        up = s.upper()
        if up[:2] in ("FA", "FB", "FC", "FD"):
            self._acknowledge()
            words = s.split()
            if up[:2] != "FC" or len(words) < 6:
                # FA/FB are FBB's own; we take only Winlink's FC.
                self._block.append((words[2] if len(words) > 2 else "?", 0, -1))
            else:
                try:
                    self._block.append((words[2], int(words[3]), int(words[4])))
                except ValueError:
                    raise SessionError("a proposal that does not parse") from None
            self._block_lines.append(s)
            if len(self._block) > MAX_BLOCK:
                raise SessionError("more than five proposals")
            return
        if up.startswith("F>"):
            theirs = up[2:].strip()
            if theirs and theirs != checksum(self._block_lines):
                raise SessionError("proposal checksum")
            self._answer_block()
            return
        if up == "FF":
            self._acknowledge()
            self._remote_no_msgs = True
            self._my_turn()
            return
        if up == "FQ":
            self._acknowledge()
            self._finish("done")
            self._close()
            return
        raise SessionError(f"unexpected {s[:40]!r}")

    def _answer_block(self) -> None:
        block, self._block, self._block_lines = self._block, [], []
        if not block:
            self._remote_no_msgs = True
            self._my_turn()
            return
        self._remote_no_msgs = False
        answers, seen = "", set()
        for mid, size, csize in block:
            if csize < 0 or mid in seen:
                a = "="          # an FBB proposal, or the same MID twice in a block (wl2k-go defers it)
            elif csize > self.max_bytes:
                a = "-"
            else:
                a = self.mailroom.answer(mid, "", csize)
                a = a if a in "+-=" and a else "-"
            seen.add(mid)
            answers += a
            if a == "+":
                self._accepted.append((mid, size, csize))
        self._line("FS " + answers)
        if self._accepted:
            self.state = "receiving"
        else:
            self._my_turn()

    def _binary(self) -> bool:
        """One step of a binary message; False when it needs more bytes."""
        buf = self._buf
        rx = self._rx
        if rx is None:
            if not buf:
                return False
            if buf[0] == ord("*"):
                line = self._take_line()
                if line is None:
                    return False
                if not self._remote_says(line):
                    raise SessionError("an error line in a transfer")
                return True
            if buf[0] != SOH:
                raise SessionError("a transfer that does not start with SOH")
            if len(buf) < 2 or len(buf) < 2 + buf[1]:
                return False
            n = buf[1]
            head = bytes(buf[2:2 + n])
            del buf[:2 + n]
            parts = head.split(b"\0")
            if len(parts) < 3:
                raise SessionError("a transfer header that does not parse")
            mid, size, csize = self._accepted[0]
            offset = parts[1].decode("ascii", "replace")
            if offset not in ("0", ""):
                raise SessionError("an offset we did not ask for")
            self._rx = {"mid": mid, "title": _decode_word(parts[0].decode("latin-1")), "data": bytearray(),
                        "sum": 0, "csize": csize}
            return True
        if not buf:
            return False
        if buf[0] == STX:
            if len(buf) < 2:
                return False
            n = buf[1] or 256
            if len(buf) < 2 + n:
                return False
            chunk = bytes(buf[2:2 + n])
            del buf[:2 + n]
            rx["data"] += chunk
            rx["sum"] = (rx["sum"] + sum(chunk)) & 0xFF
            self.progress = ("in", rx["mid"], len(rx["data"]), rx["csize"])
            if len(rx["data"]) > max(rx["csize"], 0) or len(rx["data"]) > self.max_bytes:
                raise SessionError("a message longer than proposed")
            return True
        if buf[0] == EOT:
            if len(buf) < 2:
                return False
            check = buf[1]
            del buf[:2]
            if (rx["sum"] + check) & 0xFF:
                raise SessionError("transfer checksum")
            if len(rx["data"]) != rx["csize"]:
                raise SessionError("transfer length")
            try:
                raw = lzhuf.decompress(bytes(rx["data"]), crc=True, max_size=4 * self.max_bytes)
                msg = WinlinkMessage.from_bytes(raw)
            except (lzhuf.ChecksumError, ValueError) as e:
                raise SessionError(f"a message that does not decode: {e}") from None
            self._rx = None
            self._accepted.pop(0)
            self.mailroom.received(msg)
            self.result.received += 1
            if not self._accepted:
                self._my_turn()
            return True
        raise SessionError("an unexpected byte in a transfer")

    # ------------------------------------------------------------------ output
    def _line(self, text: str) -> None:
        self._send((text + "\r").encode("latin-1", "replace"))

    def _send_message(self, o: _Offer) -> None:
        title = _encode_word(o.message.subject or "No title")[:80]
        offset = "0"
        head = title.encode("ascii", "replace") + b"\0" + offset.encode() + b"\0"
        out = bytearray([SOH, len(head)]) + head
        total = 0
        for at in range(0, len(o.data), CHUNK):
            chunk = o.data[at:at + CHUNK]
            out += bytes([STX, len(chunk)]) + chunk
            total += sum(chunk)
        out += bytes([EOT, (-total) & 0xFF])
        self._send(bytes(out))
        self.progress = ("out", o.message.mid, len(o.data), len(o.data))

    def _acknowledge(self) -> None:
        sent, self._in_flight = self._in_flight, []
        for o in sent:
            self.result.sent += 1
            self.mailroom.delivered(o.message.mid, "sent")

    def _my_turn(self) -> None:
        offers = []
        for m in self.mailroom.outgoing():
            o = offer(m)
            if len(o.data) > self.max_bytes:
                log.info("b2f: %s is larger than %d bytes compressed; not offered", m.mid, self.max_bytes)
                continue
            offers.append(o)
            if len(offers) == MAX_BLOCK:
                break
        if offers:
            lines = [o.line() for o in offers]
            for line in lines:
                self._line(line)
            self._line(f"F> {checksum(lines)}")
            self._offer = offers
            self.state = "wait-fs"
            return
        if self._remote_no_msgs:
            self._line("FQ")
            self._finish("done")
            self._close()
            return
        self._line("FF")
        self.state = "their-turn"

    def _fail(self, key: str, why: str = "") -> None:
        log.info("b2f with %s failed: %s %s", self.target, key, why)
        try:
            self._line(f"*** {why or key}")
        except Exception:                               # noqa: BLE001 - the link may be gone already
            pass
        self._finish("failed", key)
        self._close()

    def _finish(self, state: str, error: str = "") -> None:
        if self.state in ("done", "failed"):
            return
        self.state = state
        self.error = error
        self._in_flight.clear()
        if self.on_done is not None:
            self.on_done(self)

    # ------------------------------------------------------------------ time
    def lost(self) -> None:
        if self.state not in ("done", "failed"):
            self._finish("failed", "lost")

    def poll(self, now: float, sending: bool = False) -> None:
        """`sending`: our own data is still going out (a big message at
        1200 baud takes minutes), so the idle clock waits for it."""
        if self.state in ("done", "failed"):
            return
        if sending:
            self.deadline = max(self.deadline, now + IDLE_S)
        elif now >= self.deadline:
            self._fail("timeout", "no answer in time")

    def next_deadline(self) -> float | None:
        return None if self.state in ("done", "failed") else self.deadline


# ------------------------------------------------------------------ over AX.25
class _Timer:
    def __init__(self, sess: Session, conn):
        self.sess = sess
        self.conn = conn

    @property
    def ended(self) -> bool:
        return self.sess.state in ("done", "failed")

    def poll(self, now: float) -> None:
        self.sess.poll(now, sending=bool(getattr(self.conn.link, "queued", 0)))

    def next_deadline(self) -> float | None:
        return self.sess.next_deadline()


def _attach(engine, conn, sess: Session) -> None:
    def data(chunk: bytes) -> None:
        sess.feed(chunk)

    previous = conn.on_event

    def event(e, previous=previous) -> None:
        if e.kind == "disconnected":
            sess.lost()
        if previous is not None:
            previous(e)

    conn.on_data = data
    conn.on_event = event
    with engine.lock:
        engine.services[:] = [s for s in engine.services if not (isinstance(s, _Timer) and s.ended)]
        engine.services.append(_Timer(sess, conn))


def call(engine, gateway: str, mailroom: Mailroom, *, mycall: str, password: Callable[[], str],
         via=(), locator: str = "", version: str = "0.3.0", max_bytes: int = MAX_BYTES,
         on_done: Callable[[Session], None] | None = None) -> Session:
    """Connect to an RMS gateway and exchange mail as the client. The
    session's `on_done` hears how it ended, a connection that never came
    up included (`error` "lost")."""
    conn = engine.connect(gateway, via=list(via))
    sess = Session("client", mailroom, conn.send, conn.close, engine.clock, mycall=mycall, target=gateway,
                   password=password, locator=locator, version=version, max_bytes=max_bytes, on_done=on_done)
    _attach(engine, conn, sess)
    sess.connection = conn
    return sess


class Gateway:
    """A pretend RMS gateway on one SSID: it challenges, checks the
    password with `accounts` ({call: password}; None takes any password,
    for the app's simulator, where the tester's real one must not be
    needed), keeps what it is sent in `received`, and offers each call
    what is in `waiting[call]`. `welcome(call)`, if given, is asked once
    per call for messages to put there first. For the simulator and the
    tests; nothing leaves it."""

    def __init__(self, engine, ssid: int = 10, accounts: dict | None = None,
                 challenge: Callable[[], str] | None = None,
                 welcome: Callable[[str], list[WinlinkMessage]] | None = None):
        self.engine = engine
        self.accounts = None if accounts is None else {k.upper(): v for k, v in accounts.items()}
        self.welcome = welcome
        self._welcomed: set[str] = set()
        self.received: list[WinlinkMessage] = []
        self.waiting: dict[str, list[WinlinkMessage]] = {}
        self.sessions: list[Session] = []
        self._challenge = challenge or (lambda: f"{int.from_bytes(os.urandom(4), 'big') % 100_000_000:08d}")
        self.addr = engine.station.address(ssid)
        engine.listen(self.addr, self._accept)

    def _accept(self, call):
        return self._open

    def _open(self, conn) -> None:
        gw = self
        remote = str(conn.remote).split("-")[0].upper()
        if self.welcome is not None and remote not in self._welcomed:
            self._welcomed.add(remote)
            self.waiting.setdefault(remote, []).extend(self.welcome(remote))

        class _Room:
            def outgoing(self):
                return list(gw.waiting.get(remote, []))

            def answer(self, mid, title, size):
                return "-" if any(m.mid == mid for m in gw.received) else "+"

            def received(self, msg):
                gw.received.append(msg)

            def delivered(self, mid, how):
                gw.waiting[remote] = [m for m in gw.waiting.get(remote, []) if m.mid != mid]

        def check(who: str, response: str) -> bool:
            if self.accounts is None:
                return bool(response)
            pw = self.accounts.get(who.split("-")[0].upper())
            return pw is not None and response == login_response(sess._challenge, pw)

        sess = Session("master", _Room(), conn.send, conn.close, self.engine.clock, mycall=str(self.addr),
                       target=remote, challenge=self._challenge(), check_password=check)
        self.sessions.append(sess)
        previous = conn.on_event

        def on_event(e, previous=previous):
            if e.kind == "connected":
                sess.greet()
            if previous is not None:
                previous(e)
        conn.on_event = on_event
        _attach(self.engine, conn, sess)
