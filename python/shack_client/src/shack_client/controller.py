"""The controller's side of sync (SYNC.md §6 and §10), framework-free.

The Dock360 OpenHamShack edition mounts this behind its own routes and
authentication; the reference controller in shack_client.testing mounts it
behind http.server for the client's tests. One implementation of push,
pull, epochs, sequence numbers and history, beside the merge it uses, so
the client is tested against the same server logic the controller runs.

Storage is `services.db`'s sync tables (§10): records as the client's plus
`seq`; ops as the client's plus `received_at`, `received_seq` and `lost`;
meta with `epoch` and `latest_seq`. Every merge that changes a record takes
the next seq in the same transaction, so a pull "since N" never misses a
change and never sees half of one.

Callers pass `now_ms` (the controller's clock) so tests can run it at any
time; in production it is the system clock.
"""
from __future__ import annotations

from ._i18n import _

import json
import secrets
import sqlite3
import threading
from pathlib import Path

from . import hlc as H
from .merge import apply_edit, extra_of, is_live, lost_fields, merge, same

MAX_BATCH = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL, id TEXT NOT NULL, fields TEXT NOT NULL, field_hlc TEXT NOT NULL,
    deleted_hlc TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, origin TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}', seq INTEGER NOT NULL,
    PRIMARY KEY (collection, id));
CREATE INDEX IF NOT EXISTS records_by_seq ON records(seq);
CREATE TABLE IF NOT EXISTS ops (
    op_id TEXT PRIMARY KEY, collection TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
    fields TEXT NOT NULL, hlc TEXT NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL, received_seq INTEGER NOT NULL, lost TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ops_by_record ON ops(collection, id, hlc);
"""


class SyncService:
    def __init__(self, path: str | Path, controller_id: str, now_ms=H.system_ms):
        self.path = str(path)
        self.controller_id = controller_id
        self.now_ms = now_ms
        self.node = "ctl_" + controller_id.replace("-", "")[:12]
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(_SCHEMA)
        if self._meta("epoch") is None:
            self._set_meta("epoch", self._new_epoch())
            self._set_meta("latest_seq", "0")
            self._set_meta("hlc_wall", "0")
            self._set_meta("hlc_count", "0")

    # ---------------------------------------------------------- meta
    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    @staticmethod
    def _new_epoch() -> str:
        return "e_" + secrets.token_hex(4)

    @property
    def epoch(self) -> str:
        return self._meta("epoch")

    @property
    def latest_seq(self) -> int:
        return int(self._meta("latest_seq"))

    def restored_from_backup(self) -> str:
        """Call after the services database is restored from a backup
        (§6.2): a new epoch, so every client re-pushes what the backup
        lacks instead of trusting a cursor into a history that was
        rewound."""
        with self._lock:
            e = self._new_epoch()
            self._set_meta("epoch", e)
            return e

    def info(self) -> dict:
        """GET /api/shack/v1/ (SERVICES.md §6): clients measure their clock
        offset from `time`."""
        return {"services": {"profile": 1, "logbook": 1, "sync": 1}, "controllerId": self.controller_id,
                "time": H.iso_ms(self.now_ms())}

    # ---------------------------------------------------------- rows
    @staticmethod
    def _row_to_record(row) -> dict:
        coll, rid, fields, fh, dh, created, _updated, origin, extra, seq = row
        rec = {"collection": coll, "id": rid, "fields": json.loads(fields), "fieldHlc": json.loads(fh),
               "deletedHlc": dh, "createdAt": created, "origin": json.loads(origin)}
        rec.update(json.loads(extra))
        rec["seq"] = seq
        return rec

    def get(self, collection: str, rid: str) -> dict | None:
        row = self.db.execute("SELECT collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, "
                              "extra, seq FROM records WHERE collection=? AND id=?", (collection, rid)).fetchone()
        return self._row_to_record(row) if row else None

    def all(self, collection: str | None = None) -> list[dict]:
        q = ("SELECT collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra, seq "
             "FROM records")
        rows = self.db.execute(q + (" WHERE collection=?" if collection else "") + " ORDER BY seq",
                               (collection,) if collection else ()).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _store(self, rec: dict, seq: int) -> None:
        self.db.execute(
            "INSERT INTO records(collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, "
            "extra, seq) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collection, id) DO UPDATE SET fields=excluded.fields, "
            "field_hlc=excluded.field_hlc, deleted_hlc=excluded.deleted_hlc, created_at=excluded.created_at, "
            "updated_at=excluded.updated_at, origin=excluded.origin, extra=excluded.extra, seq=excluded.seq",
            (rec["collection"], rec["id"], json.dumps(rec["fields"], sort_keys=True),
             json.dumps(rec["fieldHlc"], sort_keys=True), rec.get("deletedHlc"), rec.get("createdAt") or "",
             H.iso_ms(self.now_ms()), json.dumps(rec.get("origin") or {}, sort_keys=True),
             json.dumps(extra_of(rec), sort_keys=True), seq))

    # ---------------------------------------------------------- push
    def push(self, body: dict, can_edit_profile: bool = True) -> tuple[int, dict]:
        """POST /api/shack/v1/sync/push. `can_edit_profile` is the caller's
        right to change the station profile (a device with `transmit` on
        some app; never an app token: SERVICES.md §1)."""
        recs = body.get("records") or []
        ops = body.get("ops") or []
        if not isinstance(recs, list) or not isinstance(ops, list) or len(recs) > MAX_BATCH or len(ops) > 4 * MAX_BATCH:
            return 400, {"error": _("A sync push carries at most 500 records."), "code": "bad_request"}
        now = self.now_ms()
        limit = now + H.MAX_AHEAD_MS
        results, rejected = [], set()
        # One transaction per push, a savepoint per record: §6.1 asks that a
        # dropped connection leave nothing half-applied, which all-or-nothing
        # per batch satisfies, and one commit (one fsync) per 500 records
        # instead of one per record is what lets a 10,000-contact first sync
        # finish in seconds on a disk with synchronous=FULL.
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._push_records(recs, now, limit, results, rejected, can_edit_profile)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("BEGIN IMMEDIATE")
            try:
                accepted, duplicate, ignored = self._push_ops(ops, now, limit, rejected)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return 200, {"controllerId": self.controller_id, "epoch": self.epoch, "records": results,
                     "ops": {"accepted": accepted, "duplicate": duplicate, "ignored": ignored}}

    def _push_records(self, recs, now, limit, results, rejected, can_edit_profile) -> None:
        for rec in recs:
            key = (rec.get("collection"), rec.get("id"))
            bad = self._invalid(rec)
            if bad:
                results.append({"collection": key[0], "id": key[1], "status": "rejected", "code": "bad_request",
                                "error": bad})
                rejected.add(key)
                continue
            stamps = list(rec["fieldHlc"].values()) + ([rec["deletedHlc"]] if rec.get("deletedHlc") else [])
            if any(H.wall_of(s) > limit for s in stamps):
                results.append({"collection": key[0], "id": key[1], "status": "rejected", "code": "clock_ahead",
                                "serverTime": H.iso_ms(now)})
                rejected.add(key)
                continue
            if key[0] == "profile" and not can_edit_profile:
                results.append({"collection": key[0], "id": key[1], "status": "rejected", "code": "level_too_low",
                                "error": _("Changing the station profile needs the Transmit level.")})
                rejected.add(key)
                continue
            self.db.execute("SAVEPOINT rec")
            try:
                stored = self.get(*key)
                incoming = {k: v for k, v in rec.items() if k != "seq"}
                merged = merge({k: v for k, v in stored.items() if k != "seq"} if stored else None, incoming)
                if stored is not None and same(merged, stored):
                    results.append({"collection": key[0], "id": key[1], "status": "unchanged",
                                    "seq": stored["seq"], "lost": []})
                else:
                    seq = self.latest_seq + 1
                    self._set_meta("latest_seq", seq)
                    self._store(merged, seq)
                    results.append({"collection": key[0], "id": key[1], "status": "merged", "seq": seq,
                                    "lost": self._lost_against(incoming, stored)})
                self._observe(stamps)
                self.db.execute("RELEASE rec")
            except BaseException:
                self.db.execute("ROLLBACK TO rec")
                self.db.execute("RELEASE rec")
                raise

    def _push_ops(self, ops, now, limit, rejected):
        accepted, duplicate, ignored = [], [], []
        for op in ops:
            key = (op.get("collection"), op.get("id"))
            oid = op.get("opId")
            if not oid or key in rejected or H.wall_of(op.get("hlc")) > limit:
                continue
            if self.db.execute("SELECT 1 FROM ops WHERE op_id=?", (oid,)).fetchone():
                duplicate.append(oid)
                continue
            state = self.get(*key)
            if state is None:
                # The state is the truth and ops are the story (§6.1): an
                # op whose record never arrived tells nothing yet.
                ignored.append(oid)
                continue
            self.db.execute(
                "INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at, received_at, "
                "received_seq, lost) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (oid, key[0], key[1], op.get("kind", "update"), json.dumps(op.get("fields") or {}, sort_keys=True),
                 op["hlc"], json.dumps(op.get("origin") or {}, sort_keys=True), op.get("createdAt", ""),
                 H.iso_ms(now), state["seq"], json.dumps(lost_fields(op, state))))
            accepted.append(oid)
        return accepted, duplicate, ignored

    @staticmethod
    def _invalid(rec: dict) -> str:
        if rec.get("collection") not in ("logbook", "profile") or not isinstance(rec.get("id"), str) or not rec["id"]:
            return "A sync record needs a collection (logbook or profile) and an id."
        if not isinstance(rec.get("fields"), dict) or not isinstance(rec.get("fieldHlc"), dict):
            return "A sync record needs fields and fieldHlc objects."
        try:
            for s in list(rec["fieldHlc"].values()) + ([rec["deletedHlc"]] if rec.get("deletedHlc") else []):
                H.parse(s)
        except (ValueError, TypeError):
            return "A sync record carries a stamp that is not a hybrid logical clock."
        return ""

    @staticmethod
    def _lost_against(incoming: dict, stored: dict | None) -> list:
        """Fields of an incoming state whose value lost to one already
        stored with a later stamp from another store."""
        if stored is None:
            return []
        out = []
        for f, h in incoming["fieldHlc"].items():
            s = stored["fieldHlc"].get(f)
            if s and s > h and s.rsplit("-", 1)[-1] != h.rsplit("-", 1)[-1]:
                out.append(f)
        return sorted(out)

    def _observe(self, stamps: list) -> None:
        """The controller's own HLC follows everything it receives, so its
        own edits (a web UI edit, an app token's QSO) sort after them."""
        w, c = int(self._meta("hlc_wall")), int(self._meta("hlc_count"))
        now = H.corrected(self.now_ms(), 0)
        for s in stamps:
            w, c = H.receive(w, c, s, now)
        self._set_meta("hlc_wall", w)
        self._set_meta("hlc_count", c)

    # ---------------------------------------------------------- pull
    def pull(self, since: int = 0, epoch: str | None = None, limit: int = 500,
             collections: list | None = None) -> tuple[int, dict]:
        """GET /api/shack/v1/sync/pull. Records with seq > since, in seq
        order, each at its current state; `next` is the cursor to store
        after merging the page."""
        latest = self.latest_seq
        if (epoch and epoch != self.epoch) or since > latest:
            return 409, {"error": _("This controller's sync history was restored from a backup; syncing again from "
                                    "the start."), "code": "epoch_changed", "epoch": self.epoch}
        limit = max(1, min(int(limit), MAX_BATCH))
        q = ("SELECT collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra, seq "
             "FROM records WHERE seq > ?")
        args: list = [since]
        if collections:
            q += " AND collection IN (%s)" % ",".join("?" * len(collections))
            args += list(collections)
        rows = self.db.execute(q + " ORDER BY seq LIMIT ?", args + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        recs = [self._row_to_record(r) for r in rows]
        nxt = recs[-1]["seq"] if recs else (latest if not more else since)
        return 200, {"controllerId": self.controller_id, "epoch": self.epoch, "latestSeq": latest,
                     "records": recs, "next": nxt, "more": more}

    # ---------------------------------------------------------- history and own edits
    def history(self, collection: str, rid: str) -> dict:
        rows = self.db.execute("SELECT op_id, hlc, kind, fields, origin, received_at, lost FROM ops "
                               "WHERE collection=? AND id=? ORDER BY hlc", (collection, rid)).fetchall()
        return {"uuid": rid, "edits": [{"opId": o, "hlc": h, "kind": k, "fields": json.loads(f),
                                        "origin": json.loads(org), "receivedAt": r, "lost": json.loads(lost)}
                                       for o, h, k, f, org, r, lost in rows]}

    def edit(self, collection: str, rid: str, kind: str, fields: dict, origin: dict) -> dict:
        """The controller's own write (its web UI, an app token's QSO),
        stamped with its own HLC, node ctl_<first 12 of controllerId> (§10)."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                w, c = int(self._meta("hlc_wall")), int(self._meta("hlc_count"))
                w, c, stamp = H.local(w, c, self.node, H.corrected(self.now_ms(), 0))
                self._set_meta("hlc_wall", w)
                self._set_meta("hlc_count", c)
                stored = self.get(collection, rid)
                base = {k: v for k, v in stored.items() if k != "seq"} if stored else None
                rec = apply_edit(base, collection, rid, kind, fields, stamp, origin, H.iso_ms(self.now_ms()))
                seq = self.latest_seq + 1
                self._set_meta("latest_seq", seq)
                self._store(rec, seq)
                oid = secrets.token_hex(16)
                self.db.execute(
                    "INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at, received_at, "
                    "received_seq, lost) VALUES (?,?,?,?,?,?,?,?,?,?,'[]')",
                    (oid, collection, rid, kind, json.dumps(fields, sort_keys=True), stamp,
                     json.dumps(origin, sort_keys=True), H.iso_ms(self.now_ms()), H.iso_ms(self.now_ms()), seq))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return rec

    def live(self, collection: str) -> list[dict]:
        return [r for r in self.all(collection) if is_live(r)]

    def snapshot(self) -> bytes:
        """A backup of the services database (the restore test's input).
        Through a file, not :memory:, because a backup of a WAL database
        keeps the WAL flag in its header and an in-memory copy cannot open
        it."""
        tmp = Path(self.path + f".snap-{secrets.token_hex(4)}")
        try:
            with self._lock:
                dst = sqlite3.connect(str(tmp))
                self.db.backup(dst)
                dst.close()
            return tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)

    def restore(self, snapshot: bytes) -> None:
        """Restore a snapshot() and mint a new epoch, as the platform's
        restore path must (§6.2)."""
        tmp = Path(self.path + f".restore-{secrets.token_hex(4)}")
        try:
            tmp.write_bytes(snapshot)
            with self._lock:
                src = sqlite3.connect(str(tmp))
                src.backup(self.db)
                src.close()
        finally:
            tmp.unlink(missing_ok=True)
            for extra in ("-wal", "-shm"):
                Path(str(tmp) + extra).unlink(missing_ok=True)
        self.restored_from_backup()

    def close(self) -> None:
        self.db.close()

