"""What an app holds: its recordings folder, the recordings running now, and
the limits, behind the calls its API answers (start, stop, status, list,
file, delete).

Each app does only what is its own: where the audio is tapped (and
unsubscribing it, through `on_close`), the receive facts for the
metadata, and its HTTP routes. The rules are here, once:

- one recording of each kind at a time (audio, and IQ where the app has
  it), and both kinds may run together;
- before a start: the oldest recordings pruned to the disk-use limit, then
  the free-space floor checked (`507 low_disk` below it);
- while running, every GUARD_EVERY seconds: the oldest pruned again (never
  a running one), and a recording stopped if free space falls below the
  floor;
- each recording stops by itself at its kind's longest duration, and at
  the disk-use limit or the free space above the floor, whichever is less.

**Settings.** Each is a module setting (`env:` in the manifest; a setting
cannot hold a line break, so each is one number):

| Key | Default | |
|---|---|---|
| `RECORDINGS_LIMIT_GB` | the app's (5 for audio-only apps) | the folder's most; past it the oldest go |
| `RECORDINGS_MIN_FREE_GB` | 2 | free space below which nothing records |
| `RECORDINGS_AUDIO_MAX_HOURS` | 4 | the longest audio recording |
| `RECORDINGS_IQ_MAX_MINUTES` | 10 | the longest IQ recording (gigabytes: see the app's docs) |

A value that is not a positive number is ignored with a log line and the
default used, so a typo never turns a limit off.

**An IQ recording is the app's own** (NB's engine writes SigMF), so the app
`adopt()`s a handle for it: anything with `id`, `kind`, `nbytes`,
`status()`, `close(reason)` and a `done` Event.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time

from .audio import AudioRecorder
from .i18n import _
from .store import GB, Refused, Store, size_text

log = logging.getLogger("shack_rec")

GUARD_EVERY = 2.0
KINDS = ("audio", "iq")


class Limits:
    ENV = {"limit_bytes": ("RECORDINGS_LIMIT_GB", GB), "floor_bytes": ("RECORDINGS_MIN_FREE_GB", GB),
           "audio_max_s": ("RECORDINGS_AUDIO_MAX_HOURS", 3600.0), "iq_max_s": ("RECORDINGS_IQ_MAX_MINUTES", 60.0)}

    def __init__(self, limit_bytes: float = 5 * GB, floor_bytes: float = 2 * GB, audio_max_s: float = 4 * 3600,
                 iq_max_s: float = 600):
        self.limit_bytes = int(limit_bytes)
        self.floor_bytes = int(floor_bytes)
        self.audio_max_s = float(audio_max_s)
        self.iq_max_s = float(iq_max_s)

    @classmethod
    def from_env(cls, env, **defaults) -> "Limits":
        """Limits from module settings, over the app's defaults (in Limits'
        own units: bytes and seconds)."""
        lim = cls(**defaults)
        for attr, (key, unit) in cls.ENV.items():
            raw = str(env.get(key, "") or "").strip()
            if not raw:
                continue
            try:
                v = float(raw)
                if not v > 0 or v != v or v == float("inf"):
                    raise ValueError
            except ValueError:
                log.warning("%s=%r is not a positive number; using %s", key, raw, getattr(lim, attr))
                continue
            setattr(lim, attr, type(getattr(lim, attr))(v * unit))
        return lim

    def public(self) -> dict:
        return {"limitBytes": self.limit_bytes, "floorBytes": self.floor_bytes,
                "audioMaxSeconds": self.audio_max_s, "iqMaxSeconds": self.iq_max_s}


class Recordings:
    def __init__(self, folder, limits: Limits, *, app: str, kinds=("audio",), disk_usage=shutil.disk_usage,
                 wall=time.time, guard_every: float = GUARD_EVERY):
        self.limits = limits
        self.app = app
        self.kinds = tuple(k for k in KINDS if k in kinds)
        self.store = Store(folder, limits.limit_bytes, limits.floor_bytes, disk_usage)
        self.wall = wall
        self.guard_every = guard_every
        self.lock = threading.RLock()
        self.active: dict[str, object] = {}
        self.last: dict | None = None             # the last recording that ended: {id, kind, stopped, error}
        self._guard: threading.Thread | None = None
        self._wake = threading.Event()

    # ---------------------------------------------------------------- starting
    def prepare(self, kind: str) -> dict:
        """Everything that may refuse a start, before anything is opened:
        the kind, one at a time, then room (prune, then the floor). Returns
        the recording's budget: {"max_seconds", "max_bytes"}."""
        if kind not in KINDS:
            raise Refused(400, "bad_kind", _("kind must be audio or iq"))
        if kind not in self.kinds:
            raise Refused(400, "kind_unavailable", _("This app has no {kind} to record.").format(
                kind=_("IQ") if kind == "iq" else _("audio")))
        with self.lock:
            self._reap()
            if kind in self.active:
                raise Refused(409, "already_recording", _("Already recording."))
            keep = {h.id for h in self.active.values()}
        self.store.folder.mkdir(parents=True, exist_ok=True)
        self.store.prune(keep)
        self.store.check_room()
        room = self.store.free() - self.store.floor_bytes
        return {"max_seconds": self.limits.iq_max_s if kind == "iq" else self.limits.audio_max_s,
                "max_bytes": max(1, min(self.limits.limit_bytes, room))}

    def new_id(self, freq_hz=None, mode=None) -> str:
        return self.store.new_id(self.app, self.wall(), freq_hz, mode)

    def start_audio(self, rate: int, channels: int, meta: dict) -> AudioRecorder:
        """Start recording audio; the app then feeds `push()` and passes its
        unsubscribe to `on_close()`. `meta` holds the receive facts
        (`freqHz`, `mode`, `station`, `radio`, `processing`, `appName`...)."""
        budget = self.prepare("audio")
        rid = self.new_id(meta.get("freqHz"), meta.get("mode"))
        try:
            rec = AudioRecorder(self.store.folder, rid, rate, channels, {"app": self.app, **meta},
                                max_seconds=budget["max_seconds"], max_bytes=budget["max_bytes"],
                                wall=self.wall).start()
        except OSError as e:
            raise Refused(500, "cannot_write", _("Could not record to {folder}: {error}").format(
                folder=self.store.folder, error=e)) from e
        self.adopt("audio", rec)
        log.info("recording audio to %s", rec.path)
        return rec

    def adopt(self, kind: str, handle) -> None:
        """Hold a recording the app started itself (`prepare()` first)."""
        with self.lock:
            self.active[kind] = handle
            if self._guard is None or not self._guard.is_alive():
                self._guard = threading.Thread(target=self._guard_loop, name="shack-rec-guard", daemon=True)
                self._guard.start()

    # ---------------------------------------------------------------- stopping
    def stop(self, kind: str | None = None, reason: str = "", wait: float = 0.0) -> list[dict]:
        """Stop the recording of `kind`, or every one; returns their status
        as they stopped. `wait` seconds for the files to be finished (a
        thread may wait; an event loop must not)."""
        with self.lock:
            handles = [h for k, h in self.active.items() if kind in (None, k)]
        out = []
        for h in handles:
            h.close(reason)
        for h in handles:
            if wait:
                h.done.wait(wait)
            out.append(h.status())
        with self.lock:
            self._reap()
        return out

    def _reap(self) -> None:
        """Forget recordings whose files are finished (under the lock), and
        bring the folder back within its limit now that one has ended: the
        guard stops with the last recording, and a folder left over its
        limit until the next start would read as a limit not kept."""
        ended = False
        for kind, h in list(self.active.items()):
            if h.done.is_set():
                del self.active[kind]
                ended = True
                self.last = {"id": h.id, "kind": kind, "stopped": getattr(h, "reason", "") or _("by the operator"),
                             "error": getattr(h, "error", "") or ""}
        if ended:
            try:
                self.store.prune({h.id for h in self.active.values()} | {self.last["id"]})
            except OSError as e:
                log.warning("recordings: pruning failed: %s", e)

    def _guard_loop(self) -> None:
        while True:
            self._wake.wait(self.guard_every)
            self._wake.clear()
            with self.lock:
                self._reap()
                handles = list(self.active.values())
                if not handles:
                    self._guard = None
                    return
            try:
                self.store.prune({h.id for h in handles})
                free = self.store.free()
            except OSError as e:
                log.warning("recordings guard: %s", e)
                continue
            if free < self.store.floor_bytes:
                why = _("free disk space fell below {floor}").format(floor=size_text(self.store.floor_bytes))
                log.warning("recordings: %s; stopping", why)
                for h in handles:
                    h.close(why)

    # ---------------------------------------------------------------- reading
    def status(self) -> dict:
        with self.lock:
            self._reap()
            active = [h.status() for h in self.active.values()]
            last = dict(self.last) if self.last else None
        return {"recording": bool(active), "active": active, "kinds": list(self.kinds), "last": last,
                "usage": self.store.usage(), "limits": self.limits.public()}

    def listing(self) -> dict:
        with self.lock:
            self._reap()
            running = {h.id for h in self.active.values()}
        items = self.store.list()
        for e in items:
            e["recording"] = e["id"] in running
        return {"recordings": items, **self.status()}

    def file(self, rid: str, part: str = "data"):
        if part not in ("data", "meta"):
            raise Refused(400, "bad_part", _("part must be data or meta"))
        return self.store.file(rid, part)

    def delete(self, rid: str) -> dict:
        with self.lock:
            self._reap()
            if any(h.id == rid for h in self.active.values()):
                raise Refused(409, "recording_active", _("Stop that recording before deleting it."))
            return self.store.delete(rid)
