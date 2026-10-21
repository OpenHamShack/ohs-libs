"""Recording receive audio: 16-bit PCM in, a FLAC file and a JSON sidecar out.

The app hands over the audio its listeners hear: post-demodulation, after
NR2 when NR2 is on (Brian, 12/10/2026). Each app's own docs say where it
taps it.

**Never makes the audio path wait.** `push()` is called on the app's audio
path (an asyncio callback in Icom and Flex, the engine's audio thread in
NB) and only appends the block to a queue; encoding and the disk are this
recorder's own thread. If the disk falls more than QUEUE_S behind, blocks
are dropped and counted (`droppedSeconds`) rather than delaying the radio,
as NB's IQ recorder does.

**The sidecar** (`<id>.json`) says what the recording is without opening
it: the station, the frequency and mode at the start and every change
after (`changes`, seconds into the recording), the sample format, what
processing the audio had (NR2), and how it ended. It is written when the
recording starts, every META_EVERY seconds and at the end, each time whole
and renamed into place, so a crash leaves one that describes what was
written up to the last rewrite. The same facts are in the FLAC file's own
tags, as they were at the start, for a recording that travels without its
sidecar.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import store
from .flac import FlacWriter
from .i18n import _

log = logging.getLogger("shack_rec")

QUEUE_S = 4.0            # seconds of audio the writer may fall behind before blocks are dropped
META_EVERY = 10.0        # s between sidecar rewrites
FORMAT_VERSION = 1


class AudioRecorder:
    """One audio recording. Anything may call `push()`, `note()` and
    `close()`; the writer thread does the rest.

    `meta` is the sidecar's fields beyond those the recorder owns: at least
    `app`, and the receive facts known at the start (`freqHz`, `mode`,
    `station`, `radio`, `processing`...). `max_seconds` and `max_bytes` end
    the recording by themselves."""

    kind = "audio"

    def __init__(self, folder, rid: str, rate: int, channels: int, meta: dict, *, max_seconds: float = 0,
                 max_bytes: int = 0, queue_s: float = QUEUE_S, wall=time.time):
        self.folder = Path(folder)
        self.id = rid
        self.path = self.folder / f"{rid}.flac"
        self.meta_path = self.folder / f"{rid}.json"
        self.rate, self.channels = int(rate), int(channels)
        self.meta = dict(meta)
        self.max_samples = int(max_seconds * self.rate) if max_seconds else 0
        self.max_bytes = int(max_bytes)
        self.cap = int(queue_s * self.rate) * self.channels * 2      # bytes of PCM the queue may hold
        self.wall = wall
        self.cond = threading.Condition()
        self.q: deque = deque()
        self.queued = 0
        self.accepted = 0              # samples a channel taken into the file's timeline
        self.dropped = 0
        self.closing = False
        self.reason = ""
        self.error = ""
        self.t_start = None
        self.t_end = None
        self.changes: list[dict] = []
        self._last_facts: dict = {}
        self._on_close: list = []
        self.done = threading.Event()
        self.writer: FlacWriter | None = None
        self.thread = threading.Thread(target=self._run, name=f"shack-rec-{rid}", daemon=True)

    # ---------------------------------------------------------------- start
    def start(self) -> "AudioRecorder":
        """Open the file here, on the caller's thread, so a folder that
        cannot be written is an answer at once rather than a recording
        that never was."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.t_start = self.wall()
        self.f = open(self.path, "xb")
        tags = {"title": self._title(), "date": store.iso_utc(self.t_start), "frequency": self.meta.get("freqHz"),
                "mode": self.meta.get("mode"), "station": self.meta.get("station"),
                "radio": self.meta.get("radio"), "encoded_by": self.meta.get("appName") or self.meta.get("app")}
        try:
            self.writer = FlacWriter(self.f, self.rate, self.channels, tags)
            self._write_meta()
        except OSError:
            self.f.close()
            self.path.unlink(missing_ok=True)
            raise
        self.note(freqHz=self.meta.get("freqHz"), mode=self.meta.get("mode"))
        self.thread.start()
        return self

    def _title(self) -> str:
        f, m = self.meta.get("freqHz"), self.meta.get("mode")
        return " ".join(p for p in (f"{float(f) / 1e6:.6f} MHz" if f else "", str(m or "").upper()) if p)

    def on_close(self, fn) -> None:
        """Call `fn()` once when the recording starts to close, whoever
        closes it (the app unsubscribes its audio tap there). Called at
        once if it already has."""
        with self.cond:
            if not self.closing:
                self._on_close.append(fn)
                return
        fn()

    # ---------------------------------------------------------------- audio path
    def push(self, pcm) -> bool:
        """Take one block of s16le PCM (bytes, interleaved when stereo) or an
        int16 array. Never blocks and never touches the disk. Returns False
        once the recording has ended: the caller stops calling."""
        if self.closing:
            return False
        raw = pcm.tobytes() if isinstance(pcm, np.ndarray) else bytes(pcm)
        frame = 2 * self.channels
        raw = raw[:len(raw) // frame * frame]
        n = len(raw) // frame
        if not n:
            return True
        hit_limit = False
        with self.cond:
            if self.max_samples and self.accepted + n >= self.max_samples:
                n = self.max_samples - self.accepted
                raw = raw[:n * frame]
                hit_limit = True
            if self.queued + len(raw) > self.cap:
                self.dropped += n
                return True
            self.q.append(raw)
            self.queued += len(raw)
            self.accepted += n
            self.cond.notify()
        if hit_limit:
            self.close(_("the time limit"))
            return False
        return True

    def note(self, **facts) -> None:
        """The receive facts now (freqHz, mode...): each change is kept in
        the sidecar with when it happened. Cheap enough to call on every
        state change or poll."""
        facts = {k: v for k, v in facts.items() if v is not None}
        with self.cond:
            changed = {k: v for k, v in facts.items() if self._last_facts.get(k) != v}
            if not changed:
                return
            self._last_facts.update(changed)
            self.changes.append({"t": round(self.accepted / self.rate, 3), **changed})

    # ---------------------------------------------------------------- any thread
    def close(self, reason: str = "") -> None:
        """End the recording. Returns at once; the writer finishes what is
        queued and writes the final sidecar (wait() for it)."""
        with self.cond:
            if self.closing:
                return
            self.closing = True
            self.reason = reason
            fns, self._on_close = self._on_close, []
            self.cond.notify()
        for fn in fns:
            try:
                fn()
            except Exception:  # noqa: BLE001 - an app's unsubscribe failing must not keep the file open
                log.exception("recording %s: on_close", self.id)

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    @property
    def seconds(self) -> float:
        return self.accepted / self.rate

    @property
    def nbytes(self) -> int:
        w = self.writer
        return w.bytes if w is not None else 0

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "format": "flac", "startUtc": store.iso_utc(self.t_start or 0),
                "seconds": round(self.seconds, 1), "bytes": self.nbytes,
                "maxSeconds": round(self.max_samples / self.rate) if self.max_samples else None,
                "maxBytes": self.max_bytes or None, "droppedSeconds": round(self.dropped / self.rate, 1),
                "freqHz": self._last_facts.get("freqHz"), "mode": self._last_facts.get("mode")}

    # ---------------------------------------------------------------- writer thread
    def _run(self) -> None:
        next_meta = time.monotonic() + META_EVERY
        try:
            while True:
                with self.cond:
                    while not self.q and not self.closing:
                        self.cond.wait(0.5)
                        if time.monotonic() >= next_meta:
                            break
                    raw = self.q.popleft() if self.q else None
                    if raw is None and self.closing:
                        break
                if raw is not None:
                    x = np.frombuffer(raw, "<i2").reshape(-1, self.channels)
                    self.writer.write(x)
                    with self.cond:
                        self.queued -= len(raw)
                    if self.max_bytes and self.writer.bytes >= self.max_bytes:
                        # What is still queued would only take it further
                        # past the size it was given.
                        with self.cond:
                            self.q.clear()
                            self.queued = 0
                        self.close(_("the size limit"))
                if time.monotonic() >= next_meta:
                    next_meta = time.monotonic() + META_EVERY
                    self._write_meta()
        except OSError as e:
            # A full disk, a volume gone read-only: the recording ends with
            # what was written, and says why.
            self.error = _("Recording stopped: could not write {file} ({error})").format(file=self.path.name, error=e)
            log.warning("%s", self.error)
            with self.cond:
                self.closing = True
                self.reason = self.reason or self.error
                self.q.clear()
                self.queued = 0
        finally:
            try:
                if self.writer is not None and not self.error:
                    self.writer.close()
            except OSError as e:
                self.error = self.error or str(e)
            try:
                self.f.close()
            except OSError:
                pass
            self.t_end = self.wall()
            try:
                self._write_meta(final=True)
            except OSError as e:
                log.warning("writing %s failed: %s", self.meta_path, e)
            self.done.set()

    def _write_meta(self, final: bool = False) -> None:
        with self.cond:
            changes = [dict(c) for c in self.changes]
        written = self.writer.samples if self.writer is not None else 0
        doc = {"shackRec": FORMAT_VERSION, "kind": "audio", "format": "flac", "file": self.path.name,
               **self.meta,
               "startUtc": store.iso_utc(self.t_start), "sampleRate": self.rate, "channels": self.channels,
               "sampleFormat": "s16", "seconds": round(written / self.rate, 3),
               "droppedSeconds": round(self.dropped / self.rate, 3), "changes": changes,
               "complete": bool(final), "stopped": (self.reason or _("by the operator")) if final else ""}
        if final:
            doc["endUtc"] = store.iso_utc(self.t_end or self.wall())
        store.write_json(self.meta_path, doc)
