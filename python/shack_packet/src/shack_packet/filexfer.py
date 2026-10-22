"""File and picture transfer over a connected-mode session.

Two protocols, one set of transfers:

- **OHS** (this module, FILES.md): the default between OpenHamShack
  stations. OFFER (name, size, type, SHA-256), the receiving operator's
  ACCEPT with the offset it already has (0, or more when resuming), DATA
  in chunks, ACKs of progress, DONE, and RESULT once the receiver has
  checked the whole file against the SHA-256. Either end may CANCEL.
  Data is DEFLATE-compressed unless the file is compressed already (JPEG,
  PNG, ZIP...) or would not shrink.
- **YAPP** (yapp.py), for other packet software; same transfers, same
  inbox, no resume across a different protocol.

**Resume.** A transfer is known by its SHA-256 and size, not by a session:
a receiver keeps what it has as `<name>.part` beside a small JSON note
(sender, name, size, hash, bytes held) in its inbox, and answers a later
OFFER of the same file - in this session or after the link dropped and
was made again, even after a restart - with the offset it holds. The
sender reads from there. The hash at the end checks the whole file, the
part sent before the drop included, so a resumed file is as good as one
sent in one go; if it does not match, the part is thrown away and the
sender is told (RESULT, not ok), and sending again starts from nothing.

**What arrives is never opened or run.** Files land in the inbox folder
under a name made safe (no path, no control characters, no leading dot,
not a Windows device name, nothing replaced: "name (2).ext" when taken),
written without execute permission. Opening one is the operator's own
act, in the app.

**Nothing encrypted is sent** (UK licence clause 11(2)): a file that is
evidently encrypted - OpenPGP, age, an OpenSSL "Salted__" file, a ZIP
whose entries are encrypted - is refused before anything is keyed, with
the reason (`encrypted_reason`). Like the IP guard it catches what is
plainly encrypted, not everything that could be.

All of it runs under the engine's lock: the service is an engine service
(`poll`, `next_deadline`), its frames arrive in the connection's
`on_data`, and the app calls `send_file`, `accept`, `decline`, `cancel`
and `resume` from its own thread through the same lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ohssession as oh
from .i18n import _

log = logging.getLogger(__name__)

#: Raw bytes per DATA frame. Big enough that the 13-byte DATA header and
#: the 7-byte frame header cost 2 %, small enough that a cancel or a chat
#: line is not stuck behind much; the engine cuts it into I frames of N1.
CHUNK = 1024
#: The receiver ACKs at least this often (bytes); smaller files every 5 %.
ACK_EVERY = 8 * 1024
#: Waiting for a station that never said HELLO back: it is not an OHS
#: station (or not there), so the offer is given up.
NO_HELLO_S = 90.0
#: Waiting for the receiving operator to decide.
DECIDE_S = 600.0
#: Biggest offer a receiver takes by default (the app has a setting).
MAX_SIZE = 50 * 1024 * 1024

#: Reasons, as keys the app translates (DECLINE, CANCEL, RESULT, and the
#: transfer's `reason`).
REASONS = {
    "declined": "the other station declined the file",
    "too-big": "the file is bigger than the other station accepts",
    "cancelled": "cancelled",
    "peer-cancelled": "the other station cancelled",
    "link-lost": "the link dropped; the transfer can be resumed",
    "timeout": "nothing heard from the other station for two minutes; the transfer can be resumed",
    "no-answer": "no answer: the other station may not run OpenHamShack (try YAPP)",
    "not-decided": "the other station did not decide in time",
    "hash-mismatch": "the file arrived damaged (SHA-256 did not match); sending again starts afresh",
    "bad-offset": "the other station asked for a part of the file that does not exist",
    "write-failed": "the file could not be written",
    "read-failed": "the file could not be read",
    "encrypted": "the file is encrypted, and encrypted messages may not be sent (licence clause 11(2))",
    "protocol": "the other station sent something this protocol does not allow",
    "not-connected": "not connected",
}

#: Types already compressed: DEFLATE would only add to them.
PACKED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif", ".zip", ".gz", ".tgz", ".bz2", ".xz",
              ".zst", ".7z", ".rar", ".mp3", ".ogg", ".opus", ".m4a", ".mp4", ".mkv", ".webm", ".pdf", ".docx",
              ".xlsx", ".pptx", ".odt", ".jar", ".apk", ".lzh", ".lha", ".arj"}
_PACKED_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"\xfd7zXZ",
                 b"7z\xbc\xaf", b"Rar!", b"\x28\xb5\x2f\xfd", b"RIFF", b"%PDF", b"OggS")


def encrypted_reason(head: bytes) -> str | None:
    """'encrypted' when the start of a file shows it is plainly encrypted,
    else None. `head` is the first 64 KiB or so."""
    if head.startswith(b"Salted__"):                      # openssl enc
        return "encrypted"
    if head.startswith(b"age-encryption.org/v1") or b"-----BEGIN AGE ENCRYPTED FILE-----" in head[:200]:
        return "encrypted"
    if b"-----BEGIN PGP MESSAGE-----" in head[:4096]:
        return "encrypted"
    if head[:1] and head[0] & 0x80:
        # OpenPGP binary: a packet tag of public-key or symmetric-key
        # encrypted session key (1, 3), or encrypted data (9, 18, 20).
        b = head[0]
        tag = (b & 0x3F) if b & 0x40 else ((b >> 2) & 0x0F)
        if tag in (1, 3) and len(head) > 8 and head[2 if b & 0x40 else 3] in (3, 4, 5, 6):
            return "encrypted"
    if head.startswith(b"PK\x03\x04") and len(head) >= 8:
        # Every local header: general-purpose flag bit 0 = this entry is
        # encrypted. Checked over the head, which is where small ZIPs end.
        at = 0
        while 0 <= at <= len(head) - 30 and head[at:at + 4] == b"PK\x03\x04":
            if int.from_bytes(head[at + 6:at + 8], "little") & 1:
                return "encrypted"
            size = int.from_bytes(head[at + 18:at + 22], "little")
            n = int.from_bytes(head[at + 26:at + 28], "little")
            m = int.from_bytes(head[at + 28:at + 30], "little")
            flags = int.from_bytes(head[at + 6:at + 8], "little")
            if flags & 0x08:                              # sizes in a trailer: cannot walk on
                break
            at += 30 + n + m + size
    return None


def worth_compressing(name: str, head: bytes) -> bool:
    """False for a file that is compressed already, by its name, its first
    bytes, or a trial: DEFLATE on the head saving less than 5 %."""
    if Path(name).suffix.lower() in PACKED_EXT:
        return False
    if any(head.startswith(m) for m in _PACKED_MAGIC):
        return False
    if not head:
        return False
    sample = head[:65536]
    return len(oh.deflate(sample)) < 0.95 * len(sample)


_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """A received name made safe to create in the inbox: the last path
    part only, no control or reserved characters, no leading dots or
    spaces, not a Windows device name, at most 120 characters."""
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE.sub("_", name).strip().lstrip(". ").rstrip(". ")
    if not name:
        name = "received"
    stem, dot, ext = name.rpartition(".")
    base = (stem if dot else name).upper()
    if base in _DEVICES:
        name = "_" + name
    if len(name) > 120:
        ext = Path(name).suffix[:16]
        name = name[:120 - len(ext)] + ext
    return name


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    if not p.exists():
        return p
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(2, 10_000):
        p = folder / f"{stem} ({i}){ext}"
        if not p.exists():
            return p
    raise OSError("no free name")


def file_hash(path: Path, upto: int | None = None) -> hashlib._Hash:
    h = hashlib.sha256()
    left = upto
    with open(path, "rb") as f:
        while left is None or left > 0:
            block = f.read(1 << 16 if left is None else min(1 << 16, left))
            if not block:
                break
            h.update(block)
            if left is not None:
                left -= len(block)
    return h


@dataclass
class Transfer:
    """One file, either way. `state`: offering (out, waiting for the
    answer), offered (in, waiting for our operator), sending, receiving,
    checking, done, failed, declined, cancelled, interrupted (the link
    dropped: resumable)."""
    id: str
    direction: str                  # out | in
    peer: str
    name: str
    size: int
    sha256: str
    mime: str = "application/octet-stream"
    protocol: str = "ohs"           # ohs | yapp
    state: str = "offering"
    reason: str = ""
    done: int = 0                   # bytes the receiver has (ACKed, for a sender)
    sent: int = 0                   # bytes handed to the link (sender)
    start_at: int = 0               # where this run began (a resume's offset)
    compress: bool = False
    wire_bytes: int = 0             # bytes on the air for the data, after compression
    path: Path | None = None        # the source, or where it was saved
    started: float | None = None
    finished: float | None = None
    deadline: float | None = None
    session: object = None          # the OhsSession it runs on
    #: What the inbox keeps a part under: the SHA-256 for OHS; for YAPP,
    #: which has no hash, one made from the sender, name and size.
    key: str = ""
    resumable: bool = False
    _src: object = field(default=None, repr=False)
    _hash: object = field(default=None, repr=False)
    _z: object = field(default=None, repr=False)
    _acked_at: int = 0
    _seq: int = 0

    @property
    def active(self) -> bool:
        return self.state in ("offering", "offered", "sending", "receiving", "checking")

    @property
    def fraction(self) -> float:
        return 1.0 if self.size == 0 else min(1.0, self.done / self.size)

    def rate_bps(self, now: float) -> float | None:
        """Data bits per second this run (file bytes, before compression)."""
        if self.started is None:
            return None
        end = self.finished or now
        if end <= self.started:
            return None
        return (self.done - self.start_at) * 8 / (end - self.started)


class Inbox:
    """Where received files go: the folder, its partial files and their
    notes. `.part` holds the bytes, `.part.json` what they are."""

    def __init__(self, folder: Path | str):
        self.folder = Path(folder)

    def _note_path(self, sha256: str) -> Path:
        return self.folder / ".partial" / f"{sha256}.json"

    def partial(self, sha256: str, size: int) -> tuple[Path, int] | None:
        """The part already held of this file, and how many bytes."""
        try:
            note = json.loads(self._note_path(sha256).read_text("utf-8"))
            part = self.folder / ".partial" / f"{sha256}.part"
            if int(note.get("size", -1)) != size or not part.exists():
                return None
            return part, min(part.stat().st_size, size)
        except (OSError, ValueError):
            return None

    def open_part(self, t: Transfer, offset: int):
        d = self.folder / ".partial"
        d.mkdir(parents=True, exist_ok=True)
        key = t.key or t.sha256
        part = d / f"{key}.part"
        self._note_path(key).write_text(json.dumps({"name": t.name, "size": t.size, "sha256": t.sha256,
                                                    "peer": t.peer, "mime": t.mime, "protocol": t.protocol}),
                                        "utf-8")
        f = open(part, "r+b" if offset and part.exists() else "wb")
        f.truncate(offset)
        f.seek(offset)
        return part, f

    def discard(self, sha256: str) -> None:
        for p in (self.folder / ".partial" / f"{sha256}.part", self._note_path(sha256)):
            try:
                p.unlink()
            except OSError:
                pass

    def finish(self, sha256: str, name: str) -> Path:
        """The checked part, moved into the folder under a safe, free
        name, without execute permission."""
        part = self.folder / ".partial" / f"{sha256}.part"
        dest = unique_path(self.folder, safe_name(name))
        os.replace(part, dest)
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
        try:
            self._note_path(sha256).unlink()
        except OSError:
            pass
        return dest

    def partials(self) -> list[dict]:
        out = []
        for note in sorted((self.folder / ".partial").glob("*.json")):
            try:
                d = json.loads(note.read_text("utf-8"))
                part = note.with_suffix(".part")
                d["have"] = part.stat().st_size if part.exists() else 0
                out.append(d)
            except (OSError, ValueError):
                continue
        return out


@dataclass
class FilePolicy:
    """What the receiving side does without asking."""
    max_size: int = MAX_SIZE
    #: A file this station had already accepted and holds part of is
    #: resumed without asking again.
    auto_resume: bool = True
    #: Offer DEFLATE for file data (still skipped for packed files).
    compress: bool = True


class FileService:
    """File transfer for every session on one engine.

    `attach(conn)` gives a connection its OhsSession (ohssession.py) and
    this service's frame handlers. `on_event(transfer, kind)` hears:
    offer (an incoming offer waits for `accept` or `decline`), started,
    progress, done, failed, declined, cancelled, interrupted, hello (a
    peer said it is an OHS station; transfer is None, the session in
    `detail`).
    """

    def __init__(self, engine, inbox: Inbox | Path | str, *, policy: FilePolicy | None = None,
                 app: str = "", on_event: Callable | None = None):
        self.engine = engine
        self.clock = engine.clock
        self.inbox = inbox if isinstance(inbox, Inbox) else Inbox(inbox)
        self.policy = policy or FilePolicy()
        self.app = app
        self.on_event = on_event
        self.transfers: list[Transfer] = []
        self.sessions: list[oh.OhsSession] = []
        #: Peers to greet on connecting (known OHS stations, by call-SSID).
        self.greet: set[str] = set()
        self.yapp = None                         # yapp.py's handler, if loaded
        engine.services.append(self)

    # -- sessions ------------------------------------------------------------------

    def attach(self, conn) -> oh.OhsSession:
        sess = getattr(conn, "ohs", None)
        if sess is not None:
            return sess
        caps = ("file", "z", "resume") if self.policy.compress else ("file", "resume")
        sess = oh.OhsSession.attach(conn, caps=caps, app=self.app, on_hello=self._hello)
        for kind, fn in ((oh.OFFER, self._offer), (oh.ACCEPT, self._accept), (oh.DECLINE, self._declined),
                         (oh.DATA, self._data), (oh.ACK, self._ack), (oh.DONE, self._done),
                         (oh.RESULT, self._result), (oh.CANCEL, self._cancelled)):
            sess.handlers[kind] = fn
        if self.yapp is not None:
            self.yapp.attach(sess)
        self.sessions.append(sess)
        prev = conn.on_event

        def on_event(event, prev=prev, sess=sess):
            if prev is not None:
                prev(event)
            if event.kind == "disconnected":
                self._lost(sess)
        conn.on_event = on_event
        return sess

    def _hello(self, sess) -> None:
        self.greet.add(_peer(sess))
        self._emit(None, "hello", session=sess)
        # A file waiting for this station from before a drop goes again.
        for t in self.transfers:
            if (t.direction == "out" and t.state == "interrupted" and t.peer == _peer(sess)
                    and self.policy.auto_resume and t.protocol == "ohs"):
                self.resume(t, sess)

    def _lost(self, sess) -> None:
        for t in self.transfers:
            if t.session is sess and t.active:
                self._close_files(t)
                if t.state in ("sending", "receiving", "offering", "checking"):
                    t.resumable = True
                    self._end(t, "interrupted", "link-lost")
                else:
                    self._end(t, "failed", "link-lost")
        if sess in self.sessions:
            self.sessions.remove(sess)

    # -- the engine's service interface ---------------------------------------------------

    def poll(self, now: float) -> None:
        for sess in self.sessions:
            if not sess.hello_sent and sess.conn.connected and _peer(sess) in self.greet:
                sess.hello()
        for t in list(self.transfers):
            if t.deadline is not None and now >= t.deadline:
                t.deadline = None
                if t.state == "offering":
                    self._end(t, "failed", "no-answer" if not t.session.peer_is_ohs else "not-decided")
                elif t.state == "offered":
                    self._send(t, oh.DECLINE, {"id": t.id, "reason": "not-decided"})
                    self._end(t, "declined", "not-decided")
            if t.state == "sending" and t.protocol == "ohs":
                self._pump(t)
        if self.yapp is not None:
            self.yapp.poll(now)

    def next_deadline(self) -> float | None:
        due = [t.deadline for t in self.transfers if t.deadline is not None]
        if self.yapp is not None:
            d = self.yapp.next_deadline()
            if d is not None:
                due.append(d)
        return min(due) if due else None

    # -- sending -----------------------------------------------------------------------

    def send_file(self, conn, path: Path | str, *, name: str | None = None, mime: str | None = None,
                  protocol: str = "ohs", data: bytes | None = None) -> Transfer:
        """Offer a file (or `data`, e.g. a re-encoded picture, under
        `name`) to the station at the other end of `conn`. Raises
        ValueError('encrypted') for an encrypted file and OSError when it
        cannot be read."""
        path = Path(path)
        name = name or path.name
        with self.engine.lock:
            sess = self.attach(conn)
            if data is None:
                with open(path, "rb") as f:
                    head = f.read(65536)
                size = path.stat().st_size
                digest = file_hash(path).hexdigest()
            else:
                head, size, digest = data[:65536], len(data), hashlib.sha256(data).hexdigest()
            if encrypted_reason(head):
                raise ValueError("encrypted")
            t = Transfer(secrets.token_hex(4), "out", _peer(sess), name, size, digest,
                         mime or mimetypes.guess_type(name)[0] or "application/octet-stream", protocol=protocol,
                         path=path, session=sess)
            t._src = data
            t.compress = self.policy.compress and worth_compressing(name, head)
            self.transfers.append(t)
            if protocol == "yapp":
                if self.yapp is None:
                    raise ValueError(_("YAPP is not loaded"))
                self.yapp.send(t, sess)
            else:
                self._offer_out(t, sess)
        self.engine._wake()
        return t

    def _offer_out(self, t: Transfer, sess) -> None:
        t.session, t.state, t.reason = sess, "offering", ""
        t.done = t.sent = 0
        sess.hello()
        sess.send_json(oh.OFFER, {"id": t.id, "name": t.name, "size": t.size, "sha256": t.sha256, "type": t.mime,
                                  "z": t.compress})
        t.deadline = self.clock() + (DECIDE_S if sess.peer_is_ohs else NO_HELLO_S)
        self._emit(t, "started")

    def resume(self, t: Transfer, sess=None) -> bool:
        """Offer an interrupted (or failed) outgoing file again on `sess`,
        or on a connected session to the same station. False when there is
        none."""
        with self.engine.lock:
            if sess is None:
                sess = next((s for s in self.sessions if _peer(s) == t.peer and s.conn.connected), None)
            if sess is None or t.direction != "out" or t.active:
                return False
            t.id = secrets.token_hex(4)
            if t.protocol == "yapp" and self.yapp is not None:
                self.yapp.send(t, sess)
            else:
                self._offer_out(t, sess)
        self.engine._wake()
        return True

    def _accept(self, sess, payload: bytes) -> None:
        d = _json(payload)
        t = self._find(d.get("id"), "out")
        if t is None or t.state != "offering":
            return
        offset = int(d.get("offset", 0))
        if not 0 <= offset <= t.size:
            self._send(t, oh.CANCEL, {"id": t.id, "reason": "bad-offset"})
            self._end(t, "failed", "bad-offset")
            return
        t.deadline = None
        t.state, t.start_at, t.sent, t.done, t._acked_at = "sending", offset, offset, offset, offset
        t.started = self.clock()
        t._z = zlib.compressobj(6, zlib.DEFLATED, -15) if t.compress else None
        if t._src is None:
            try:
                t._src = open(t.path, "rb")
            except OSError:
                self._send(t, oh.CANCEL, {"id": t.id, "reason": "read-failed"})
                self._end(t, "failed", "read-failed")
                return
        self._emit(t, "progress")
        self._pump(t)

    def _read(self, t: Transfer, at: int, n: int) -> bytes:
        if isinstance(t._src, (bytes, bytearray)):
            return bytes(t._src[at:at + n])
        t._src.seek(at)
        return t._src.read(n)

    def _pump(self, t: Transfer) -> None:
        """Hand the link more DATA while it has fewer than two windows of
        I frames outstanding: enough to keep it full, not so much that a
        CANCEL or a chat line waits behind the whole file."""
        conn = t.session.conn
        if not conn.connected or getattr(conn.link, "_closing", False):
            # Closing (the operator pressed Disconnect): the link drains
            # what is queued and goes, and the file stops there, resumable,
            # instead of the disconnect waiting for the whole file.
            return
        k = max(1, int(getattr(conn.link, "k", 4) or 4))
        idb = bytes.fromhex(t.id)
        while t.sent < t.size and conn.pending < 2 * k:
            try:
                raw = self._read(t, t.sent, CHUNK)
            except OSError:
                self._send(t, oh.CANCEL, {"id": t.id, "reason": "read-failed"})
                self._end(t, "failed", "read-failed")
                return
            if not raw:
                break
            flags = 0
            body = raw
            if t._z is not None:
                body = t._z.compress(raw) + t._z.flush(zlib.Z_SYNC_FLUSH)
                flags = 1
            t.session.send_frame(oh.DATA, idb + t.sent.to_bytes(8, "big") + bytes([flags]) + body)
            t.wire_bytes += len(body) + oh.HEADER + 13
            t.sent += len(raw)
        if t.sent >= t.size and t.state == "sending":
            t.state = "checking"
            self._send(t, oh.DONE, {"id": t.id})

    def _ack(self, sess, payload: bytes) -> None:
        d = _json(payload)
        t = self._find(d.get("id"), "out")
        if t is not None and t.active:
            t.done = max(t.done, min(t.size, int(d.get("have", 0))))
            self._emit(t, "progress")

    def _result(self, sess, payload: bytes) -> None:
        d = _json(payload)
        t = self._find(d.get("id"), "out")
        if t is None or not t.active:
            return
        self._close_files(t)
        if d.get("ok"):
            t.done = t.size
            self._end(t, "done")
        else:
            self._end(t, "failed", _reason(d.get("reason"), "hash-mismatch"))

    def _declined(self, sess, payload: bytes) -> None:
        d = _json(payload)
        t = self._find(d.get("id"), "out")
        if t is not None and t.state == "offering":
            self._end(t, "declined", _reason(d.get("reason"), "declined"))

    # -- receiving -----------------------------------------------------------------------

    def _offer(self, sess, payload: bytes) -> None:
        d = _json(payload)
        try:
            t = Transfer(str(d["id"])[:8], "in", _peer(sess), str(d["name"])[:200], int(d["size"]),
                         str(d["sha256"]).lower(), str(d.get("type") or "application/octet-stream")[:80],
                         state="offered", compress=bool(d.get("z")), session=sess)
            bytes.fromhex(t.id)
            if len(t.id) != 8 or not re.fullmatch(r"[0-9a-f]{64}", t.sha256) or t.size < 0:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            sess.send_json(oh.DECLINE, {"id": str(d.get("id", ""))[:8], "reason": "protocol"})
            return
        self.transfers.append(t)
        if t.size > self.policy.max_size:
            self._send(t, oh.DECLINE, {"id": t.id, "reason": "too-big"})
            self._end(t, "declined", "too-big")
            return
        held = self.inbox.partial(t.sha256, t.size)
        t.start_at = held[1] if held else 0
        t.resumable = held is not None
        if held is not None and self.policy.auto_resume:
            self.accept(t)
            return
        t.deadline = self.clock() + DECIDE_S
        self._emit(t, "offer")

    def accept(self, t: Transfer) -> None:
        """Our operator took the file: answer with the offset already held."""
        with self.engine.lock:
            if t.direction != "in" or t.state != "offered":
                return
            if t.protocol == "yapp":
                self.yapp.accept(t)
                return
            held = self.inbox.partial(t.sha256, t.size)
            offset = held[1] if held else 0
            try:
                t.path, t._src = self.inbox.open_part(t, offset)
                t._hash = file_hash(t.path, offset) if offset else hashlib.sha256()
            except OSError:
                self._send(t, oh.DECLINE, {"id": t.id, "reason": "write-failed"})
                self._end(t, "failed", "write-failed")
                return
            t.deadline = None
            t.state, t.start_at, t.done, t._acked_at = "receiving", offset, offset, offset
            t.started = self.clock()
            t._z = zlib.decompressobj(-15)
            self._send(t, oh.ACCEPT, {"id": t.id, "offset": offset})
            self._emit(t, "progress")
        self.engine._wake()

    def decline(self, t: Transfer, reason: str = "declined") -> None:
        with self.engine.lock:
            if t.direction != "in" or t.state != "offered":
                return
            if t.protocol == "yapp":
                self.yapp.decline(t)
            else:
                self._send(t, oh.DECLINE, {"id": t.id, "reason": reason})
            self._end(t, "declined", reason)
        self.engine._wake()

    def _data(self, sess, payload: bytes) -> None:
        if len(payload) < 13:
            return
        t = self._find(payload[:4].hex(), "in")
        if t is None or t.state != "receiving":
            return
        offset = int.from_bytes(payload[4:12], "big")
        flags, body = payload[12], payload[13:]
        if offset != t.done:
            self._fail_in(t, "protocol")
            return
        try:
            raw = t._z.decompress(body, CHUNK * 4) if flags & 1 else body
            if flags & 1 and t._z.unconsumed_tail:
                raise ValueError(_("chunk inflates past its size"))
            if t.done + len(raw) > t.size:
                raise ValueError(_("more data than the offer said"))
            t._src.write(raw)
        except (ValueError, zlib.error):
            self._fail_in(t, "protocol")
            return
        except OSError:
            self._fail_in(t, "write-failed")
            return
        t._hash.update(raw)
        t.done += len(raw)
        t.wire_bytes += len(body) + oh.HEADER + 13
        # Often enough that a small file's sender sees it move (every 5 %,
        # at least 1 KiB apart), at most every 8 KiB on a big one.
        if t.done - t._acked_at >= max(1024, min(ACK_EVERY, t.size // 20)):
            t._acked_at = t.done
            self._send(t, oh.ACK, {"id": t.id, "have": t.done})
        self._emit(t, "progress")

    def _done(self, sess, payload: bytes) -> None:
        d = _json(payload)
        t = self._find(d.get("id"), "in")
        if t is None or t.state != "receiving":
            return
        t.state = "checking"
        try:
            t._src.flush()
        except OSError:
            pass
        self._close_files(t)
        ok = t.done == t.size and t._hash.hexdigest() == t.sha256
        if not ok:
            self.inbox.discard(t.sha256)
            self._send(t, oh.RESULT, {"id": t.id, "ok": False, "reason": "hash-mismatch"})
            self._end(t, "failed", "hash-mismatch")
            return
        try:
            t.path = self.inbox.finish(t.sha256, t.name)
        except OSError:
            self._send(t, oh.RESULT, {"id": t.id, "ok": False, "reason": "write-failed"})
            self._end(t, "failed", "write-failed")
            return
        self._send(t, oh.RESULT, {"id": t.id, "ok": True, "sha256": t.sha256})
        self._end(t, "done")

    def _fail_in(self, t: Transfer, reason: str) -> None:
        self._send(t, oh.CANCEL, {"id": t.id, "reason": reason})
        self._close_files(t)
        if reason == "protocol":
            self.inbox.discard(t.sha256)
        self._end(t, "failed", reason)

    # -- either way ------------------------------------------------------------------------

    def cancel(self, t: Transfer) -> None:
        """Stop a transfer from this end. A receiver keeps what it has, so
        the file can still be resumed if it is offered again."""
        with self.engine.lock:
            if not t.active:
                return
            if t.protocol == "yapp" and self.yapp is not None:
                self.yapp.cancel(t)
            elif t.state == "offered":
                self._send(t, oh.DECLINE, {"id": t.id, "reason": "declined"})
            else:
                self._send(t, oh.CANCEL, {"id": t.id, "reason": "cancelled"})
            self._close_files(t)
            t.resumable = t.direction == "out"
            self._end(t, "cancelled", "cancelled")
        self.engine._wake()

    def _cancelled(self, sess, payload: bytes) -> None:
        d = _json(payload)
        for t in self.transfers:
            if t.id == d.get("id") and t.session is sess and t.active:
                self._close_files(t)
                reason = d.get("reason") or "cancelled"
                self._end(t, "cancelled" if reason == "cancelled" else "failed",
                          "peer-cancelled" if reason == "cancelled" else _reason(reason, "protocol"))
                return

    def _send(self, t: Transfer, kind: int, obj: dict) -> None:
        sess = t.session
        if sess is not None and sess.conn.connected:
            sess.send_json(kind, obj)

    def _find(self, tid, direction: str) -> Transfer | None:
        for t in reversed(self.transfers):
            if t.id == tid and t.direction == direction:
                return t
        return None

    def _close_files(self, t: Transfer) -> None:
        src = t._src
        if src is not None and not isinstance(src, (bytes, bytearray)):
            try:
                src.close()
            except OSError:
                pass
            t._src = None

    def _end(self, t: Transfer, state: str, reason: str = "") -> None:
        t.state, t.reason, t.deadline = state, reason, None
        t.finished = self.clock()
        if t.direction == "out" and state == "done":
            t.resumable = False
        self._emit(t, state)

    def _emit(self, t: Transfer | None, kind: str, **detail) -> None:
        if self.on_event is not None:
            try:
                self.on_event(t, kind, detail)
            except Exception:                           # noqa: BLE001 - the app's handler never stops the station
                log.exception("file transfer event handler failed")


def _peer(sess) -> str:
    r = sess.conn.remote
    return f"{r.call}-{r.ssid}" if r.ssid else r.call


def _json(payload: bytes) -> dict:
    try:
        d = json.loads(payload.decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _reason(value, default: str) -> str:
    return value if isinstance(value, str) and value in REASONS else default
