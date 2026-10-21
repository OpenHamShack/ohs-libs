"""The OHS session layer: what two OpenHamShack stations add to an ordinary
connected-mode session, without breaking it for anyone else.

A connected AX.25 session is a byte stream of text (PID F0, "no layer 3").
Everything here rides in that same stream, so it passes through a node or
a digipeater that only carries text - a separate PID would not. Text stays
text: what any other station sends arrives exactly as before. Two kinds of
thing are added between the lines (FILES.md has the whole wire format):

- **Frames**: `01 'O' 'H' 'S'`, a type byte, a 16-bit big-endian length,
  then the payload. HELLO says what this end understands; TEXTZ is text
  compressed with DEFLATE; the file transfer's own frames (filexfer.py)
  are the rest. 0x01 (SOH) is not something a person types into a chat or
  a BBS prints, and a 0x01 that is not followed by "OHS" stays text.
  There is no checksum: AX.25's FCS and the connected mode's sequence
  numbers already deliver the stream whole and in order, and a file is
  checked end to end with SHA-256.
- **A claim**: a protocol that takes the whole stream for a while, as
  YAPP does (yapp.py). While a claim is held every byte goes to it; when
  it lets go, what it did not use is text again.

**Nothing is sent to a station that has not shown it understands it.** A
HELLO goes out only when this station is offering a file, when the peer
sent one first, or when the app knows the peer as an OHS station from an
earlier session - never on its own to a node or BBS, where it would be a
line of junk at a command prompt. Text is compressed only after both ends
have said "z" in a HELLO, and only when it comes out smaller.

Compression is DEFLATE (RFC 1951, raw, no zlib header), which is published
and readable by anyone; nothing here is encryption, and nothing could be
(UK licence clause 11(2)).
"""

from __future__ import annotations

import json
import logging
import zlib
from typing import Callable

log = logging.getLogger(__name__)

MAGIC = b"\x01OHS"
HEADER = len(MAGIC) + 3
MAX_PAYLOAD = 0xFFFF
VERSION = 1

#: Frame types (FILES.md). Never renumber: they are on the air.
HELLO = 1
TEXTZ = 2
OFFER = 3
ACCEPT = 4
DECLINE = 5
DATA = 6
ACK = 7
DONE = 8
RESULT = 9
CANCEL = 10

NAMES = {HELLO: "HELLO", TEXTZ: "TEXTZ", OFFER: "OFFER", ACCEPT: "ACCEPT", DECLINE: "DECLINE", DATA: "DATA",
         ACK: "ACK", DONE: "DONE", RESULT: "RESULT", CANCEL: "CANCEL"}

#: What this end understands, as said in HELLO: file transfer, DEFLATE
#: for text and file data, and resuming a file at an offset.
CAPS = ("file", "z", "resume")

#: Text shorter than this goes as it is: DEFLATE's own overhead (a block
#: header and the sync-flush marker) costs more than a short line saves.
TEXTZ_MIN = 64

#: YAPP's "send init" (yapp.py): ENQ 01 at the start of a transfer.
ENQ_SI = b"\x05\x01"


def frame(kind: int, payload: bytes = b"") -> bytes:
    if not 0 < kind < 256:
        raise ValueError("frame type out of range")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("frame payload longer than 65535 bytes")
    return MAGIC + bytes([kind]) + len(payload).to_bytes(2, "big") + payload


def jframe(kind: int, obj: dict) -> bytes:
    """A frame whose payload is a JSON object (UTF-8, compact)."""
    return frame(kind, json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def inflate(data: bytes, limit: int = 1 << 20) -> bytes:
    """Raw DEFLATE back to bytes, refusing more than `limit` out (a peer
    cannot fill memory with a small bomb)."""
    d = zlib.decompressobj(-15)
    out = d.decompress(data, limit)
    if d.unconsumed_tail:
        raise ValueError("inflated text longer than the limit")
    return out


class Demux:
    """Bytes in; text and frames out, in the order they came.

    `feed(data)` returns a list of ("text", bytes), ("frame", type,
    payload) and ("enq", b"\x05\x01") - YAPP's send init, so a receiver
    can claim the stream before the bytes after it. A frame cut across two feeds is held until it is whole; so
    are up to three trailing bytes that might be the start of MAGIC. A
    claim (`claim(consumer)`) diverts everything to the consumer's
    `feed(data)`, which returns the bytes it did not use once it lets go
    (None while it still holds the stream)."""

    def __init__(self):
        self._buf = bytearray()
        self.claimant = None

    def claim(self, consumer) -> None:
        self.claimant = consumer

    def feed(self, data: bytes) -> list:
        out: list = []
        self._buf += data
        while self._buf:
            if self.claimant is not None:
                chunk, self._buf = bytes(self._buf), bytearray()
                rest = self.claimant.feed(chunk)
                if rest is None:
                    break
                self.claimant = None
                self._buf = bytearray(rest)
                continue
            at = self._buf.find(MAGIC[:1])
            enq = self._buf.find(ENQ_SI)
            if enq >= 0 and (at < 0 or enq < at):
                # YAPP's send init, whose 01 is not a frame's SOH: handed
                # up on its own, so a YAPP receiver can claim the stream
                # from exactly here.
                if enq:
                    out.append(("text", bytes(self._buf[:enq])))
                out.append(("enq", ENQ_SI))
                del self._buf[:enq + len(ENQ_SI)]
                continue
            if at < 0:
                out.append(("text", bytes(self._buf)))
                self._buf.clear()
                break
            if at > 0:
                out.append(("text", bytes(self._buf[:at])))
                del self._buf[:at]
                continue
            # SOH at the front: a frame, the start of one, or text.
            head = bytes(self._buf[:len(MAGIC)])
            if not MAGIC.startswith(head):
                out.append(("text", bytes(self._buf[:1])))
                del self._buf[:1]
                continue
            if len(self._buf) < HEADER:
                break                                   # wait for the rest of the header
            kind = self._buf[len(MAGIC)]
            n = int.from_bytes(self._buf[len(MAGIC) + 1:HEADER], "big")
            if len(self._buf) < HEADER + n:
                break
            out.append(("frame", kind, bytes(self._buf[HEADER:HEADER + n])))
            del self._buf[:HEADER + n]
        # Text before a lone SOH at the very end is handed on now; the SOH
        # waits for what follows it.
        return _merge(out)


def _raw(item) -> bytes:
    return item[1] if item[0] in ("text", "enq") else frame(item[1], item[2])


def _merge(items: list) -> list:
    merged: list = []
    for item in items:
        if item[0] == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + item[1])
        elif item[0] != "text" or item[1]:
            merged.append(item)
    return merged


class OhsSession:
    """The OHS layer over one engine.Connection.

    `attach` wraps the connection: what the app had as `on_data` becomes
    `on_text` here and receives only text (TEXTZ already inflated), and
    the connection's `send` becomes `send_text`, so every text path the
    app has - a chat line, a node command, a script, a mail - goes through
    one place and is compressed when both ends allow it, without each of
    them knowing. Frames go out by `send_frame`, on the connection's own
    send. Handlers for frame types are registered in `handlers` (the file
    service does); a frame nobody handles is dropped and counted.
    """

    def __init__(self, conn, *, caps=CAPS, app: str = "", on_hello: Callable | None = None):
        self.conn = conn
        self.caps = tuple(caps)
        self.app = app
        self.demux = Demux()
        self.peer_caps: tuple | None = None
        self.peer_app = ""
        self.hello_sent = False
        self.on_text: Callable[[bytes], None] | None = None
        self.on_hello = on_hello
        #: Called when text holds YAPP's "send init" (ENQ 01): a receiver
        #: that wants it claims the stream (yapp.py).
        self.on_enq: Callable[[OhsSession], None] | None = None
        self.handlers: dict[int, Callable[[OhsSession, bytes], None]] = {}
        self.unknown = 0
        self.text_raw_bytes = 0
        self.text_sent_bytes = 0
        self._raw_send = conn.send

    @classmethod
    def attach(cls, conn, **kw) -> OhsSession:
        sess = cls(conn, **kw)
        sess.on_text = conn.on_data
        conn.on_data = sess.feed
        conn.send = sess.send_text
        conn.ohs = sess
        return sess

    @property
    def peer_is_ohs(self) -> bool:
        return self.peer_caps is not None

    def compressing(self) -> bool:
        return "z" in self.caps and self.peer_caps is not None and "z" in self.peer_caps

    # -- out ---------------------------------------------------------------------------

    def hello(self) -> None:
        """Say what this end understands (once per session)."""
        if self.hello_sent:
            return
        self.hello_sent = True
        self._raw_send(jframe(HELLO, {"v": VERSION, "caps": list(self.caps), "app": self.app}))

    def send_frame(self, kind: int, payload: bytes = b"") -> None:
        self._raw_send(frame(kind, payload))

    def send_json(self, kind: int, obj: dict) -> None:
        self._raw_send(jframe(kind, obj))

    def send_raw(self, data: bytes) -> None:
        """Bytes as they are, for a protocol holding a claim (yapp.py)."""
        self._raw_send(data)

    def send_text(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("latin-1")
        self.text_raw_bytes += len(data)
        if self.compressing() and len(data) >= TEXTZ_MIN:
            packed = deflate(data)
            if len(packed) + HEADER < len(data):
                self.text_sent_bytes += len(packed) + HEADER
                self._raw_send(frame(TEXTZ, packed))
                return
        self.text_sent_bytes += len(data)
        self._raw_send(data)

    # -- in ----------------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        pending = data
        while pending:
            items = self.demux.feed(pending)
            pending = b""
            for i, item in enumerate(items):
                rest = b""
                if item[0] == "text":
                    self._text(item[1])
                elif item[0] == "enq":
                    if self.on_enq is not None:
                        self.on_enq(self)
                    if self.demux.claimant is not None:
                        rest = item[1]
                    else:
                        self._text(item[1])
                else:
                    self._frame(item[1], item[2])
                if self.demux.claimant is not None:
                    # Claimed part-way (a YAPP start in the text, or our own
                    # SI going out): what was already split belongs to the
                    # claimant, as bytes, along with anything held back.
                    later = b"".join(_raw(it) for it in items[i + 1:])
                    pending = rest + later + bytes(self.demux._buf)
                    self.demux._buf.clear()
                    break

    def _text(self, data: bytes) -> None:
        if self.on_text is not None and data:
            self.on_text(data)

    def _frame(self, kind: int, payload: bytes) -> None:
        if kind == HELLO:
            try:
                d = json.loads(payload.decode("utf-8"))
                caps = tuple(str(c) for c in d.get("caps", ()))
                app = str(d.get("app", ""))[:80]
            except (ValueError, AttributeError, TypeError):
                self.unknown += 1
                return
            first = self.peer_caps is None
            self.peer_caps, self.peer_app = caps, app
            self.hello()                                # answer, once
            if first and self.on_hello is not None:
                self.on_hello(self)
            return
        if kind == TEXTZ:
            try:
                text = inflate(payload)
                if self.on_text is not None and text:
                    self.on_text(text)
            except (ValueError, zlib.error):
                self.unknown += 1
            return
        handler = self.handlers.get(kind)
        if handler is None:
            self.unknown += 1
            return
        try:
            handler(self, payload)
        except Exception:                               # noqa: BLE001 - one bad frame never stops a session
            log.exception("OHS frame %s not handled", NAMES.get(kind, kind))
            self.unknown += 1
