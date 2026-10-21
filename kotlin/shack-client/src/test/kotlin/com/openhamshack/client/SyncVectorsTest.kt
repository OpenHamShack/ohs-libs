package com.openhamshack.client

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.DynamicTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestFactory
import org.junit.jupiter.api.io.TempDir
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import java.nio.file.Files

/** The repository's spec/ directory (set by the Gradle build). */
val SPEC: File = File(System.getProperty("shack.spec") ?: "../../spec")

/**
 * Runs spec/vectors/sync/T1-T21 (SYNC.md §8) against ShackStation and the
 * reference controller: the Kotlin runner of the step language in
 * spec/schemas/sync-vector.schema.json, a port of python/shack_client
 * tests/test_vectors.py. The vector files are read from the repository,
 * never copied, so this suite and the Python and Swift ones run the same
 * contract.
 *
 * Time is simulated: one true time advanced by `tick`, and each store or
 * controller sees true time plus its own skew, so clock faults (a device in
 * 1970, a laptop two days fast) are exact and the suite is fast.
 */
class SyncVectorsTest {
    private val vectors = SPEC.resolve("vectors/sync").listFiles { f -> f.name.matches(Regex("T\\d+\\.json")) }!!.sortedBy { it.name }

    private fun load(f: File) = Json.parseObject(f.readText())

    @Test
    fun everyCaseOfTheSpecHasAVector() {
        assertEquals((1..21).map { "T$it" }, vectors.map { load(it)["id"] })
    }

    @TestFactory
    fun syncVectors(): List<DynamicTest> = vectors.filter { load(it)["runner"] == "script" }.map { f ->
        DynamicTest.dynamicTest(f.nameWithoutExtension) {
            val vec = load(f)
            val dir = Files.createTempDirectory("shack-vec-${f.nameWithoutExtension}").toFile()
            val w = World(vec, dir)
            try {
                w.run(vec["steps"].asList().map { it.asMap() })
                for (e in vec["expect"].asList()) w.check(e.asMap())
            } finally {
                w.close()
                dir.deleteRecursively()
            }
        }
    }

    /** T14: a controller that accepts nothing and answers nothing (a socket
     *  listening with no one accepting, so the request hangs as dropped
     *  packets do). Adding a QSO while a sync round is stuck on it returns
     *  within the vector's budget. */
    @Test
    fun t14UnreachableControllerNeverSlowsAWrite(@TempDir tmp: File) {
        val vec = load(SPEC.resolve("vectors/sync/T14.json"))
        ServerSocket(0, 1, InetAddress.getLoopbackAddress()).use { hole ->
            val url = "http://127.0.0.1:${hole.localPort}/"
            val st = ShackStation.open(File(tmp, "a/station.db"), "shack-vector", "0.0", JdbcSqlite.OPENER, background = false,
                transports = { base, tok -> HttpTransport(base, tok, 1_000) })
            try {
                st.vault.write(mapOf("controllerId" to "c", "baseUrl" to url, "token" to "shackdev_x", "deviceId" to "",
                    "user" to "", "maxLevel" to "transmit", "name" to "hole"))
                st.sync.now()
                Thread.sleep(50)
                var worst = 0L
                for (i in 0 until 20) {
                    val t0 = System.nanoTime()
                    st.logbook.add(mapOf("call" to "DL${i}ABC", "start" to "2026-10-09T10:12:30Z"))
                    worst = maxOf(worst, System.nanoTime() - t0)
                }
                assertTrue(worst / 1_000_000 < vec["budgetMs"].asLong(), "slowest add took ${worst / 1_000_000} ms")
                val deadline = System.currentTimeMillis() + 5_000
                while (st.sync.status().state != "offline" && System.currentTimeMillis() < deadline) Thread.sleep(50)
                assertEquals("offline", st.sync.status().state)
            } finally {
                st.close()
            }
        }
    }

    /** T18: separate JVM processes writing one store file, then the lease. */
    @Test
    fun t18TwoProcessesOneStore(@TempDir tmp: File) {
        val vec = load(SPEC.resolve("vectors/sync/T18.json"))
        val path = File(tmp, "shared/station.db")
        ShackStation.open(path, "shack-vector", "0.0", JdbcSqlite.OPENER, background = false).close()
        val java = File(System.getProperty("java.home"), "bin/java").path
        val n = vec["processes"].asLong().toInt()
        val per = vec["perProcess"].asLong().toInt()
        val procs = (0 until n).map { k ->
            ProcessBuilder(java, "-cp", System.getProperty("java.class.path"), T18Writer::class.java.name,
                path.path, per.toString(), "P${k}A").inheritIO().start()
        }
        for (p in procs) assertEquals(0, p.waitFor())
        val st = ShackStation.open(path, "shack-vector", "0.0", JdbcSqlite.OPENER, background = false)
        try {
            assertEquals(n * per, st.logbook.count())
            val stamps = st.store.query("SELECT hlc FROM ops").map { it[0] as String }
            assertEquals(stamps.size, stamps.toSet().size, "two edits shared a stamp")
            // Only one process syncs at a time: the lease.
            ShackStation.open(path, "shack-vector", "0.0", JdbcSqlite.OPENER, background = false).use { other ->
                assertTrue(st.store.takeLease(st.sync.holder))
                assertTrue(!other.store.takeLease(other.sync.holder))
                st.store.releaseLease(st.sync.holder)
                assertTrue(other.store.takeLease(other.sync.holder))
            }
        } finally {
            st.close()
        }
    }
}

/** The T18 writer process: adds `n` contacts to the store at `path`. */
object T18Writer {
    @JvmStatic
    fun main(args: Array<String>) {
        ShackStation.open(File(args[0]), "shack-vector", "0.0", JdbcSqlite.OPENER, background = false).use { s ->
            for (i in 0 until args[1].toInt()) s.logbook.add(mapOf("call" to "${args[2]}${i}X", "start" to "2026-10-09T10:12:30Z"))
        }
    }
}

class World(private val vec: Map<String, Any?>, private val tmp: File) {
    private var t = Hlc.parseIsoMs(vec["start"].asStr())
    private val skew = HashMap<String, Long>()
    private val net = Network()
    val ctls = LinkedHashMap<String, ReferenceController>()
    val stores = LinkedHashMap<String, ShackStation>()
    private val calls = HashMap<String, Int>()
    private val marks = HashMap<String, Int>()
    private val backups = HashMap<String, ByteArray>()
    private var lastPair: PairResult? = null
    private var page = 0

    init {
        for ((name, c) in vec["controllers"].asMap()) {
            val cm = c.asMap()
            skew[name] = cm["skewMs"].asLong()
            @Suppress("UNCHECKED_CAST")
            val users = cm["users"] as Map<String, Map<String, Any?>>?
            val ctl = ReferenceController(File(tmp, "$name.db"), cm["name"].asStr().ifEmpty { name }, nowMs = clock(name), users = users)
            ctls[name] = ctl
            net.controllers[url(name)] = ctl
        }
        for ((name, s) in vec["stores"].asMap()) {
            val sm = s.asMap()
            skew[name] = sm["skewMs"].asLong()
            if ("clock" in sm) skew[name] = Hlc.parseIsoMs(sm["clock"].asStr()) - t
            calls[name] = 0
            val inner = net.factory()
            val counted = TransportFactory { base, token ->
                val tr = inner.create(base, token)
                Transport { m, p, b, q -> calls[name] = calls.getValue(name) + 1; tr.request(m, p, b, q) }
            }
            stores[name] = ShackStation.open(File(tmp, "$name/station.db"), "shack-vector", "0.0", JdbcSqlite.OPENER,
                clock = clock(name), transports = counted, background = false)
        }
    }

    private fun url(name: String) = "http://${name.lowercase()}.test/"
    private fun clock(name: String): () -> Long = { t + (skew[name] ?: 0) }

    fun run(steps: List<Map<String, Any?>>) {
        for (s in steps) step(s)
    }

    private fun store(s: Map<String, Any?>) = stores.getValue(s["store"].asStr())
    private fun ctl(s: Map<String, Any?>) = ctls.getValue(s["controller"].asStr())

    private fun step(s: Map<String, Any?>) {
        when (val op = s["do"].asStr()) {
            "tick" -> t += s["ms"].asLong()
            "skew" -> {
                skew[s["who"].asStr()] = s["ms"].asLong()
                if (s["resetOffset"] == true) stores.getValue(s["who"].asStr()).store.tx { stores.getValue(s["who"].asStr()).store.setMeta("clock_offset_ms", 0) }
            }
            "add" -> {
                var id = s["id"] as String?
                if ("idFromPage" in s) {
                    page += 1
                    id = s["idFromPage"].asStr() + page.toString().padStart(12, '0')
                }
                store(s).logbook.add(s["fields"].asMap(), id)
            }
            "addMany" -> store(s).logbook.addMany((0 until s["count"].asLong().toInt()).map { i ->
                mapOf("uuid" to s["idPrefix"].asStr() + i.toString().padStart(12, '0'), "call" to "T$i", "start" to "2026-10-09T09:00:00Z")
            })
            "update" -> {
                val fields = if ("fields" in s) s["fields"].asMap() else mapOf(s["fieldsFromPage"].asStr() to "page $page")
                store(s).logbook.update(s["id"].asStr(), fields)
            }
            "delete" -> store(s).logbook.delete(s["id"].asStr())
            "profile" -> store(s).profile.update(s["set"].asMap())
            "pair" -> {
                val st = store(s)
                if ("pageLimit" in s) st.sync.pageLimit = s["pageLimit"].asLong().toInt()
                if ("duringEachPage" in s) {
                    val steps = s["duringEachPage"].asList().map { it.asMap() }
                    st.sync.afterPullPage = { run(steps) }
                }
                val res = st.pair(url(s["controller"].asStr()), s["user"] as String? ?: "brian", s["password"] as String? ?: "pw",
                    deviceName = s["store"].asStr())
                st.sync.afterPullPage = null
                val got = if (res is PairResult.NeedsDecision) "needsDecision" else "paired"
                assertEquals(s["expect"] as String? ?: "paired", got, "pair ${s["store"]} -> ${s["controller"]}")
                lastPair = res
            }
            "resolve" -> store(s).resolve(lastPair!!,
                if (s["choice"] == "use-controller") ShackStation.Choice.USE_CONTROLLER else ShackStation.Choice.MERGE)
            "unpair" -> store(s).unpair()
            "sync" -> {
                val st = store(s)
                if (s["fromSeq"] == 0L) st.store.dropCursor(st.pairingInfo()!!["controllerId"].asStr())
                val status = st.sync.runOnce()
                assertEquals(s["expectState"] as String? ?: "idle", status.state, status.toString())
            }
            "dropNextPushAnswer" -> ctl(s).dropNextPushAnswer = true
            "offline" -> ctl(s).offline = s["value"] == true
            "backup" -> backups[s["name"].asStr()] = ctl(s).sync.snapshot()
            "restore" -> ctl(s).sync.restore(backups.getValue(s["name"].asStr()))
            "inject" -> {
                val (status, ans) = ctl(s).sync.push(mapOf("records" to listOf(s["record"])))
                assertTrue(status == 200 && ans["records"].asList()[0].asMap()["status"] == "merged", ans.toString())
            }
            "markRequests" -> marks[s["store"].asStr()] = calls.getValue(s["store"].asStr())
            else -> throw AssertionError("unknown step $op")
        }
    }

    // ---------------------------------------------------------- views
    private fun records(who: String, collection: String): Map<String, Map<String, Any?>> =
        if (who in ctls) ctls.getValue(who).sync.all(collection).associate { it["id"] as String to it.filterKeys { k -> k != "seq" } }
        else stores.getValue(who).store.all(collection).associateBy { it["id"] as String }

    private fun stamps(who: String) = listOf("logbook", "profile").flatMap { c -> records(who, c).values.flatMap { Merge.stamps(it) } }

    // ---------------------------------------------------------- expectations
    fun check(e: Map<String, Any?>) {
        val (kind, a0) = e.entries.single()
        val a = a0.asMap()
        val where = a["where"].asList().map { it.asStr() }
        when (kind) {
            "record" -> for (who in where) {
                val r = records(who, a["collection"].asStr())[a["id"]] ?: throw AssertionError("$who lacks ${a["id"]}")
                if ("live" in a) assertEquals(a["live"], Merge.isLive(r), "$who: live")
                for ((k, v) in a["fields"].asMap()) assertEquals(v, Merge.fields(r)[k], "$who: $k")
                for ((k, v) in a["extra"].asMap()) assertEquals(v, r[k], "$who: envelope $k")
            }
            "count" -> for (who in where) {
                assertEquals(a["live"].asLong().toInt(), records(who, a["collection"].asStr()).values.count(Merge::isLive), "$who live records")
            }
            "countAtLeast" -> for (who in where) {
                val n = records(who, a["collection"].asStr()).values.count(Merge::isLive)
                assertTrue(n >= a["live"].asLong(), "$who has $n live records, expected at least ${a["live"]}")
            }
            "converged" -> {
                fun view(who: String) = records(who, a["collection"].asStr()).mapValues { (_, r) ->
                    listOf(Merge.isLive(r), Merge.fields(r), Merge.fieldHlc(r), Merge.deletedHlc(r))
                }
                val first = view(where[0])
                for (who in where.drop(1)) {
                    val other = view(who)
                    assertEquals(first.keys, other.keys, "${where[0]} and $who hold different records")
                    val diff = first.keys.filter { first[it] != other[it] }
                    assertTrue(diff.isEmpty(), "${where[0]} and $who differ on ${diff.take(3)}: ${diff.firstOrNull()?.let { first[it] }} / ${diff.firstOrNull()?.let { other[it] }}")
                }
            }
            "history" -> {
                val h = ctls.getValue(a["controller"].asStr()).sync.history("logbook", a["id"].asStr())
                if ("edits" in a) assertEquals(a["edits"].asLong().toInt(), h.size, h.toString())
                if ("lostFields" in a) assertEquals(a["lostFields"].asList().map { it.asStr() }.sorted(),
                    h.flatMap { it["lost"].asList().map { x -> x.asStr() } }.toSortedSet().toList(), h.toString())
                if ("kinds" in a) assertEquals(a["kinds"].asList().map { it.asStr() }.sorted(), h.map { it["kind"].asStr() }.sorted())
            }
            "status" -> {
                val st = stores.getValue(a["store"].asStr()).sync.status()
                val m = mapOf("state" to st.state, "controller" to st.controller, "lastSyncAt" to st.lastSyncAt,
                    "pendingRecords" to st.pendingRecords.toLong(), "error" to st.error, "profileRejected" to st.profileRejected)
                for ((k, v) in a) if (k != "store") assertEquals(v, m[k], "status $k: $st")
            }
            "noFutureHlc" -> {
                val c = ctls.getValue(a["controller"].asStr())
                val limit = c.nowMs() + Hlc.MAX_AHEAD_MS
                val bad = stamps(a["controller"].asStr()).filter { Hlc.wallOf(it) > limit }
                assertTrue(bad.isEmpty(), bad.toString())
            }
            "hlcAtLeastFloor" -> {
                val s = stamps(a["store"].asStr())
                assertTrue(s.isNotEmpty() && s.all { Hlc.wallOf(it) >= Hlc.CLOCK_FLOOR_MS }, s.toString())
            }
            "archive" -> {
                val d = stores.getValue(a["store"].asStr()).store.path.absoluteFile.parentFile
                val names = d.list()!!.toList()
                assertTrue(names.any { it.startsWith("station-") && it.endsWith(".db") } &&
                    names.any { it.startsWith("station-") && it.endsWith(".adi") }, names.toString())
            }
            "requests" -> assertEquals(a["sinceMark"].asLong().toInt(), calls.getValue(a["store"].asStr()) - marks.getValue(a["store"].asStr()))
            "profile" -> for (who in where) {
                val got = stores.getValue(who).profile.get()
                assertTrue(contains(got, a["has"].asMap()), "$who: $got")
            }
            else -> throw AssertionError("unknown expectation $kind")
        }
    }

    private fun contains(big: Map<String, Any?>, small: Map<String, Any?>): Boolean = small.all { (k, v) ->
        k in big && (if (v is Map<*, *>) big[k] is Map<*, *> && contains(big[k].asMap(), v.asMap()) else big[k] == v)
    }

    fun close() {
        stores.values.forEach { it.close() }
        ctls.values.forEach { it.close() }
    }
}
