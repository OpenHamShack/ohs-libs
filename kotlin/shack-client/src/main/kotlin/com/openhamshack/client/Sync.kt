package com.openhamshack.client

import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** §7: the same status in every app. [state] is `unpaired`, `idle`,
 *  `syncing`, `offline`, `error` (with [error] the controller's sentence)
 *  or `needsDecision`. */
data class SyncStatus(
    val state: String,
    val controller: String = "",
    val lastSyncAt: String = "",
    val pendingRecords: Int = 0,
    val error: String = "",
    val profileRejected: Boolean = false,
)

/**
 * The sync round and its triggers (SYNC.md §6.3, §7), a port of
 * python/shack_client sync.py.
 *
 * A round: take the store's sync lease; measure the controller's clock
 * (once per pairing, then hourly); push dirty records and unacknowledged
 * ops; pull until `more` is false, merging each record; store the cursor;
 * release the lease. Nothing in a UI path waits for any of it: [now] asks
 * for a round and returns at once, and the background thread does the work.
 *
 * Failures back off 1 s, 2 s, 4 s ... to 5 min; a controller that is away
 * leaves the app logging exactly as it did standalone (§7).
 */
class SyncClient internal constructor(private val st: ShackStation) {
    // Unique per client, not per process: two Stations on one file in one
    // process (or a test's two "processes") must not share a lease.
    val holder = UUID.randomUUID().toString()

    @Volatile var state = "unpaired"
        internal set
    @Volatile var error = ""
        internal set
    @Volatile var lastSyncAt = ""
        private set
    @Volatile var profileRejected = false
        internal set

    /** Records per pull page (tests set it lower to exercise paging). */
    var pageLimit = 500

    /** Test hook: called with each pull page's answer after it is merged. */
    var afterPullPage: ((Map<String, Any?>) -> Unit)? = null

    var debounceMs = DEBOUNCE_MS
    var periodMs = PERIOD_MS

    private var offsetAtNanos = 0L
    private var backoffMs = 0L
    private val roundLock = ReentrantLock()
    private val wakeLock = Object()
    private var woken = false
    @Volatile private var stopping = false
    @Volatile private var thread: Thread? = null
    private val listeners = CopyOnWriteArrayList<(SyncStatus) -> Unit>()

    // ---------------------------------------------------------- what apps see
    fun status(): SyncStatus {
        val p = st.pairingInfo()
        var s = state
        if (p == null) s = "unpaired" else if (st.store.meta("needs_decision") != null) s = "needsDecision"
        return SyncStatus(s, p?.get("name").asStr(), lastSyncAt, st.store.pendingCount(),
            if (s == "error") error else "", profileRejected)
    }

    /** Called with the status after every round (on the sync thread), so a
     *  UI can show it; an Android app wraps this in a StateFlow. */
    fun addListener(listener: (SyncStatus) -> Unit) { listeners.add(listener) }

    fun removeListener(listener: (SyncStatus) -> Unit) { listeners.remove(listener) }

    private fun publish() {
        if (listeners.isEmpty()) return
        val s = status()
        listeners.forEach { it(s) }
    }

    /** Ask for a round; returns at once. */
    fun now() {
        if (thread?.isAlive == true) {
            wake()
            return
        }
        Thread({ safeRound() }, "shack-sync-once").apply { isDaemon = true }.start()
    }

    /** A local edit happened: sync within the debounce (§6.3). */
    fun localWrite() {
        if (thread?.isAlive == true) wake()
    }

    private fun wake() = synchronized(wakeLock) {
        woken = true
        wakeLock.notifyAll()
    }

    /** Waits up to [ms]; true when woken early. */
    private fun waitWake(ms: Long): Boolean = synchronized(wakeLock) {
        val end = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(ms)
        while (!woken && !stopping) {
            val left = TimeUnit.NANOSECONDS.toMillis(end - System.nanoTime())
            if (left <= 0) break
            wakeLock.wait(left)
        }
        val w = woken
        woken = false
        w
    }

    // ---------------------------------------------------------- the thread
    fun start() {
        if (thread?.isAlive == true) return
        stopping = false
        thread = Thread({ loop() }, "shack-sync").apply { isDaemon = true; start() }
    }

    fun stop() {
        stopping = true
        wake()
        val t = thread
        if (t != null && t !== Thread.currentThread()) t.join(15_000)
        thread = null
    }

    private fun loop() {
        while (!stopping) {
            safeRound()
            val woke = waitWake(if (backoffMs > 0) backoffMs else periodMs)
            if (woke && !stopping && backoffMs == 0L) {
                // Debounce: a burst of edits becomes one round.
                synchronized(wakeLock) { if (!stopping) wakeLock.wait(debounceMs) }
                synchronized(wakeLock) { woken = false }
            }
        }
    }

    private fun safeRound() {
        try {
            runOnce()
        } catch (e: Exception) {
            // A bug must not kill the thread: say so in the status.
            state = "error"
            error = "Sync stopped on an unexpected problem: $e"
            publish()
        }
    }

    // ---------------------------------------------------------- one round
    /** One sync round, synchronously (off the UI thread). Returns the status. */
    fun runOnce(): SyncStatus = roundLock.withLock {
        val p = st.pairingInfo()
        if (p == null) {
            state = "unpaired"
            return status()
        }
        if (st.store.meta("needs_decision") != null) return status()
        val store = st.store
        // Another process is syncing this store.
        if (!store.takeLease(holder)) return status()
        state = "syncing"
        val cid = p["controllerId"].asStr()
        try {
            val t = st.transport()
            measure(t)
            push(t, cid)
            pull(t, cid)
            state = "idle"
            error = ""
            lastSyncAt = store.nowIso()
            val known = Json.parse(store.meta("synced_with") ?: "[]").asList()
            if (cid !in known) store.tx { store.setMeta("synced_with", Json.dump(known + cid)) }
            backoffMs = 0
        } catch (e: Offline) {
            state = "offline"
            backoff()
        } catch (r: Refused) {
            state = "error"
            error = r.message ?: ""
            backoff()
        } finally {
            store.releaseLease(holder)
        }
        val s = status()
        publish()
        s
    }

    private fun backoff() {
        backoffMs = minOf(BACKOFF_MAX_MS, maxOf(1_000L, backoffMs * 2))
    }

    /** §3.2 offset: the controller's `time`, half the round trip as the
     *  error, kept when the error is under 2 s. */
    private fun measure(t: Transport, force: Boolean = false) {
        if (!force && offsetAtNanos != 0L && System.nanoTime() - offsetAtNanos < TimeUnit.MILLISECONDS.toNanos(OFFSET_EVERY_MS)) return
        val t0 = st.store.clock()
        val (status, ans) = t.get("api/shack/v1/")
        val t1 = st.store.clock()
        if (status != 200 || "time" !in ans) {
            check(status, ans)
            return
        }
        val err = (t1 - t0) / 2
        if (err < MAX_OFFSET_ERROR_MS) {
            val offset = Hlc.parseIsoMs(ans["time"].asStr()) - (t0 + t1) / 2
            st.store.tx { st.store.setMeta("clock_offset_ms", offset) }
        }
        offsetAtNanos = System.nanoTime()
    }

    private fun push(t: Transport, cid: String) {
        val store = st.store
        val rollback = LinkedHashSet<Pair<String, String>>()
        // Bounded so a controller that keeps answering without confirming
        // anything cannot spin this loop; each pass moves up to 500 records.
        for (attempt in 0 until 1000) {
            val batch = store.dirty(500)
            val keys = batch.map { (r, _) -> r["collection"] as String to r["id"] as String }.toSet()
            val dirty = store.dirtyKeys()
            // Ops travel with their record's state, or alone once that state
            // is confirmed; an op whose state is still waiting for a later
            // batch would only be ignored.
            val ops = store.unackedOps(cid, 2000).filter {
                val k = it["collection"] as String to it["id"] as String
                k in keys || k !in dirty
            }
            if (batch.isEmpty() && ops.isEmpty()) break
            val body = mapOf("storeId" to store.storeId, "records" to batch.map { it.first }, "ops" to ops)
            val (status, ans) = t.post("api/shack/v1/sync/push", body)
            check(status, ans)
            val revs = batch.associate { (r, rev) -> (r["collection"] as String to r["id"] as String) to rev }
            val acked = ArrayList<Pair<Pair<String, String>, Long>>()
            var ahead = false
            val rejectedOps = ArrayList<String>()
            for (res in ans["records"].asList().map { it.asMap() }) {
                val key = res["collection"].asStr() to res["id"].asStr()
                val s = res["status"].asStr()
                val code = res["code"].asStr()
                if ((s == "merged" || s == "unchanged") && key in revs) {
                    acked.add(key to revs.getValue(key))
                } else if (s == "rejected" && code == "clock_ahead") {
                    ahead = true
                } else if (s == "rejected" && code == "level_too_low") {
                    // §4.2: roll the local copy back to the controller's on
                    // the next pull; its ops are never pushed again.
                    rollback.add(key)
                    acked.add(key to (revs[key] ?: -1))
                    rejectedOps += ops.filter { (it["collection"] to it["id"]) == key }.map { it["opId"] as String }
                    profileRejected = true
                }
            }
            val done = ans["ops"].asMap()
            val accepted = done["accepted"].asList().map { it.asStr() }
            val duplicate = done["duplicate"].asList().map { it.asStr() }
            store.ack(cid, acked, accepted + duplicate, rejectedOps)
            if (ahead) {
                // §3.3: re-measure, reset the HLC to the corrected clock,
                // re-stamp what is unacknowledged, push again.
                measure(t, force = true)
                store.restampUnacked(cid)
                continue
            }
            if (acked.isEmpty() && accepted.isEmpty() && duplicate.isEmpty()) break   // nothing moved: do not spin
        }
        if (rollback.isNotEmpty()) rollback(t, rollback)
    }

    private fun rollback(t: Transport, keys: Set<Pair<String, String>>) {
        val (status, ans) = t.get("api/shack/v1/sync/pull", mapOf("since" to 0, "limit" to 500, "collections" to "profile"))
        check(status, ans)
        val theirs = ans["records"].asList().map { it.asMap() }.associateBy { it["collection"].asStr() to it["id"].asStr() }
        val recs = keys.map { key ->
            // The controller has no profile yet: the station's profile is the
            // controller's when paired, so the local edit goes (it stays in
            // the local ops as history).
            theirs[key] ?: mapOf("collection" to key.first, "id" to key.second, "fields" to emptyMap<String, Any?>(),
                "fieldHlc" to emptyMap<String, String>(), "deletedHlc" to null, "createdAt" to "", "origin" to emptyMap<String, Any?>())
        }
        st.notify(st.store.mergeRemote(recs, keys))
    }

    private fun pull(t: Transport, cid: String) {
        val store = st.store
        for (restart in 0 until 2) {
            val cur = store.cursor(cid)
            var epoch: String? = cur?.first
            var since: Long = cur?.second ?: 0
            while (true) {
                val (status, ans) = t.get("api/shack/v1/sync/pull", mapOf("since" to since, "epoch" to epoch,
                    "limit" to pageLimit, "collections" to "logbook,profile"))
                if (status == 409 && ans["code"] == "epoch_changed") {
                    // §6.2: restored from a backup. Drop the cursor, push
                    // everything, pull from the start.
                    store.dropCursor(cid)
                    store.markAllDirty()
                    push(t, cid)
                    break
                }
                check(status, ans)
                st.notify(store.mergeRemote(ans["records"].asList().map { it.asMap() }))
                since = if ("next" in ans) ans["next"].asLong(since) else since
                epoch = ans["epoch"] as? String ?: epoch
                store.setCursor(cid, epoch ?: "", since)
                afterPullPage?.invoke(ans)
                if (ans["more"] != true) {
                    // Anything merged that the controller lacks goes back.
                    if (store.pendingCount() > 0) push(t, cid)
                    return
                }
            }
        }
    }

    internal class Refused(val status: Int, message: String, val code: String) : Exception(message)

    companion object {
        const val DEBOUNCE_MS = 2_000L
        const val PERIOD_MS = 60_000L
        const val BACKOFF_MAX_MS = 300_000L
        const val OFFSET_EVERY_MS = 3_600_000L
        const val MAX_OFFSET_ERROR_MS = 2_000L

        internal fun check(status: Int, ans: Map<String, Any?>) {
            if (status >= 400) {
                throw Refused(status, ans["error"].asStr().ifEmpty { "The controller answered $status." }, ans["code"].asStr())
            }
        }
    }
}
