package com.openhamshack.client

import java.io.File
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * The local store (SYNC.md §2), a port of python/shack_client store.py,
 * with the same schema and the same three additive columns (`records.extra`,
 * `records.rev`, `ops.rejected_by`), so a store file means the same thing
 * whichever library wrote it.
 *
 * Every read and write goes here and returns: nothing a person does waits
 * on the network (§1 rule 1). Writes run in BEGIN IMMEDIATE transactions
 * that also advance the HLC kept in `meta`, so two connections writing one
 * file never mint the same stamp and never lose a write (T18).
 *
 * One connection per store, behind a lock: the app's UI thread and the sync
 * thread take turns. The Python store opens one connection per thread
 * instead; here the lock is simpler and costs nothing, because no network
 * I/O ever happens while it is held (the sync client builds a push, lets
 * go, sends it, and comes back with the answer).
 */
class Store(val path: File, opener: SqlOpener, val clock: () -> Long = Hlc::systemMs) : AutoCloseable {
    private val db: SqlConnection
    private val lock = ReentrantLock()
    val storeId: String

    init {
        path.absoluteFile.parentFile?.mkdirs()
        db = opener.open(path.path)
        withDb {
            for (stmt in SCHEMA.split(";").map { it.trim() }.filter { it.isNotEmpty() }) db.exec(stmt)
            db.immediate {
                if (meta("store_id") == null) {
                    setMeta("store_id", mintStoreId())
                    setMeta("schema_version", SCHEMA_VERSION)
                    setMeta("hlc_wall", 0)
                    setMeta("hlc_count", 0)
                    setMeta("clock_offset_ms", 0)
                }
            }
        }
        storeId = meta("store_id")!!
    }

    // ---------------------------------------------------------- plumbing
    fun <T> withDb(block: () -> T): T = lock.withLock(block)

    fun <T> tx(block: () -> T): T = lock.withLock { db.immediate(block) }

    fun query(sql: String, vararg args: Any?): List<Array<Any?>> = withDb { db.query(sql, *args) }

    fun meta(key: String): String? = withDb { db.query("SELECT value FROM meta WHERE key=?", key).firstOrNull()?.get(0) as String? }

    fun setMeta(key: String, value: Any) = withDb {
        db.exec("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key, value.toString())
    }

    fun deleteMeta(key: String) = tx { db.exec("DELETE FROM meta WHERE key=?", key) }

    /** §3.2 now(): the corrected clock, never below the floor. */
    fun now(): Long = Hlc.corrected(clock(), (meta("clock_offset_ms") ?: "0").toLong())

    fun nowIso(): String = Hlc.isoMs(now())

    /** A new HLC for a local edit; call inside a transaction. */
    private fun stamp(): String {
        val (st, s) = Hlc.local(meta("hlc_wall")!!.toLong(), meta("hlc_count")!!.toInt(), storeId, now())
        setMeta("hlc_wall", st.wall)
        setMeta("hlc_count", st.count)
        return s
    }

    private fun receive(stamps: List<String>) {
        var w = meta("hlc_wall")!!.toLong()
        var c = meta("hlc_count")!!.toInt()
        val now = now()
        for (s in stamps) {
            if (s.isEmpty()) continue
            val st = Hlc.receive(w, c, s, now)
            w = st.wall
            c = st.count
        }
        setMeta("hlc_wall", w)
        setMeta("hlc_count", c)
    }

    /** SQLite's change counter for other connections' commits: how a
     *  process notices another's writes (§2.2). */
    fun dataVersion(): Long = query("PRAGMA data_version").first()[0].asLong()

    // ---------------------------------------------------------- records
    fun get(collection: String, id: String): Record? =
        query("SELECT $COLS FROM records WHERE collection=? AND id=?", collection, id).firstOrNull()?.let(::row)

    fun all(collection: String): List<Record> =
        query("SELECT $COLS FROM records WHERE collection=? ORDER BY created_at, id", collection).map(::row)

    private fun put(rec: Record, dirty: Boolean?, bump: Boolean) {
        val coll = rec["collection"] as String
        val id = rec["id"] as String
        val cur = db.query("SELECT dirty, rev FROM records WHERE collection=? AND id=?", coll, id).firstOrNull()
        val d = when (dirty) {
            null -> cur?.get(0).asLong()
            true -> 1L
            false -> 0L
        }
        val rev = (cur?.get(1).asLong()) + (if (bump) 1 else 0)
        db.exec("INSERT INTO records($COLS, dirty, rev) VALUES (?,?,?,?,?,?,?,?,?,?,?) " +
            "ON CONFLICT(collection, id) DO UPDATE SET fields=excluded.fields, field_hlc=excluded.field_hlc, " +
            "deleted_hlc=excluded.deleted_hlc, created_at=excluded.created_at, updated_at=excluded.updated_at, " +
            "origin=excluded.origin, extra=excluded.extra, dirty=excluded.dirty, rev=excluded.rev",
            coll, id, Json.dump(Merge.fields(rec)), Json.dump(Merge.fieldHlc(rec)), Merge.deletedHlc(rec),
            rec["createdAt"].asStr(), nowIso(), Json.dump(rec["origin"].asMap()), Json.dump(Merge.extraOf(rec)), d, rev)
        if (coll == "logbook") {
            val f = Merge.fields(rec)
            val band = f["band"].asStr().ifEmpty { Adif.bandFor(f["freq_hz"]) }
            db.exec("INSERT OR REPLACE INTO qso_index(id, call, band, mode, start, deleted) VALUES (?,?,?,?,?,?)",
                id, (f["call"]?.toString() ?: "").uppercase(java.util.Locale.ROOT), band,
                (f["mode"]?.toString() ?: "").uppercase(java.util.Locale.ROOT), f["start"].asStr(),
                if (Merge.isLive(rec)) 0L else 1L)
        }
    }

    private fun logOp(collection: String, id: String, kind: String, fields: Map<String, Any?>, stamp: String,
                      origin: Map<String, Any?>) {
        db.exec("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
            UUID.randomUUID().toString(), collection, id, kind, Json.dump(fields), stamp, Json.dump(origin), nowIso())
    }

    /** One local edit: stamp it, merge it into the record, log it in the
     *  outbox, mark the record dirty. Returns the new state. */
    fun edit(collection: String, id: String, kind: String, fields: Map<String, Any?>, origin: Map<String, Any?>): Record =
        tx {
            val s = stamp()
            val rec = Merge.applyEdit(get(collection, id), collection, id, kind, fields, s, origin, nowIso())
            put(rec, dirty = true, bump = true)
            logOp(collection, id, kind, fields, s, origin)
            rec
        }

    data class Edit(val collection: String, val id: String, val kind: String, val fields: Map<String, Any?>,
                    val origin: Map<String, Any?>)

    /** Several local edits in one transaction (ADIF import, bulk adds). */
    fun editMany(edits: List<Edit>): List<Record> = tx {
        edits.map { e ->
            val s = stamp()
            val rec = Merge.applyEdit(get(e.collection, e.id), e.collection, e.id, e.kind, e.fields, s, e.origin, nowIso())
            put(rec, dirty = true, bump = true)
            logOp(e.collection, e.id, e.kind, e.fields, s, e.origin)
            rec
        }
    }

    /** Records from a pull: the receive rule for every stamp, then the
     *  merge. [replace] names (collection, id) pairs to take as they are
     *  rather than merge (a profile edit the controller refused: §4.2).
     *  Returns the pairs that changed. */
    fun mergeRemote(recs: List<Record>, replace: Set<Pair<String, String>> = emptySet()): List<Pair<String, String>> {
        val changed = ArrayList<Pair<String, String>>()
        tx {
            for (raw in recs) {
                val rec = raw.filterKeys { it != "seq" }
                val key = rec["collection"] as String to rec["id"] as String
                receive(Merge.stamps(rec))
                val cur = get(key.first, key.second)
                val new: Record
                val dirty: Boolean
                if (cur == null || key in replace) {
                    new = rec
                    dirty = false
                } else {
                    new = Merge.merge(cur, rec)
                    // Dirty while the local copy holds something the
                    // controller lacks; clean once the merge is its state.
                    dirty = !Merge.same(new, rec)
                }
                put(new, dirty = dirty, bump = dirty)
                if (cur == null || !Merge.same(new, cur)) changed.add(key)
            }
        }
        return changed
    }

    // ---------------------------------------------------------- outbox
    fun dirty(limit: Int = 500): List<Pair<Record, Long>> =
        query("SELECT $COLS, rev FROM records WHERE dirty=1 ORDER BY rowid LIMIT ?", limit.toLong())
            .map { row(it.copyOfRange(0, it.size - 1)) to it.last().asLong() }

    fun dirtyKeys(): Set<Pair<String, String>> =
        query("SELECT collection, id FROM records WHERE dirty=1").map { it[0] as String to it[1] as String }.toSet()

    fun pendingCount(): Int = query("SELECT COUNT(*) FROM records WHERE dirty=1").first()[0].asLong().toInt()

    fun unackedOps(controllerId: String, limit: Int = 2000): List<Map<String, Any?>> {
        // The JSON lists hold quoted ids, so instr() on the quoted id is an
        // exact membership test that keeps a 10,000-op outbox in SQLite.
        val q = Json.dump(controllerId)
        val rows = query("SELECT op_id, collection, id, kind, fields, hlc, origin, created_at, acked_by, rejected_by " +
            "FROM ops WHERE instr(acked_by, ?) = 0 AND instr(rejected_by, ?) = 0 ORDER BY hlc", q, q)
        val out = ArrayList<Map<String, Any?>>()
        for (r in rows) {
            if (controllerId in Json.parse(r[8] as String).asList() || controllerId in Json.parse(r[9] as String).asList()) continue
            out.add(linkedMapOf("opId" to r[0], "collection" to r[1], "id" to r[2], "kind" to r[3],
                "fields" to Json.parse(r[4] as String), "hlc" to r[5], "origin" to Json.parse(r[6] as String),
                "createdAt" to r[7]))
            if (out.size >= limit) break
        }
        return out
    }

    /** Clear dirty for what the answer confirmed (§6.1), only where the
     *  record has not changed since the push was built. */
    fun ack(controllerId: String, records: List<Pair<Pair<String, String>, Long>>, opIds: List<String>,
            rejectedOps: List<String> = emptyList()) = tx {
        for ((key, rev) in records) {
            db.exec("UPDATE records SET dirty=0 WHERE collection=? AND id=? AND rev=?", key.first, key.second, rev)
        }
        for ((oid, column) in opIds.map { it to "acked_by" } + rejectedOps.map { it to "rejected_by" }) {
            val row = db.query("SELECT $column FROM ops WHERE op_id=?", oid).firstOrNull() ?: continue
            val ids = Json.parse(row[0] as String).asList().toMutableList()
            if (controllerId !in ids) {
                ids.add(controllerId)
                db.exec("UPDATE ops SET $column=? WHERE op_id=?", Json.dump(ids), oid)
            }
        }
    }

    fun markAllDirty() = tx { db.exec("UPDATE records SET dirty=1") }

    /** After `clock_ahead` (§3.3): reset the HLC to the corrected clock and
     *  re-stamp the unacknowledged ops in their original order, moving each
     *  record's field stamps with them, so no future stamp ever reaches the
     *  controller. Returns how many stamps moved. */
    fun restampUnacked(controllerId: String): Int = tx {
        val now = now()
        setMeta("hlc_wall", now)
        setMeta("hlc_count", 0)
        val ops = unackedOps(controllerId, Int.MAX_VALUE)
        val future = now + Hlc.MAX_AHEAD_MS
        // Field stamps from edits whose ops are already acknowledged can
        // still be in the future (made offline under the wrong clock and
        // pushed nowhere yet): every local stamp past the limit moves.
        val remap = LinkedHashMap<String, String>()
        for (op in ops) {
            val new = stamp()
            remap[op["hlc"] as String] = new
            db.exec("UPDATE ops SET hlc=? WHERE op_id=?", new, op["opId"])
        }
        fun mine(s: String) = s.endsWith(storeId) && Hlc.wallOf(s) > future
        for (rec in all("logbook") + all("profile")) {
            var moved = false
            val fh = LinkedHashMap(Merge.fieldHlc(rec))
            for ((f, s) in fh.entries.toList()) {
                if (s in remap) {
                    fh[f] = remap.getValue(s); moved = true
                } else if (mine(s)) {
                    fh[f] = remap.getOrPut(s) { stamp() }; moved = true
                }
            }
            var d = Merge.deletedHlc(rec)
            if (d != null && (d in remap || mine(d))) {
                d = remap.getOrPut(d) { stamp() }; moved = true
            }
            if (moved) {
                val r = LinkedHashMap(rec)
                r["fieldHlc"] = fh
                r["deletedHlc"] = d
                put(r, dirty = true, bump = true)
            }
        }
        remap.size
    }

    // ---------------------------------------------------------- cursors and lease
    fun cursor(controllerId: String): Pair<String, Long>? =
        query("SELECT epoch, seq FROM cursors WHERE controller_id=?", controllerId).firstOrNull()
            ?.let { it[0] as String to it[1].asLong() }

    fun setCursor(controllerId: String, epoch: String, seq: Long) = tx {
        db.exec("INSERT INTO cursors(controller_id, epoch, seq, last_sync_at) VALUES (?,?,?,?) " +
            "ON CONFLICT(controller_id) DO UPDATE SET epoch=excluded.epoch, seq=excluded.seq, " +
            "last_sync_at=excluded.last_sync_at", controllerId, epoch, seq, nowIso())
    }

    fun dropCursor(controllerId: String) = tx { db.exec("DELETE FROM cursors WHERE controller_id=?", controllerId) }

    /** Only one process syncs a shared store at a time (§2.2). The lease
     *  is wall-clock seconds like the Python store's, so a Python app and a
     *  JVM process on one desktop respect each other's. */
    fun takeLease(holder: String, seconds: Double = 30.0): Boolean = tx {
        val row = db.query("SELECT holder, expires_at FROM sync_lease WHERE one=1").firstOrNull()
        val now = System.currentTimeMillis() / 1000.0
        if (row != null && row[0] != holder && (row[1] as Number).toDouble() > now) {
            false
        } else {
            db.exec("INSERT INTO sync_lease(one, holder, expires_at) VALUES (1,?,?) ON CONFLICT(one) " +
                "DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at", holder, now + seconds)
            true
        }
    }

    fun releaseLease(holder: String) = tx { db.exec("DELETE FROM sync_lease WHERE one=1 AND holder=?", holder) }

    // ---------------------------------------------------------- whole-store operations
    /** Forget every record and op, keep the store's identity and clock:
     *  "use the controller's log" after archiving (§7). */
    fun wipeReplica() = tx {
        for (t in listOf("records", "ops", "cursors", "qso_index")) db.exec("DELETE FROM $t")
    }

    fun archiveTo(dest: File) = withDb { db.backupTo(dest.path) }

    override fun close() = withDb { db.close() }

    companion object {
        const val SCHEMA_VERSION = 1

        private const val COLS = "collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra"

        private val SCHEMA = """
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
            CREATE TABLE IF NOT EXISTS sync_lease (one INTEGER PRIMARY KEY CHECK (one = 1), holder TEXT, expires_at REAL)
        """.trimIndent()

        private const val B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        private val random = SecureRandom()

        fun mintStoreId(): String = "st_" + (1..12).map { B32[random.nextInt(32)] }.joinToString("")

        /** An RFC 9562 version 7 UUID (time-ordered, so the logbook's index
         *  stays in insertion order). */
        fun uuid7(nowMs: Long = Hlc.systemMs()): String {
            val rand = ByteArray(10).also { random.nextBytes(it) }
            val hi = ((nowMs and 0xFFFFFFFFFFFFL) shl 16) or (0x7L shl 12) or
                (((rand[0].toLong() and 0xFF) shl 4 or ((rand[1].toLong() and 0xF0) shr 4)) and 0xFFF)
            var lo = 0L
            for (i in 2 until 10) lo = (lo shl 8) or (rand[i].toLong() and 0xFF)
            lo = (lo and 0x3FFFFFFFFFFFFFFFL) or Long.MIN_VALUE   // variant 10
            return UUID(hi, lo).toString()
        }

        private fun row(r: Array<Any?>): Record {
            val rec = LinkedHashMap<String, Any?>()
            rec["collection"] = r[0]
            rec["id"] = r[1]
            rec["fields"] = Json.parse(r[2] as String)
            rec["fieldHlc"] = Json.parse(r[3] as String)
            rec["deletedHlc"] = r[4]
            rec["createdAt"] = r[5]
            rec["origin"] = Json.parse(r[7] as String)
            rec.putAll(Json.parse(r[8] as String).asMap())
            return rec
        }
    }
}
