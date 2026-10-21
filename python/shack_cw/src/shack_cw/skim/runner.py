"""Running the skimmer beside a receiver: in a child process (the default)
or a thread (tests, and the fallback when a process cannot start).

Why a process (CW-SKIMMER-PLAN §2.3, Brian 13/10/2026): each decoder's
frame loop is plain Python and holds the GIL - at 64 channels about a
sixth of the time - and the receive thread that feeds the SDR lives in the
app's process. A child process (the `spawn` context, importing numpy and
shack_cw only) takes that off the receive thread entirely. It is not a
server: two queues, no sockets, no ports.

The receive side calls `put()` with each block of the segment's stream.
The queue in is bounded: a full queue drops the block and counts it
(`dropped`), so the skimmer can never stall receive. A thread in the
caller drains the queue out and hands each result to `on_result(kind,
payload)`:

* `("snapshot", dict)` once a second: Skimmer.snapshot() plus `t`;
* `("spots", [spot, ...])` as they are made (cwspot items with `rev`);
* `("status", {"state", "why", "signals", "channels", "cpu", "dropped"})`
  once a second and on every change of state;
* `("spotlist", [...])` in answer to `request_spots()`.

If the worker dies, state goes to `error` with the reason, and it is
started again after 2, 4, 8 ... s, up to 60 s. Nothing it does can stop
receive, and it holds no device.

`country` and `history` cannot be closures here (a spawned process
receives only what pickles): they are given as "module:function" names
with arguments, called in the worker to make the lookup, e.g.
`("ohscore.dxcc:skim_lookup", [data_dir])`.
"""
from __future__ import annotations

import contextlib
import importlib
import multiprocessing as mp
import queue
import sys
import threading
import time
import traceback
from typing import Callable

import numpy as np

QUEUE_BLOCKS = 32         # ~2.4 s of 75 ms blocks
SNAP_S = 1.0
BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)


def _factory(spec):
    """("module:function", [args]) -> function(*args), or None."""
    if not spec:
        return None
    name, args = spec[0], list(spec[1]) if len(spec) > 1 else []
    mod, _, attr = name.partition(":")
    return getattr(importlib.import_module(mod), attr)(*args)


def _work(inq, outq, config: dict) -> None:
    """The worker's loop: blocks in, results out, until ("stop",)."""
    from . import Skimmer
    from .detect import Birdies
    try:
        country = _factory(config.get("country"))
        history = _factory(config.get("history"))
    except Exception as exc:          # a lookup that cannot load: shape-only checks, and say so
        country = history = None
        outq.put(("warning", f"no country lookup: {exc}"))
    sk = None
    seg = None
    last_snap = 0.0
    cpu0, wall0 = time.process_time(), time.monotonic()
    cpu_pct = 0.0
    while True:
        try:
            msg = inq.get(timeout=0.5)
        except queue.Empty:
            msg = None
        now = time.monotonic()
        if msg is not None:
            kind = msg[0]
            if kind == "stop":
                return
            if kind == "config":
                config.update(msg[1])
                if sk is not None:
                    sk.configure(max_channels=config.get("max_channels"), squelch_db=config.get("squelch_db"))
            elif kind == "crash":          # tests: prove the parent survives a worker that dies
                raise SystemExit(3)
            elif kind == "spots":
                outq.put(("spotlist", sk.spots() if sk is not None else []))
            elif kind == "block":
                _, gen, t, centre, rate, lo, hi, band, dc, z = msg
                if sk is None or seg != (rate, lo, hi, band):
                    if sk is None or seg[0] != rate:
                        bird = config.get("birdies")
                        sk = Skimmer(rate, lo, hi, centre, max_channels=config.get("max_channels", 64),
                                     squelch_db=config.get("squelch_db", 6.0), country=country,
                                     history=history, birdies=Birdies(bird) if bird else None, band=band,
                                     pitch=config.get("pitch", 700.0), dc=dc,
                                     engine=config.get("engine", "both"))
                    else:
                        sk.retune(centre=centre, lo=lo, hi=hi, band=band, dc=dc)
                    seg = (rate, lo, hi, band)
                spots = sk.feed(z, t=t, centre=centre, generation=gen)
                if spots:
                    outq.put(("spots", spots))
        if sk is not None and now - last_snap >= SNAP_S:
            last_snap = now
            c, w = time.process_time(), time.monotonic()
            if w - wall0 > 0:
                cpu_pct = 100.0 * (c - cpu0) / (w - wall0)
            cpu0, wall0 = c, w
            snap = sk.snapshot()
            snap["t"] = sk._t
            outq.put(("snapshot", snap))
            outq.put(("stats", {"signals": sk.signals, "channels": sk.decoding, "cpu": round(cpu_pct, 1)}))


_MAIN_LOCK = threading.Lock()


@contextlib.contextmanager
def _main_hidden():
    """Start a spawned worker without the app's own main module.

    `spawn` re-imports the parent's __main__ in the child (as __mp_main__),
    so that pickled functions defined there can be found. For an app run
    as `python3 -m ohsradio --server` that is the whole app - Qt, the radio,
    the transmit path - in a process meant to import numpy and shack_cw
    only. The worker's function lives here, not in __main__, so the child
    has no need of it: while the process starts, __main__ is made to look
    like an interactive session (no __spec__, no __file__), which is the
    case multiprocessing already handles by importing nothing. Put back at
    once; one start at a time."""
    main = sys.modules.get("__main__")
    with _MAIN_LOCK:
        saved = {k: main.__dict__[k] for k in ("__spec__", "__file__") if main is not None and k in main.__dict__}
        try:
            if main is not None:
                main.__dict__["__spec__"] = None
                main.__dict__.pop("__file__", None)
            yield
        finally:
            if main is not None:
                main.__dict__.update(saved)
                if "__spec__" not in saved:
                    main.__dict__.pop("__spec__", None)


def _work_guarded(inq, outq, config):
    try:
        _work(inq, outq, config)
    except SystemExit:
        raise
    except BaseException as exc:
        outq.put(("died", f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=6)))
        raise


class SkimRunner:
    """See the module. `mode`: "process" or "thread"."""

    def __init__(self, on_result: Callable[[str, object], None], *, mode: str = "process",
                 max_channels: int = 64, squelch_db: float = 6.0, pitch: float = 700.0, country=None,
                 history=None, birdies: str | None = None, engine: str = "both"):
        self.on_result = on_result
        self.mode = mode
        # engine: the CW engines behind every channel (Skimmer.ENGINES),
        # fixed for the worker's life, as its channels are built with it.
        self.config = {"max_channels": max_channels, "squelch_db": squelch_db, "pitch": pitch,
                       "country": country, "history": history, "birdies": birdies, "engine": engine}
        self.dropped = 0
        self.state = "off"
        self.why = ""
        self.stats = {"signals": 0, "channels": 0, "cpu": 0.0}
        self.restarts = 0
        self._lock = threading.Lock()
        self._worker = None
        self._inq = self._outq = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self._lock:
            if self.state not in ("off", "error"):
                return
            self._stop.clear()
            self._spawn()
            t = threading.Thread(target=self._drain, name="cwskim-results", daemon=True)
            t.start()
            self._threads = [t]

    def _spawn(self) -> None:
        if self.mode == "process":
            ctx = mp.get_context("spawn")
            self._inq, self._outq = ctx.Queue(QUEUE_BLOCKS), ctx.Queue()
            self._worker = ctx.Process(target=_work_guarded, args=(self._inq, self._outq, dict(self.config)),
                                       name="cwskim", daemon=True)
            with _main_hidden():
                self._worker.start()
            self._set_state("running", "")
            return
        else:
            self._inq, self._outq = queue.Queue(QUEUE_BLOCKS), queue.Queue()
            self._worker = threading.Thread(target=self._thread_main, args=(self._inq, self._outq),
                                            name="cwskim", daemon=True)
        self._worker.start()
        self._set_state("running", "")

    def _thread_main(self, inq, outq):
        try:
            _work_guarded(inq, outq, dict(self.config))
        except BaseException:
            pass   # reported through outq ("died"); the supervisor restarts it

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            w, inq = self._worker, self._inq
            if inq is not None:
                try:
                    inq.put_nowait(("stop",))
                except Exception:
                    pass
            if w is not None:
                w.join(timeout=3)
                if self.mode == "process" and w.is_alive():
                    w.terminate()
                    w.join(timeout=2)
            self._worker = None
            self._set_state("off", "")
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []

    def alive(self) -> bool:
        w = self._worker
        return w is not None and w.is_alive()

    # -------------------------------------------------------------- traffic
    def put(self, z: np.ndarray, *, t: float, centre: float, rate: float, lo: float, hi: float,
            band: str | None = None, generation=0, dc: float | None = None) -> bool:
        """Hand one block of the segment to the worker; False (and counted)
        if the queue is full or the worker is not running. Never blocks."""
        inq = self._inq
        if inq is None or self.state != "running":
            return False
        try:
            inq.put_nowait(("block", generation, float(t), float(centre), float(rate), float(lo), float(hi), band,
                            dc, np.ascontiguousarray(z, np.complex64)))
            return True
        except (queue.Full, ValueError, OSError):
            self.dropped += 1
            return False

    def configure(self, **settings) -> None:
        self.config.update(settings)
        if self._inq is not None:
            try:
                self._inq.put_nowait(("config", settings))
            except Exception:
                pass

    def request_spots(self) -> None:
        """Ask for the spot list ("spotlist" result). Not called on the
        receive thread, so it may wait a moment for room in the queue."""
        if self._inq is not None:
            try:
                self._inq.put(("spots",), timeout=2.0)
            except Exception:
                pass

    def crash_for_test(self) -> None:
        self._inq.put(("crash",))

    # -------------------------------------------------------------- results
    def _set_state(self, state: str, why: str) -> None:
        changed = (state, why) != (self.state, self.why)
        self.state, self.why = state, why
        if changed:
            self._emit_status()

    def _emit_status(self) -> None:
        try:
            self.on_result("status", dict(self.stats, state=self.state, why=self.why, dropped=self.dropped))
        except Exception:
            pass

    def _drain(self) -> None:
        backoff = 0
        next_try = None
        while not self._stop.is_set():
            outq = self._outq
            try:
                msg = outq.get(timeout=0.5) if outq is not None else None
            except (queue.Empty, EOFError, OSError):
                msg = None
            if msg is not None:
                kind = msg[0]
                if kind == "died":
                    self.why = msg[1]
                elif kind == "stats":
                    self.stats = msg[1]
                    self._emit_status()
                elif kind == "warning":
                    self.on_result("warning", msg[1])
                else:
                    try:
                        self.on_result(kind, msg[1])
                    except Exception:
                        pass   # a consumer's error is its own; the skimmer goes on
                    if kind == "snapshot":
                        backoff = 0
                continue
            if self._stop.is_set():
                break
            # Supervision: a worker that has died is restarted, with backoff.
            with self._lock:
                if self._worker is not None and not self._worker.is_alive() and self.state == "running":
                    code = getattr(self._worker, "exitcode", None)
                    reason = self.why or (f"the skimmer stopped (exit code {code})" if code is not None
                                          else "the skimmer stopped")
                    self._set_state("error", reason)
                    next_try = time.monotonic() + BACKOFF[min(backoff, len(BACKOFF) - 1)]
                    backoff += 1
                if self.state == "error" and next_try is not None and time.monotonic() >= next_try \
                        and not self._stop.is_set():
                    next_try = None
                    self.restarts += 1
                    self.why = ""
                    self._spawn()
