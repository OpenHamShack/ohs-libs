"""The local store: one SQLite file per user per machine, shared by every
OpenHamShack app on it (SYNC.md §2).

Every read and write goes here and returns: nothing a person does waits on
the network (§1 rule 1). Writes run in `BEGIN IMMEDIATE` transactions that
also advance the HLC kept in `meta`, so several processes writing one
store (QO-100 NB and Radio on one desktop) never mint the same stamp and
never lose a write (§2.2, T18).

Schema is §2.2's, plus three additive columns the spec's own rules need:
`records.extra` (envelope keys a newer contract adds, carried through:
T19), `records.rev` (bumped by every local write, so a push answer clears
`dirty` only for the state it actually confirmed, not for an edit made
while it was in flight) and `ops.rejected_by` (ops a controller refused for
good, such as a profile edit without the Transmit level, which must not be
pushed again). §2.2 allows additive changes only, so an older library
opening this store ignores them.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import hlc as H
from .adif import band_for
from .merge import apply_edit, is_live, merge, same

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    collection  TEXT NOT NULL,
    id          TEXT NOT NULL,
    fields      TEXT NOT NULL,
    field_hlc   TEXT NOT NULL,
    deleted_hlc TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    origin      TEXT NOT NULL,
    dirty       INTEGER NOT NULL DEFAULT 0,
    extra       TEXT NOT NULL DEFAULT '{}',
    rev         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection, id));
CREATE TABLE IF NOT EXISTS ops (
    op_id       TEXT PRIMARY KEY,
    collection  TEXT NOT NULL,
    id          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    fields      TEXT NOT NULL,
    hlc         TEXT NOT NULL,
    origin      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    acked_by    TEXT NOT NULL DEFAULT '[]',
    rejected_by TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ops_by_record ON ops(collection, id, hlc);
CREATE TABLE IF NOT EXISTS cursors (
    controller_id TEXT PRIMARY KEY,
    epoch         TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    last_sync_at  TEXT);
CREATE TABLE IF NOT EXISTS qso_index (
    id TEXT PRIMARY KEY, call TEXT, band TEXT, mode TEXT, start TEXT, deleted INTEGER);
CREATE INDEX IF NOT EXISTS qso_by_call  ON qso_index(call, band, mode);
CREATE INDEX IF NOT EXISTS qso_by_start ON qso_index(start);
CREATE TABLE IF NOT EXISTS sync_lease (one INTEGER PRIMARY KEY CHECK (one = 1), holder TEXT, expires_at REAL);
"""

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def mint_store_id() -> str:
    return "st_" + "".join(secrets.choice(_B32) for _ in range(12))


def uuid7(now_ms: int | None = None) -> str:
    """An RFC 9562 version 7 UUID (time-ordered, so the logbook's index
    stays in insertion order). Python 3.11-3.13 have no uuid.uuid7."""
    ms = H.system_ms() if now_ms is None else now_ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def default_path() -> Path:
    """§2.1: one store per user per machine."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "OpenHamShack"
    elif os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "OpenHamShack"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "openhamshack"
    return base / "station.db"


def _row(row) -> dict:
    coll, rid, fields, fh, dh, created, _updated, origin, extra = row
    rec = {"collection": coll, "id": rid, "fields": json.loads(fields), "fieldHlc": json.loads(fh),
           "deletedHlc": dh, "createdAt": created, "origin": json.loads(origin)}
    rec.update(json.loads(extra))
    return rec


_COLS = "collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra"


class Store:
    """Low-level access; the friendly API is shack_client.Station."""

    def __init__(self, path: str | Path, clock=H.system_ms):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        # One connection per thread: an app's UI thread and the sync thread
        # each get their own, so a write on one never lands inside the
        # other's transaction. SQLite's own locking (WAL, busy_timeout)
        # then serialises them exactly as it does two processes.
        #
        # _conns pairs each connection with the thread that opened it, so
        # close() can shut every one and a dead thread's can be found. It
        # used to be a bare list, which kept every connection (the database
        # and its WAL: two fds) alive until the store closed - harmless for
        # a UI thread and a sync thread, fatal for a ThreadingHTTPServer
        # that starts a thread per request (openhamshack-nb ran out of fds in
        # about 40 minutes). SQLite connections cannot be weakly referenced,
        # so instead each new connection sweeps up those whose thread has
        # ended; a thread that knows it is finishing can call
        # release_thread() and not wait for the sweep.
        self._local = threading.local()
        self._conns: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._conns_lock = threading.Lock()
        self.db.executescript(_SCHEMA)
        with self.tx():
            if self.meta("store_id") is None:
                self.set_meta("store_id", mint_store_id())
                self.set_meta("schema_version", SCHEMA_VERSION)
                self.set_meta("hlc_wall", 0)
                self.set_meta("hlc_count", 0)
                self.set_meta("clock_offset_ms", 0)
        self.store_id = self.meta("store_id")

    # ---------------------------------------------------------- plumbing
    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            with self._conns_lock:
                dead = [c for t, c in self._conns if not t.is_alive()]
                self._conns = [(t, c) for t, c in self._conns if t.is_alive()]
                self._conns.append((threading.current_thread(), conn))
            # Nothing else can be using a dead thread's connection: it was
            # only ever reachable through that thread's local.
            for c in dead:
                c.close()
        return conn

    def release_thread(self) -> None:
        """Close the calling thread's connection, for a thread about to end
        (a request handler, a worker). The store stays usable: touching it
        again from this thread opens a fresh connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conns_lock:
            self._conns = [(t, c) for t, c in self._conns if c is not conn]
        conn.close()

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def now(self) -> int:
        """§3.2 now(): corrected clock, never below the floor."""
        return H.corrected(self.clock(), int(self.meta("clock_offset_ms") or 0))

    def now_iso(self) -> str:
        return H.iso_ms(self.now())

    def _stamp(self) -> str:
        """A new HLC for a local edit; call inside a transaction."""
        w, c, s = H.local(int(self.meta("hlc_wall")), int(self.meta("hlc_count")), self.store_id, self.now())
        self.set_meta("hlc_wall", w)
        self.set_meta("hlc_count", c)
        return s

    def _receive(self, stamps) -> None:
        w, c = int(self.meta("hlc_wall")), int(self.meta("hlc_count"))
        now = self.now()
        for s in stamps:
            if s:
                w, c = H.receive(w, c, s, now)
        self.set_meta("hlc_wall", w)
        self.set_meta("hlc_count", c)

    def data_version(self) -> int:
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    # ---------------------------------------------------------- records
    def get(self, collection: str, rid: str) -> dict | None:
        row = self.db.execute(f"SELECT {_COLS} FROM records WHERE collection=? AND id=?", (collection, rid)).fetchone()
        return _row(row) if row else None

    def all(self, collection: str) -> list[dict]:
        rows = self.db.execute(f"SELECT {_COLS} FROM records WHERE collection=? ORDER BY created_at, id",
                               (collection,)).fetchall()
        return [_row(r) for r in rows]

    def _put(self, rec: dict, dirty: bool | None, bump: bool) -> None:
        from .merge import extra_of
        cur = self.db.execute("SELECT dirty, rev FROM records WHERE collection=? AND id=?",
                              (rec["collection"], rec["id"])).fetchone()
        d = int(dirty) if dirty is not None else (cur[0] if cur else 0)
        rev = (cur[1] if cur else 0) + (1 if bump else 0)
        self.db.execute(
            f"INSERT INTO records({_COLS}, dirty, rev) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(collection, id) DO UPDATE SET fields=excluded.fields, field_hlc=excluded.field_hlc, "
            "deleted_hlc=excluded.deleted_hlc, created_at=excluded.created_at, updated_at=excluded.updated_at, "
            "origin=excluded.origin, extra=excluded.extra, dirty=excluded.dirty, rev=excluded.rev",
            (rec["collection"], rec["id"], json.dumps(rec["fields"], sort_keys=True),
             json.dumps(rec["fieldHlc"], sort_keys=True), rec.get("deletedHlc"), rec.get("createdAt") or "",
             self.now_iso(), json.dumps(rec.get("origin") or {}, sort_keys=True),
             json.dumps(extra_of(rec), sort_keys=True), d, rev))
        if rec["collection"] == "logbook":
            f = rec["fields"]
            band = f.get("band") or band_for(f.get("freq_hz"))
            self.db.execute("INSERT OR REPLACE INTO qso_index(id, call, band, mode, start, deleted) VALUES (?,?,?,?,?,?)",
                            (rec["id"], str(f.get("call", "")).upper(), band, str(f.get("mode", "")).upper(),
                             f.get("start", ""), 0 if is_live(rec) else 1))

    def edit(self, collection: str, rid: str, kind: str, fields: dict, origin: dict) -> dict:
        """One local edit: stamp it, merge it into the record, log it in
        the outbox, mark the record dirty. Returns the new state."""
        with self.tx():
            stamp = self._stamp()
            rec = apply_edit(self.get(collection, rid), collection, rid, kind, fields, stamp, origin, self.now_iso())
            self._put(rec, dirty=True, bump=True)
            self.db.execute("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), collection, rid, kind, json.dumps(fields, sort_keys=True), stamp,
                             json.dumps(origin, sort_keys=True), self.now_iso()))
        return rec

    def edit_many(self, edits: list) -> list:
        """Several local edits in one transaction (ADIF import, bulk adds):
        [(collection, id, kind, fields, origin)]."""
        out = []
        with self.tx():
            for collection, rid, kind, fields, origin in edits:
                stamp = self._stamp()
                rec = apply_edit(self.get(collection, rid), collection, rid, kind, fields, stamp, origin,
                                 self.now_iso())
                self._put(rec, dirty=True, bump=True)
                self.db.execute("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at) "
                                "VALUES (?,?,?,?,?,?,?,?)",
                                (str(uuid.uuid4()), collection, rid, kind, json.dumps(fields, sort_keys=True), stamp,
                                 json.dumps(origin, sort_keys=True), self.now_iso()))
                out.append(rec)
        return out

    def merge_remote(self, recs: list, replace: set | None = None) -> list:
        """Records from a pull: the receive rule for every stamp, then the
        merge. `replace` names (collection, id) pairs to take as they are
        rather than merge (a profile edit the controller refused: §4.2).
        Returns the (collection, id) pairs that changed."""
        changed = []
        with self.tx():
            for rec in recs:
                rec = {k: v for k, v in rec.items() if k != "seq"}
                key = (rec["collection"], rec["id"])
                self._receive(list(rec["fieldHlc"].values()) + [rec.get("deletedHlc")])
                cur = self.get(*key)
                if cur is None or (replace and key in replace):
                    new, dirty = rec, False
                else:
                    new = merge(cur, rec)
                    # Dirty while the local copy holds something the
                    # controller lacks; clean once the merge is its state.
                    dirty = not same(new, rec)
                self._put(new, dirty=dirty, bump=dirty)
                if cur is None or not same(new, cur):
                    changed.append(key)
        return changed

    # ---------------------------------------------------------- outbox
    def dirty(self, limit: int = 500) -> list[tuple[dict, int]]:
        rows = self.db.execute(f"SELECT {_COLS}, rev FROM records WHERE dirty=1 ORDER BY rowid LIMIT ?",
                               (limit,)).fetchall()
        return [(_row(r[:-1]), r[-1]) for r in rows]

    def dirty_keys(self) -> set:
        return {(c, i) for c, i in self.db.execute("SELECT collection, id FROM records WHERE dirty=1")}

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM records WHERE dirty=1").fetchone()[0]

    def unacked_ops(self, controller_id: str, keys: set | None = None, limit: int = 2000) -> list[dict]:
        # The JSON lists hold quoted ids, so instr() on the quoted id is an
        # exact membership test that keeps a 10,000-op outbox out of Python.
        q = json.dumps(controller_id)
        rows = self.db.execute("SELECT op_id, collection, id, kind, fields, hlc, origin, created_at, acked_by, "
                               "rejected_by FROM ops WHERE instr(acked_by, ?) = 0 AND instr(rejected_by, ?) = 0 "
                               "ORDER BY hlc", (q, q)).fetchall()
        out = []
        for oid, coll, rid, kind, fields, stamp, origin, created, acked, rejected in rows:
            if controller_id in json.loads(acked) or controller_id in json.loads(rejected):
                continue
            if keys is not None and (coll, rid) not in keys:
                continue
            out.append({"opId": oid, "collection": coll, "id": rid, "kind": kind, "fields": json.loads(fields),
                        "hlc": stamp, "origin": json.loads(origin), "createdAt": created})
            if len(out) >= limit:
                break
        return out

    def ack(self, controller_id: str, records: list, op_ids: list, rejected_ops: list = ()) -> None:
        """Clear dirty for what the answer confirmed (§6.1), only where the
        record has not changed since the push was built."""
        with self.tx():
            for (coll, rid), rev in records:
                self.db.execute("UPDATE records SET dirty=0 WHERE collection=? AND id=? AND rev=?", (coll, rid, rev))
            for oid, column in [(o, "acked_by") for o in op_ids] + [(o, "rejected_by") for o in rejected_ops]:
                row = self.db.execute(f"SELECT {column} FROM ops WHERE op_id=?", (oid,)).fetchone()
                if row:
                    ids = json.loads(row[0])
                    if controller_id not in ids:
                        ids.append(controller_id)
                        self.db.execute(f"UPDATE ops SET {column}=? WHERE op_id=?", (json.dumps(ids), oid))

    def mark_all_dirty(self) -> None:
        with self.tx():
            self.db.execute("UPDATE records SET dirty=1")

    def restamp_unacked(self, controller_id: str) -> int:
        """After `clock_ahead` (§3.3): reset the HLC to the corrected clock
        and re-stamp the unacknowledged ops in their original order, moving
        each record's field stamps with them, so no future stamp ever
        reaches the controller. Returns how many ops were re-stamped."""
        with self.tx():
            now = self.now()
            self.set_meta("hlc_wall", now)
            self.set_meta("hlc_count", 0)
            ops = self.unacked_ops(controller_id, limit=1 << 30)
            future = now + H.MAX_AHEAD_MS
            # Field stamps from edits whose ops are already acknowledged can
            # still be in the future (made offline under the wrong clock and
            # pushed nowhere yet): every local stamp past the limit moves.
            remap = {}
            for op in ops:
                new = self._stamp()
                remap[op["hlc"]] = new
                self.db.execute("UPDATE ops SET hlc=? WHERE op_id=?", (new, op["opId"]))
            for rec in self.all("logbook") + self.all("profile"):
                moved = False
                for f, s in list(rec["fieldHlc"].items()):
                    if s in remap:
                        rec["fieldHlc"][f] = remap[s]
                        moved = True
                    elif s.endswith(self.store_id) and H.wall_of(s) > future:
                        rec["fieldHlc"][f] = remap.setdefault(s, self._stamp())
                        moved = True
                d = rec.get("deletedHlc")
                if d and (d in remap or (d.endswith(self.store_id) and H.wall_of(d) > future)):
                    rec["deletedHlc"] = remap.setdefault(d, self._stamp())
                    moved = True
                if moved:
                    self._put(rec, dirty=True, bump=True)
        return len(remap)

    # ---------------------------------------------------------- cursors and lease
    def cursor(self, controller_id: str) -> tuple[str, int] | None:
        row = self.db.execute("SELECT epoch, seq FROM cursors WHERE controller_id=?", (controller_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def set_cursor(self, controller_id: str, epoch: str, seq: int) -> None:
        with self.tx():
            self.db.execute("INSERT INTO cursors(controller_id, epoch, seq, last_sync_at) VALUES (?,?,?,?) "
                            "ON CONFLICT(controller_id) DO UPDATE SET epoch=excluded.epoch, seq=excluded.seq, "
                            "last_sync_at=excluded.last_sync_at", (controller_id, epoch, seq, self.now_iso()))

    def drop_cursor(self, controller_id: str) -> None:
        with self.tx():
            self.db.execute("DELETE FROM cursors WHERE controller_id=?", (controller_id,))

    def take_lease(self, holder: str, seconds: float = 30.0) -> bool:
        """Only one process syncs a shared store at a time (§2.2)."""
        with self.tx():
            row = self.db.execute("SELECT holder, expires_at FROM sync_lease WHERE one=1").fetchone()
            now = time.time()
            if row and row[0] != holder and row[1] > now:
                return False
            self.db.execute("INSERT INTO sync_lease(one, holder, expires_at) VALUES (1,?,?) ON CONFLICT(one) "
                            "DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at", (holder, now + seconds))
            return True

    def release_lease(self, holder: str) -> None:
        with self.tx():
            self.db.execute("DELETE FROM sync_lease WHERE one=1 AND holder=?", (holder,))

    # ---------------------------------------------------------- whole-store operations
    def wipe_replica(self) -> None:
        """Forget every record and op, keep the store's identity and clock:
        "use the controller's log" after archiving (§7)."""
        with self.tx():
            for t in ("records", "ops", "cursors", "qso_index"):
                self.db.execute(f"DELETE FROM {t}")

    def archive_to(self, dest: Path) -> None:
        dst = sqlite3.connect(str(dest))
        with dst:
            self.db.backup(dst)
        dst.close()

    def close(self) -> None:
        with self._conns_lock:
            for _, c in self._conns:
                c.close()
            self._conns.clear()
        self._local = threading.local()
