"""YAPP and YAPP-C: binary file transfer the way other packet software does
it (WA7MBL's YAPP; the YAPP-C checksum and resume extension of TPK and
F6FBB's BBS), for stations that do not run OpenHamShack.

Written from F6FBB's yapp.c (LinFBB 7.04j) and the "Yapp-protocols
extension" appendix of the FBB documentation. Not yet tried against
another program on the air or over a link (see FILES.md).

**On the wire**, inside the connected session's byte stream, each frame
is a control byte, a length byte and the rest:

    SI  ENQ 01               sender: I have a file
    RR  ACK 01               receiver: ready
    HD  SOH len name NUL size NUL [date+time NUL]
                             the header; size in ASCII decimal; the 8 hex
                             digits of a DOS date and time mark a sender
                             that can do YAPP-C
    RF  ACK 02               receiver: send it, plain YAPP
    RT  ACK ACK              receiver: send it, YAPP-C (checksums)
    RE  NAK len 'R' NUL have NUL ['C' NUL]
                             receiver: I hold `have` bytes, send the rest
                             (YAPP-C again if 'C')
    NR  NAK len reason       receiver: no
    DT  STX len data [sum]   len 0 = 256 bytes; YAPP-C adds the 8-bit sum
                             of the data bytes
    EF  ETX 01 / AF ACK 03   end of file / acknowledged
    ET  EOT 01 / AT ACK 04   end of transmission / acknowledged
    CN  CAN len reason / CA ACK 05
                             cancel / acknowledged

While a YAPP transfer runs it holds the session's stream (ohssession's
claim): everything the peer sends goes to it until AT, CA or a timeout.

**Receiving**: YAPP has no offer to decline before it starts, so this
station answers SI with RR at once and asks its operator when the header
arrives (name, size, sender); RF/RT/RE or NR answer it. A part held from
an earlier try - kept under the sender, name and size, as YAPP has no
hash - is resumed with RE. Files land in the same inbox as OHS transfers,
under the same rules; their SHA-256 is worked out on arrival, for the
operator's information only (YAPP sends none to check against).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import time

from .filexfer import DECIDE_S, Transfer, file_hash
from .i18n import _

log = logging.getLogger(__name__)

SOH, STX, ETX, EOT, ENQ, ACK, NAK, CAN = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x15, 0x18
DATA_MAX = 250                  # bytes per DT, as F6FBB sends (fits one 256-byte I frame with its header)
INIT_S = 20.0                   # waiting for RR, per try (FBB's figure)
INIT_TRIES = 6
WAIT_S = 120.0                  # any other wait (FBB's figure)
#: After NR, how long to wait for the sender's ET (or another header)
#: before the stream is text again: short, so a chat is not swallowed.
BETWEEN_S = 10.0


def dos_stamp(t: float) -> str:
    """A DOS date and time as YAPP-C's header carries them: 8 hex digits."""
    tm = time.localtime(t)
    date = ((max(1980, tm.tm_year) - 1980) << 9) | (tm.tm_mon << 5) | tm.tm_mday
    clock = (tm.tm_hour << 11) | (tm.tm_min << 5) | (tm.tm_sec // 2)
    return f"{date:04X}{clock:04X}"


def resume_key(peer: str, name: str, size: int) -> str:
    return hashlib.sha256(f"yapp|{peer}|{name}|{size}".encode()).hexdigest()


class _Frames:
    """Splits the claimed stream into YAPP frames. DT's length depends on
    whether YAPP-C is in use (one more byte), so the owner says."""

    def __init__(self):
        self.buf = bytearray()
        self.checksum = False

    def next(self):
        if len(self.buf) < 2:
            return None
        kind, n = self.buf[0], self.buf[1]
        if kind == STX:
            n = n or 256
            need = 2 + n + (1 if self.checksum else 0)
        elif kind in (ENQ, ACK, ETX, EOT):
            need = 2
        elif kind in (SOH, NAK, CAN):
            need = 2 + n
        else:
            # Not a YAPP frame: the peer is not (or no longer) speaking
            # YAPP. One byte is dropped and counted as garbage.
            del self.buf[:1]
            return ("junk", 0, b"")
        if len(self.buf) < need:
            return None
        frame = bytes(self.buf[:need])
        del self.buf[:need]
        return (kind, frame[1], frame[2:])


class _Run:
    """One YAPP transfer, holding a session's stream."""

    def __init__(self, handler, sess, t: Transfer | None):
        self.h = handler
        self.sess = sess
        self.t = t
        self.frames = _Frames()
        self.state = ""
        self.deadline: float | None = None
        self.leftover: bytes | None = None      # set when the claim is let go
        self.tries = 0

    def send(self, data: bytes) -> None:
        if self.sess.conn.connected:
            self.sess.send_raw(data)

    def wait(self, seconds: float) -> None:
        self.deadline = self.h.clock() + seconds

    def release(self) -> None:
        self.leftover = bytes(self.frames.buf)
        self.frames.buf.clear()
        self.deadline = None
        self.h.runs.pop(id(self.sess), None)

    def feed(self, data: bytes):
        """The claim's interface (ohssession.Demux): None while held."""
        self.frames.buf += data
        while self.leftover is None:
            if (self.state == "between" and self.frames.buf
                    and self.frames.buf[0] not in (ENQ, SOH, EOT, CAN)):
                # Between files nothing but another header or the ET may
                # come: anything else is the operator typing again (a
                # station that never sends ET, or a line typed before it
                # arrived), so the stream is text from here.
                self.release()
                break
            if self.state == "eot" and self.frames.buf and self.frames.buf[0] not in (ACK, CAN):
                # The file was acknowledged (AF) and ET sent; text instead
                # of AT is the other operator typing: done, and text again.
                self.h.end(self.t, "done")
                self.release()
                break
            f = self.frames.next()
            if f is None:
                break
            self.frame(*f)
        if self.leftover is None:
            return None
        rest, self.leftover = self.leftover, b""
        return rest

    def cancel_out(self, reason: bytes = b"Cancelled\r") -> None:
        self.send(bytes([CAN, len(reason)]) + reason)
        self.state = "cancel-wait"
        self.wait(10.0)


class _Send(_Run):
    def start(self) -> None:
        t = self.t
        self.sess.demux.claim(self)
        self.state = "init"
        self.tries = 1
        self.send(bytes([ENQ, 1]))
        self.wait(INIT_S)
        t.state, t.reason, t.done, t.sent = "offering", "", 0, 0

    def timeout(self) -> None:
        if self.state == "init" and self.tries < INIT_TRIES:
            self.tries += 1
            self.send(bytes([ENQ, 1]))
            self.wait(INIT_S)
            return
        if self.state != "cancel-wait":
            self.send(bytes([CAN, 8]) + b"Timeout\r")
        if self.state in ("data", "eof"):
            self.t.resumable = True
            self.h.end(self.t, "interrupted", "timeout")
        else:
            self.h.end(self.t, "failed", "no-answer" if self.state == "init" else "protocol")
        self.release()

    def frame(self, kind, n, body) -> None:
        t = self.t
        if kind == CAN:
            self.send(bytes([ACK, 5]))
            self.h.end(t, "cancelled", "peer-cancelled")
            self.release()
        elif kind == ACK and n == 5 and self.state == "cancel-wait":
            self.release()
        elif self.state == "init" and kind == ACK and n == 1:
            name = t.name.encode("latin-1", "replace")[:80]
            body = name + b"\0" + str(t.size).encode() + b"\0" + dos_stamp(self.h.clock_wall()).encode() + b"\0"
            self.send(bytes([SOH, len(body)]) + body)
            self.state = "header"
            self.wait(DECIDE_S)
        elif self.state == "header" and kind == ACK and n in (2, ACK):
            self.frames.checksum = n == ACK
            self.begin(0)
        elif self.state == "header" and kind == NAK:
            parts = body.split(b"\0")
            if body[:2] == b"R\0" and len(parts) >= 2:
                try:
                    have = int(parts[1].strip() or b"0")
                except ValueError:
                    have = -1
                if not 0 <= have <= t.size:
                    self.cancel_out(b"Bad resume\r")
                    self.h.end(t, "failed", "bad-offset")
                    return
                self.frames.checksum = len(parts) > 2 and parts[2] == b"C"
                self.begin(have)
            else:
                self.h.end(t, "declined", "declined")
                self.release()
        elif self.state == "eof" and kind == ACK and n == 3:
            self.send(bytes([EOT, 1]))
            self.state = "eot"
            self.wait(WAIT_S)
        elif self.state == "eot" and kind == ACK and n == 4:
            t.done = t.size
            self.h.end(t, "done")
            self.release()
        # Anything else (a repeated RR, junk) is ignored, as FBB's table does.

    def begin(self, offset: int) -> None:
        t = self.t
        t.state, t.start_at, t.sent, t.done = "sending", offset, offset, offset
        t.started = self.h.clock()
        t.compress = False
        if t._src is None:
            try:
                t._src = open(t.path, "rb")
            except OSError:
                self.cancel_out(b"Read error\r")
                self.h.end(t, "failed", "read-failed")
                return
        self.state = "data"
        self.deadline = None
        self.h.service._emit(t, "progress")
        self.pump()

    def pump(self) -> None:
        t = self.t
        conn = self.sess.conn
        if self.state != "data" or not conn.connected or getattr(conn.link, "_closing", False):
            return                              # closing: as filexfer's pump
        k = max(1, int(getattr(conn.link, "k", 4) or 4))
        while t.sent < t.size and conn.pending < 2 * k:
            try:
                raw = self.h.service._read(t, t.sent, DATA_MAX)
            except OSError:
                self.cancel_out(b"Read error\r")
                self.h.end(t, "failed", "read-failed")
                return
            if not raw:
                break
            frame = bytes([STX, len(raw) & 0xFF]) + raw
            if self.frames.checksum:
                frame += bytes([sum(raw) & 0xFF])
            self.send(frame)
            t.sent += len(raw)
            t.wire_bytes += len(frame)
        # No acknowledgements in YAPP until the end: progress is what the
        # link has had acknowledged, near enough.
        n1 = getattr(conn.link, "n1", 256)
        before = t.done
        t.done = max(t.start_at, min(t.sent, t.sent - conn.pending * n1))
        if t.done != before:
            self.h.service._emit(t, "progress")
        if t.sent >= t.size:
            self.send(bytes([ETX, 1]))
            self.state = "eof"
            self.wait(WAIT_S)


class _Receive(_Run):
    def frame(self, kind, n, body) -> None:
        t = self.t
        if kind == "junk":
            return
        if kind == CAN:
            self.send(bytes([ACK, 5]))
            if t is not None and t.active:
                self.h.service._close_files(t)
                self.h.end(t, "cancelled", "peer-cancelled")
            self.release()
        elif kind == ACK and n == 5 and self.state == "cancel-wait":
            self.release()
        elif kind == ENQ and n == 1 and self.state in ("", "ready", "between"):
            self.send(bytes([ACK, 1]))
            self.state = "ready"
            self.wait(WAIT_S)
        elif kind == SOH and self.state in ("ready", "between"):
            self.header(body)
        elif kind == STX and self.state == "data":
            self.data(n or 256, body)
        elif kind == ETX and self.state == "data":
            self.eof()
        elif kind == EOT and self.state in ("between", "ready"):
            self.send(bytes([ACK, 4]))
            self.release()
        elif kind in (SOH, STX, ETX, EOT, ENQ, NAK):
            self.cancel_out(b"Protocol error\r")
            if t is not None and t.active:
                self.h.service._close_files(t)
                self.h.end(t, "failed", "protocol")

    def header(self, body: bytes) -> None:
        parts = body.split(b"\0")
        name = parts[0].decode("latin-1")[:200] if parts else ""
        try:
            size = int(parts[1].strip()) if len(parts) > 1 else -1
        except ValueError:
            size = -1
        yappc = len(parts) > 2 and len(parts[2]) == 8 and all(c in b"0123456789ABCDEFabcdef" for c in parts[2])
        peer = self.h.peer(self.sess)
        if not name or size < 0:
            self.cancel_out(b"Bad header\r")
            return
        key = resume_key(peer, name, size)
        t = Transfer(key[:8], "in", peer, name, size, "", "application/octet-stream", protocol="yapp",
                     state="offered", session=self.sess, key=key)
        t.mime = mimetypes.guess_type(name)[0] or t.mime
        self.t = t
        self.frames.checksum = yappc
        self.yappc = yappc
        self.h.service.transfers.append(t)
        self.state = "deciding"
        self.wait(DECIDE_S)
        policy = self.h.service.policy
        if size > policy.max_size:
            self.refuse(b"File too big\r")
            self.h.end(t, "declined", "too-big")
            return
        held = self.h.service.inbox.partial(key, size)
        t.start_at = held[1] if held else 0
        t.resumable = held is not None
        if held is not None and policy.auto_resume:
            self.accept()
            return
        self.h.service._emit(t, "offer")

    def refuse(self, reason: bytes) -> None:
        self.send(bytes([NAK, len(reason)]) + reason)
        self.state = "between"
        self.wait(BETWEEN_S)

    def accept(self) -> None:
        t = self.t
        held = self.h.service.inbox.partial(t.key, t.size)
        offset = held[1] if held else 0
        try:
            t.path, t._src = self.h.service.inbox.open_part(t, offset)
            t._hash = file_hash(t.path, offset) if offset else hashlib.sha256()
        except OSError:
            self.refuse(b"Write error\r")
            self.h.end(t, "failed", "write-failed")
            return
        t.state, t.start_at, t.done, t.deadline = "receiving", offset, offset, None
        t.started = self.h.clock()
        if offset:
            body = b"R\0" + str(offset).encode() + b"\0" + (b"C\0" if self.yappc else b"")
            self.send(bytes([NAK, len(body)]) + body)
        else:
            self.send(bytes([ACK, ACK if self.yappc else 2]))
        self.state = "data"
        self.wait(WAIT_S)
        self.h.service._emit(t, "progress")

    def decline(self) -> None:
        self.refuse(b"Declined\r")

    def data(self, n: int, body: bytes) -> None:
        t = self.t
        raw = body[:n]
        if self.frames.checksum and body[n:n + 1] != bytes([sum(raw) & 0xFF]):
            self.cancel_out(b"Checksum error\r")
            self.h.service._close_files(t)
            self.h.end(t, "failed", "protocol")
            return
        if t.done + len(raw) > t.size:
            self.cancel_out(b"Too much data\r")
            self.h.service._close_files(t)
            self.h.end(t, "failed", "protocol")
            return
        try:
            t._src.write(raw)
        except OSError:
            self.cancel_out(b"Write error\r")
            self.h.service._close_files(t)
            self.h.end(t, "failed", "write-failed")
            return
        t._hash.update(raw)
        t.done += len(raw)
        t.wire_bytes += len(body) + 2
        self.wait(WAIT_S)
        self.h.service._emit(t, "progress")

    def eof(self) -> None:
        t = self.t
        self.h.service._close_files(t)
        if t.done != t.size:
            self.cancel_out(b"Short file\r")
            self.h.end(t, "failed", "protocol")
            return
        t.sha256 = t._hash.hexdigest()
        try:
            t.path = self.h.service.inbox.finish(t.key, t.name)
        except OSError:
            self.cancel_out(b"Write error\r")
            self.h.end(t, "failed", "write-failed")
            return
        self.send(bytes([ACK, 3]))
        self.h.end(t, "done")
        self.state = "between"
        self.wait(WAIT_S)

    def timeout(self) -> None:
        t = self.t
        if self.state == "between" and (t is None or not t.active):
            # After a refusal, or a file without the ET that should
            # follow: the stream goes back to text without a word.
            self.release()
            return
        if self.state != "cancel-wait":
            self.send(bytes([CAN, 8]) + b"Timeout\r")
        if t is not None and t.active:
            self.h.service._close_files(t)
            if t.state == "receiving":
                # Silence mid-file is usually a link that is going: what
                # arrived is kept, and the same file sent again resumes.
                t.resumable = True
                self.h.end(t, "interrupted", "timeout")
            else:
                self.h.end(t, "declined" if t.state == "offered" else "failed",
                           "not-decided" if t.state == "offered" else "protocol")
        self.release()


class YappHandler:
    """YAPP for a FileService (filexfer.py): `service.yapp`. Receives
    whenever a peer starts with SI; sends when the app asks for YAPP."""

    def __init__(self, service, wall=time.time):
        self.service = service
        self.clock = service.clock
        self.clock_wall = wall
        self.runs: dict[int, _Run] = {}
        service.yapp = self
        for sess in service.sessions:
            self.attach(sess)

    def attach(self, sess) -> None:
        sess.on_enq = self._incoming

    def peer(self, sess) -> str:
        r = sess.conn.remote
        return f"{r.call}-{r.ssid}" if r.ssid else r.call

    def _incoming(self, sess) -> None:
        if id(sess) in self.runs:
            return
        run = _Receive(self, sess, None)
        self.runs[id(sess)] = run
        sess.demux.claim(run)

    def send(self, t: Transfer, sess) -> None:
        if id(sess) in self.runs:
            raise ValueError(_("a YAPP transfer is already running on this session"))
        t.session = sess
        run = _Send(self, sess, t)
        self.runs[id(sess)] = run
        run.start()
        self.service._emit(t, "started")

    def _run_of(self, t: Transfer) -> _Run | None:
        return next((r for r in self.runs.values() if r.t is t), None)

    def accept(self, t: Transfer) -> None:
        run = self._run_of(t)
        if isinstance(run, _Receive) and run.state == "deciding":
            run.accept()

    def decline(self, t: Transfer) -> None:
        run = self._run_of(t)
        if isinstance(run, _Receive) and run.state == "deciding":
            run.decline()

    def cancel(self, t: Transfer) -> None:
        run = self._run_of(t)
        if run is not None:
            run.cancel_out()

    def end(self, t: Transfer, state: str, reason: str = "") -> None:
        if t is not None and t.active:
            self.service._close_files(t)
            self.service._end(t, state, reason)

    def poll(self, now: float) -> None:
        for run in list(self.runs.values()):
            if not run.sess.conn.connected:
                if run.t is not None and run.t.active:
                    self.service._close_files(run.t)
                    run.t.resumable = True
                    self.service._end(run.t, "interrupted", "link-lost")
                self.runs.pop(id(run.sess), None)
                continue
            if run.deadline is not None and now >= run.deadline:
                run.deadline = None
                run.timeout()
                continue
            if isinstance(run, _Send):
                run.pump()

    def next_deadline(self) -> float | None:
        due = [r.deadline for r in self.runs.values() if r.deadline is not None]
        return min(due) if due else None
