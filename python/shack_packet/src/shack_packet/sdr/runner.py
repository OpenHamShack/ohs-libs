"""The SDR monitor on threads: a reader that only reads, a worker that
decodes, and a queue between them where falling behind becomes a drop.

    runner = Runner(RtlSdrSource(145.3125e6), pipeline)
    runner.start()            # opens the source here: SdrError if it will not
    ...                       # the app reads runner.pipeline.monitor
    runner.stop()

Why two threads: a USB receiver must be read on time or the device's own
buffers overflow, and decoding a burst of traffic on sixteen channels can
take longer than the samples it came in. The reader never waits for the
decoder; the queue holds up to `queue_s` of IQ, and past that a live
source's newest block is thrown away and counted - the way the CW
skimmer's runner does it (shack_cw.skim.runner: put() never blocks; a full
queue drops and counts). The dropped block's samples are simply missing
from the numbering, so the channeliser sees a gap and every later frame is
still dated right. A file or the synthetic band is not live: there the
reader waits for room instead, so nothing is lost by playing faster than
real time.

Errors: a source that fails (SdrError) stops the runner and is handed to
`on_error`; the end of a recording to `on_end`. Both are called on the
runner's threads.
"""

from __future__ import annotations

import collections
import logging
import threading

from .pipeline import Pipeline
from .source import IqSource, SdrError

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, source: IqSource, pipeline: Pipeline, *, queue_s: float = 2.0, on_error=None, on_end=None):
        self.source = source
        self.pipeline = pipeline
        self.limit = int(queue_s * source.rate)
        self.on_error = on_error
        self.on_end = on_end
        self.error: SdrError | None = None
        self.dropped_blocks = 0
        self.dropped_samples = 0
        self.ended = False
        self._q: collections.deque = collections.deque()
        self._queued = 0
        self._cv = threading.Condition()
        self._running = False
        self._halt = False              # stop(): the worker leaves without draining the queue
        self._threads: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self.source.open()
        self._running, self._halt = True, False
        self._threads = [threading.Thread(target=self._read, name="sdr-reader", daemon=True),
                         threading.Thread(target=self._work, name="sdr-worker", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._running, self._halt = False, True
            self._cv.notify_all()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout)
        self.source.close()

    def wait(self, timeout: float | None = None) -> bool:
        """Until the worker has finished (a recording's end, an error or
        stop()); False on timeout."""
        for t in self._threads:
            t.join(timeout)
        return not any(t.is_alive() for t in self._threads)

    def status(self) -> dict:
        st = self.source.status()
        with self._cv:
            st.update({"queuedS": self._queued / self.source.rate, "droppedBlocks": self.dropped_blocks,
                       "droppedSamples": self.dropped_samples, "ended": self.ended,
                       "error": self.error.reason if self.error else None})
        return st

    # -- the threads ------------------------------------------------------------

    def _put(self, item) -> None:
        n = len(item.samples) if item is not None else 0
        with self._cv:
            if item is not None and self._queued + n > self.limit:
                if self.source.live:
                    self.dropped_blocks += 1
                    self.dropped_samples += n
                    return
                while self._running and self._queued + n > self.limit:
                    self._cv.wait(0.5)
            self._q.append(item)
            self._queued += n
            self._cv.notify_all()

    def _read(self) -> None:
        try:
            while self._running:
                block = self.source.read()
                self._put(block)
                if block is None:
                    return
        except SdrError as e:
            self._fail(e)
        except Exception as e:  # noqa: BLE001 - a backend's own exception must still stop the runner cleanly
            log.exception("SDR source failed")
            self._fail(SdrError("read-failed", f"{type(e).__name__}: {e}"))

    def _fail(self, e: SdrError) -> None:
        self.error = e
        with self._cv:
            self._running = False
            self._q.append(None)
            self._cv.notify_all()
        if self.on_error is not None:
            self.on_error(e)

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._q and self._running:
                    self._cv.wait(0.5)
                if not self._q or self._halt:
                    return
                block = self._q.popleft()
                if block is not None:
                    self._queued -= len(block.samples)
                self._cv.notify_all()
            if block is None:
                self.pipeline.flush()
                if self.error is None:
                    self.ended = True
                    if self.on_end is not None:
                        self.on_end()
                return
            self.pipeline.process(block)
            self.pipeline.monitor.source_status = self.status()
