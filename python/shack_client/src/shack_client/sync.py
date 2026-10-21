"""The sync round and its triggers (SYNC.md §6.3, §7).

A round: take the store's sync lease; measure the controller's clock
(once per pairing, then hourly); push dirty records and unacknowledged
ops; pull until `more` is false, merging each record; store the cursor;
release the lease. Nothing in a UI path waits for any of it: `now()` asks
for a round and returns at once, and the background thread does the work.

Failures back off 1 s, 2 s, 4 s ... to 5 min; a controller that is away
leaves the app logging exactly as it did standalone (§7 "the controller
disappears").
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from . import hlc as H
from .transport import Offline

DEBOUNCE_S = 2.0
PERIOD_S = 60.0
BACKOFF_MAX_S = 300.0
OFFSET_EVERY_S = 3600.0
MAX_OFFSET_ERROR_MS = 2000


class SyncClient:
    def __init__(self, station):
        self.st = station
        self.holder = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.state = "unpaired"
        self.error = ""
        self.last_sync_at = ""
        self.profile_rejected = False
        self.page_limit = 500
        self.hooks: dict = {}           # tests: "after_pull_page" -> callable
        self._offset_at = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = 0.0
        self._round_lock = threading.Lock()

    # ---------------------------------------------------------- what apps see
    def status(self) -> dict:
        """§7: the same status in every app."""
        p = self.st.pairing_info()
        state = self.state
        if p is None:
            state = "unpaired"
        elif self.st.store.meta("needs_decision"):
            state = "needsDecision"
        out = {"state": state, "controller": (p or {}).get("name", ""), "lastSyncAt": self.last_sync_at,
               "pendingRecords": self.st.store.pending_count(), "error": self.error if state == "error" else ""}
        if self.profile_rejected:
            out["profileRejected"] = True
        return out

    def now(self) -> None:
        """Ask for a round; returns at once."""
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
            return
        threading.Thread(target=self._safe_round, name="shack-sync-once", daemon=True).start()

    def local_write(self) -> None:
        """A local edit happened: sync within the debounce (§6.3)."""
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()

    # ---------------------------------------------------------- the thread
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="shack-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=15)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._safe_round()
            wait = self._backoff if self._backoff else PERIOD_S
            woke = self._wake.wait(wait)
            self._wake.clear()
            if woke and not self._stop.is_set() and not self._backoff:
                # Debounce: a burst of edits becomes one round.
                self._stop.wait(DEBOUNCE_S)

    def _safe_round(self) -> None:
        try:
            self.run_once()
        except Exception as e:      # a bug must not kill the thread: say so in the status
            self.state, self.error = "error", f"Sync stopped on an unexpected problem: {e}"

    # ---------------------------------------------------------- one round
    def run_once(self) -> dict:
        """One sync round, synchronously. Returns the status."""
        with self._round_lock:
            p = self.st.pairing_info()
            if p is None:
                self.state = "unpaired"
                return self.status()
            if self.st.store.meta("needs_decision"):
                return self.status()
            store = self.st.store
            if not store.take_lease(self.holder):
                return self.status()         # another process is syncing this store
            self.state = "syncing"
            try:
                t = self.st.transport()
                self._measure(t)
                self._push(t, p["controllerId"])
                self._pull(t, p["controllerId"])
                self.state, self.error = "idle", ""
                self.last_sync_at = store.now_iso()
                known = json.loads(store.meta("synced_with") or "[]")
                if p["controllerId"] not in known:
                    with store.tx():
                        store.set_meta("synced_with", json.dumps(known + [p["controllerId"]]))
                self._backoff = 0.0
            except Offline:
                self.state = "offline"
                self._backoff = min(BACKOFF_MAX_S, max(1.0, self._backoff * 2))
            except _Refused as r:
                self.state, self.error = "error", r.message
                self._backoff = min(BACKOFF_MAX_S, max(1.0, self._backoff * 2))
            finally:
                store.release_lease(self.holder)
            return self.status()

    def _measure(self, t, force: bool = False) -> None:
        """§3.2 offset: the controller's `time`, half the round trip as
        the error, kept when the error is under 2 s."""
        if not force and time.monotonic() - self._offset_at < OFFSET_EVERY_S and self._offset_at:
            return
        t0 = self.st.store.clock()
        status, ans = t.request("GET", "api/shack/v1/")
        t1 = self.st.store.clock()
        if status != 200 or "time" not in ans:
            _check(status, ans)
            return
        err = (t1 - t0) / 2
        if err < MAX_OFFSET_ERROR_MS:
            offset = H.parse_iso_ms(ans["time"]) - (t0 + t1) // 2
            with self.st.store.tx():
                self.st.store.set_meta("clock_offset_ms", int(offset))
        self._offset_at = time.monotonic()

    def _push(self, t, cid: str) -> None:
        store = self.st.store
        rollback = set()
        # Bounded so a controller that keeps answering without confirming
        # anything cannot spin this loop; each pass moves up to 500 records.
        for _attempt in range(1000):
            batch = store.dirty(limit=500)
            keys = {(r["collection"], r["id"]) for r, _rev in batch}
            dirty = store.dirty_keys()
            # Ops travel with their record's state, or alone once that state
            # is confirmed; an op whose state is still waiting for a later
            # batch would only be ignored.
            ops = [o for o in store.unacked_ops(cid, limit=2000)
                   if (o["collection"], o["id"]) in keys or (o["collection"], o["id"]) not in dirty]
            if not batch and not ops:
                break
            body = {"storeId": store.store_id, "records": [r for r, _ in batch], "ops": ops}
            status, ans = t.request("POST", "api/shack/v1/sync/push", body)
            _check(status, ans)
            revs = {(r["collection"], r["id"]): rev for r, rev in batch}
            acked, ahead, rejected_ops = [], False, []
            for res in ans.get("records", []):
                key = (res.get("collection"), res.get("id"))
                if res.get("status") in ("merged", "unchanged") and key in revs:
                    acked.append((key, revs[key]))
                elif res.get("status") == "rejected" and res.get("code") == "clock_ahead":
                    ahead = True
                elif res.get("status") == "rejected" and res.get("code") == "level_too_low":
                    # §4.2: roll the local copy back to the controller's on
                    # the next pull; its ops are never pushed again.
                    rollback.add(key)
                    acked.append((key, revs.get(key, -1)))
                    rejected_ops += [o["opId"] for o in ops if (o["collection"], o["id"]) == key]
                    self.profile_rejected = True
            done = ans.get("ops", {})
            store.ack(cid, acked, list(done.get("accepted", [])) + list(done.get("duplicate", [])), rejected_ops)
            if ahead:
                # §3.3: re-measure, reset the HLC to the corrected clock,
                # re-stamp what is unacknowledged, push again.
                self._measure(t, force=True)
                store.restamp_unacked(cid)
                continue
            if not acked and not done.get("accepted") and not done.get("duplicate"):
                break                       # nothing moved: do not spin
        if rollback:
            self._rollback(t, rollback)

    def _rollback(self, t, keys: set) -> None:
        status, ans = t.request("GET", "api/shack/v1/sync/pull", query={"since": 0, "limit": 500,
                                                                       "collections": "profile"})
        _check(status, ans)
        theirs = {(r["collection"], r["id"]): r for r in ans.get("records", [])}
        recs = []
        for key in keys:
            if key in theirs:
                recs.append(theirs[key])
            else:
                # The controller has no profile yet: the station's profile
                # is the controller's when paired, so the local edit goes
                # (it stays in the local ops as history).
                recs.append({"collection": key[0], "id": key[1], "fields": {}, "fieldHlc": {}, "deletedHlc": None,
                             "createdAt": "", "origin": {}})
        changed = self.st.store.merge_remote(recs, replace=set(keys))
        self.st._notify(changed)

    def _pull(self, t, cid: str) -> None:
        store = self.st.store
        for _restart in range(2):
            cur = store.cursor(cid)
            epoch, since = (cur if cur else (None, 0))
            while True:
                status, ans = t.request("GET", "api/shack/v1/sync/pull", query={
                    "since": since, "epoch": epoch, "limit": self.page_limit, "collections": "logbook,profile"})
                if status == 409 and ans.get("code") == "epoch_changed":
                    # §6.2: restored from a backup. Drop the cursor, push
                    # everything, pull from the start.
                    store.drop_cursor(cid)
                    store.mark_all_dirty()
                    self._push(t, cid)
                    break
                _check(status, ans)
                changed = store.merge_remote(ans.get("records", []))
                self.st._notify(changed)
                since, epoch = ans.get("next", since), ans.get("epoch", epoch)
                store.set_cursor(cid, epoch, since)
                hook = self.hooks.get("after_pull_page")
                if hook:
                    hook(ans)
                if not ans.get("more"):
                    # Anything merged that the controller lacks goes back.
                    if store.pending_count():
                        self._push(t, cid)
                    return
            continue


class _Refused(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _check(status: int, ans: dict) -> None:
    if status >= 400:
        raise _Refused(status, ans.get("error") or f"The controller answered {status}.", ans.get("code", ""))
