"""The decoder host's Python face (RADIO-SDR-PLAN §3.5, §4.2).

Rust decoders run in the native host (shack_sdr._native.Host): its own
threads, no GIL, a bounded ring per session, results on a queue that
`poll` drains. Python-backed decoders (catalogue.py) run on one Python
thread of this host's, fed through bounded queues of their own, so `feed`
never blocks for them either; their results come out of the same `poll`,
numbered the same way.

Every session's results carry `session`, `rev` (rising by one per result,
per session, so a gap says something was missed) and `t` (UTC seconds,
from the samples, never the clock), and a `type`: text, frame, image,
meta, or state for a session's state changes.

A file a decoder makes (an SSTV picture's PNG) is not a result either:
the host keeps it, and writes it to `files_dir` when given one. The
`image` result whose `event` is "done" names it (`file`, and `path` when
written); `file(name)` gives its bytes, from memory or the folder, which
is what an app's `api/decoders/files?id=` serves, and `files()` lists
them. Names are the session's id and the decoder's, letters, digits and
`_-.` only: anything else is refused, so a client's id cannot leave the
folder.

Sound is not a result. A decoder that makes it (DAB now; WFM, FreeDV and
the digital voice modes later) fills the session's audio ring, and the app
pulls it with `audio(sid, rate=12_000, channels=1)` at its own pace,
converted to its own format: Radio routes it to its ordinary audio stream.
"""

from __future__ import annotations

import collections
import itertools
import os
import threading
import time

import numpy as np

from . import _native
from .catalogue import entry as _entry, runner as _runner
from ._native import DecoderError

#: Seconds of samples a Python-backed session holds before dropping the oldest.
PY_QUEUE_S = 2.0


class _PySession:
    def __init__(self, sid: str, decoder: str, runner, input_kind: str, rate: float, params: dict, pinned: bool):
        self.id, self.decoder, self.runner = sid, decoder, runner
        self.input, self.rate, self.params, self.pinned = input_kind, rate, dict(params), pinned
        self.queue: collections.deque = collections.deque()
        self.queued = 0
        self.fed = self.dropped = 0
        self.rev = 0
        self.state, self.why, self.since = "starting", None, time.time()
        self.lock = threading.Lock()


class Host:
    """Decoder sessions. `threads` workers (1-4) for the Rust decoders;
    `cpu_max` the share of one core they may use together (0 = no cap)."""

    def __init__(self, threads: int = 1, cpu_max: float = 0.25, *, ring_s: float = 2.0, max_sessions: int = 17,
                 testing: bool = False, files_dir: str | os.PathLike | None = None, files_keep: int = 16):
        self._native = _native.Host(threads, cpu_max, ring_s, max_sessions, testing,
                                    None if files_dir is None else os.fspath(files_dir), files_keep)
        self._max = max_sessions
        self._py: dict[str, _PySession] = {}
        self._results: collections.deque = collections.deque(maxlen=20_000)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._ids = itertools.count(1)
        self._thread: threading.Thread | None = None

    # -- sessions --------------------------------------------------------------

    def start(self, decoder: str, params: dict | None = None, input: str = "iq", rate: float = 12_000.0, *,
              center_hz: float = 0.0, pinned: bool = False) -> str:
        """A new session; its id. `input` is "iq" or "audio"; `center_hz` is
        where in audio fed to an IQ decoder the signal sits; `pinned` means
        the CPU budget never pauses it (the mode decoder)."""
        params = dict(params or {})
        entry = _entry(decoder)
        if entry is not None and entry["engine"] != "shack_sdr":
            return self._start_python(entry, params, input, rate, center_hz, pinned)
        if entry is not None and entry.get("needs"):
            # The decoder opens its codec itself, from the file the loader's
            # probe passed; tell the native side which file that is.
            from . import codecs
            codecs.publish()
        return self._native.start(decoder, params, input, float(rate), float(center_hz), pinned)

    def configure(self, sid: str, params: dict) -> None:
        """Change a running session's parameters, live (DAB's `service`).
        Checked at once: a refusal raises DecoderError("bad_params", why)."""
        s = self._py.get(sid)
        if s is None:
            self._native.configure(sid, dict(params))
            return
        raise DecoderError("bad_params", "not_live")

    def audio(self, sid: str, max_s: float = 1.0, *, rate: int | None = None,
              channels: int | None = None) -> list[dict]:
        """The session's sound since the last pull: [{"t", "rate", "channels",
        "pcm"}], `pcm` a float32 numpy array (interleaved when stereo), each
        dict one run of one format; at most `max_s` seconds. `rate` and
        `channels` convert it (Radio: 12_000, 1), keeping the converter's
        state from one pull to the next so the sound is unbroken. [] when
        there is none, or for a decoder that makes no sound; never waits."""
        if sid in self._py:
            return []
        out = self._native.audio(sid, float(max_s), rate, channels)
        for c in out:
            c["pcm"] = np.frombuffer(c["pcm"], dtype=np.float32)
        return out

    def file(self, name: str) -> bytes | None:
        """A decoder's file by name (an `image` "done" result's `file`):
        its bytes, or None for a name not known or not allowed."""
        return self._native.file(name)

    def files(self) -> list[dict]:
        """[{"name", "session", "decoder", "t", "size", "path"}], oldest
        first: those made since this host started, after any older ones
        found in `files_dir`."""
        return self._native.files()

    def _start_python(self, entry: dict, params: dict, input_kind: str, rate: float, center_hz: float,
                      pinned: bool) -> str:
        if self._closed:
            raise DecoderError("closed", None)
        if not entry["available"]:
            raise DecoderError("decoder_unavailable", entry["why"])
        run = _runner(entry["id"])
        if run is None:
            raise DecoderError("decoder_unavailable", "mode_only")
        if input_kind not in entry["inputs"]:
            raise DecoderError("input_unsupported", None)
        if len(self._native.sessions()) + len(self._py) >= self._max:
            raise DecoderError("too_many_sessions", None)
        try:
            runner = run(params, float(rate), float(center_hz))
        except (TypeError, ValueError) as e:
            raise DecoderError("bad_params", str(e)) from None
        sid = f"dp_{next(self._ids):04x}"
        with self._lock:
            self._py[sid] = _PySession(sid, entry["id"], runner, input_kind, float(rate), params, pinned)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run_python, name="shack-sdr-py", daemon=True)
                self._thread.start()
        return sid

    def feed(self, sid: str, samples, t0: float, index: int) -> None:
        """Hand a session its next block: numpy complex64 for IQ, float32 for
        audio (other dtypes are converted). Copies and returns at once."""
        a = np.asarray(samples)
        s = self._py.get(sid)
        if s is not None:
            self._feed_python(s, a, t0, index)
            return
        if np.iscomplexobj(a):
            z = np.ascontiguousarray(a, dtype=np.complex64)
            self._native.feed_iq(sid, z.view(np.float32), float(t0), int(index))
        else:
            self._native.feed_audio(sid, np.ascontiguousarray(a, dtype=np.float32), float(t0), int(index))

    def _feed_python(self, s: _PySession, a: np.ndarray, t0: float, index: int) -> None:
        if np.iscomplexobj(a) != (s.input == "iq"):
            raise DecoderError("bad_input", "input_kind")
        if s.state in ("error", "stopped", "outside"):
            return
        block = np.array(a, dtype=np.complex64 if s.input == "iq" else np.float32)
        with s.lock:
            s.queue.append((block, float(t0), int(index)))
            s.queued += len(block)
            s.fed += len(block)
            while s.queued > PY_QUEUE_S * s.rate and len(s.queue) > 1:
                old = s.queue.popleft()[0]
                s.queued -= len(old)
                s.dropped += len(old)
        self._wake.set()

    def _run_python(self) -> None:
        while not self._closed:
            self._wake.wait(0.1)
            self._wake.clear()
            for s in list(self._py.values()):
                while True:
                    with s.lock:
                        if not s.queue:
                            break
                        block, t0, index = s.queue.popleft()
                        s.queued -= len(block)
                    try:
                        s.runner.feed(block, s.rate, t0, index)
                        out = s.runner.poll()
                    except Exception:   # noqa: BLE001 - one session's failure, as a Rust panic is
                        self._state(s, "error", "exception")
                        break
                    if s.state == "starting":
                        self._state(s, "running", None)
                    self._emit(s, out)

    def _emit(self, s: _PySession, items: list[dict]) -> None:
        with self._lock:
            for it in items:
                s.rev += 1
                self._results.append({"session": s.id, "rev": s.rev, **it})

    def _state(self, s: _PySession, state: str, why: str | None) -> None:
        if (s.state, s.why) == (state, why):
            return
        s.state, s.why, s.since = state, why, time.time()
        self._emit(s, [{"t": s.since, "type": "state", "state": state, "why": why}])

    def poll(self, max_items: int = 500) -> list[dict]:
        """Up to `max_items` results, oldest first; never blocks."""
        out = self._native.poll(max_items)
        with self._lock:
            while self._results and len(out) < max_items:
                out.append(self._results.popleft())
        return out

    def status(self, sid: str) -> dict:
        s = self._py.get(sid)
        if s is None:
            return self._native.status(sid)
        st = {}
        try:
            st = dict(s.runner.status())
        except Exception:   # noqa: BLE001 - status of a failed runner is its error state
            pass
        st.update({"fed": s.fed, "dropped": s.dropped})
        return {"id": s.id, "decoder": s.decoder, "input": s.input, "rate": s.rate, "params": dict(s.params),
                "pinned": s.pinned, "state": s.state, "why": s.why, "since": s.since, "rev": s.rev, "stats": st}

    def stats(self) -> dict:
        st = self._native.stats()
        st["sessions"] += len(self._py)
        return st

    def sessions(self) -> list[str]:
        return self._native.sessions() + list(self._py)

    def reset(self, sid: str) -> None:
        s = self._py.get(sid)
        if s is None:
            self._native.reset(sid)
            return
        with s.lock:
            s.queue.clear()
            s.queued = 0
        s.runner.reset()

    def set_outside(self, sid: str, outside: bool) -> None:
        """The session's frequency left (True) or came back into the span."""
        s = self._py.get(sid)
        if s is None:
            self._native.set_outside(sid, outside)
            return
        if outside and s.state not in ("error", "stopped"):
            with s.lock:
                s.queue.clear()
                s.queued = 0
            self._state(s, "outside", "outside_span")
        elif not outside and s.state == "outside":
            s.runner.reset()
            self._state(s, "starting", None)

    def stop(self, sid: str) -> None:
        s = self._py.pop(sid, None)
        if s is None:
            self._native.stop(sid)
            return
        self._state(s, "stopped", None)
        try:
            s.runner.close()
        except Exception:   # noqa: BLE001 - stopping must not fail on a broken runner
            pass

    def close(self) -> bool:
        """Stop everything; True if every worker joined within a second."""
        self._closed = True
        self._wake.set()
        joined = self._native.close()
        if self._thread is not None:
            self._thread.join(1.0)
            joined = joined and not self._thread.is_alive()
        for sid in list(self._py):
            self.stop(sid)
        return joined

    def __enter__(self) -> Host:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
