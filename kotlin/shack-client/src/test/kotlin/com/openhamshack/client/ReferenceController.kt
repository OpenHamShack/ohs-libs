package com.openhamshack.client

import com.sun.net.httpserver.HttpServer
import java.io.File
import java.net.InetSocketAddress
import java.net.URLDecoder
import java.security.SecureRandom
import java.util.Base64
import java.util.UUID

/**
 * A reference controller for the tests: a line-for-line port of
 * python/shack_client controller.py (SyncService: push, pull, epochs,
 * sequence numbers, history) and testing.py (the routes a client uses, fault
 * knobs), so the Kotlin client runs the shared vectors against the same
 * server rules the Python client is tested against. It is test code: the
 * real controller is the Dock360 edition, which imports the Python one.
 */
class SyncService(path: File, val controllerId: String, val nowMs: () -> Long) : AutoCloseable {
    private val path = path.path
    val node = "ctl_" + controllerId.replace("-", "").take(12)
    private val db = JdbcSqlite(this.path)
    private val lock = Any()

    init {
        for (s in SCHEMA.split(";").map { it.trim() }.filter { it.isNotEmpty() }) db.exec(s)
        if (meta("epoch") == null) {
            setMeta("epoch", newEpoch())
            setMeta("latest_seq", "0")
            setMeta("hlc_wall", "0")
            setMeta("hlc_count", "0")
        }
    }

    private fun meta(key: String) = db.query("SELECT value FROM meta WHERE key=?", key).firstOrNull()?.get(0) as String?
    private fun setMeta(key: String, v: Any) =
        db.exec("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, v.toString())

    val epoch: String get() = meta("epoch")!!
    val latestSeq: Long get() = meta("latest_seq")!!.toLong()

    fun restoredFromBackup(): String = synchronized(lock) { newEpoch().also { setMeta("epoch", it) } }

    fun info(): Map<String, Any?> = mapOf("services" to mapOf("profile" to 1, "logbook" to 1, "sync" to 1),
        "controllerId" to controllerId, "time" to Hlc.isoMs(nowMs()))

    private fun row(r: Array<Any?>): MutableMap<String, Any?> {
        val rec = linkedMapOf<String, Any?>("collection" to r[0], "id" to r[1], "fields" to Json.parse(r[2] as String),
            "fieldHlc" to Json.parse(r[3] as String), "deletedHlc" to r[4], "createdAt" to r[5],
            "origin" to Json.parse(r[7] as String))
        rec.putAll(Json.parse(r[8] as String).asMap())
        rec["seq"] = r[9].asLong()
        return rec
    }

    fun get(collection: String, id: String): MutableMap<String, Any?>? =
        db.query("SELECT $COLS FROM records WHERE collection=? AND id=?", collection, id).firstOrNull()?.let(::row)

    fun all(collection: String? = null): List<Map<String, Any?>> = synchronized(lock) {
        if (collection == null) db.query("SELECT $COLS FROM records ORDER BY seq").map(::row)
        else db.query("SELECT $COLS FROM records WHERE collection=? ORDER BY seq", collection).map(::row)
    }

    private fun store(rec: Record, seq: Long) {
        db.exec("INSERT INTO records(collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra, seq) " +
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collection, id) DO UPDATE SET fields=excluded.fields, " +
            "field_hlc=excluded.field_hlc, deleted_hlc=excluded.deleted_hlc, created_at=excluded.created_at, " +
            "updated_at=excluded.updated_at, origin=excluded.origin, extra=excluded.extra, seq=excluded.seq",
            rec["collection"], rec["id"], Json.dump(Merge.fields(rec)), Json.dump(Merge.fieldHlc(rec)), Merge.deletedHlc(rec),
            rec["createdAt"].asStr(), Hlc.isoMs(nowMs()), Json.dump(rec["origin"].asMap()), Json.dump(Merge.extraOf(rec)), seq)
    }

    fun push(body: Map<String, Any?>, canEditProfile: Boolean = true): Pair<Int, Map<String, Any?>> = synchronized(lock) {
        val recs = body["records"].asList().map { it.asMap() }
        val ops = body["ops"].asList().map { it.asMap() }
        if (recs.size > MAX_BATCH || ops.size > 4 * MAX_BATCH) {
            return 400 to mapOf("error" to "A sync push carries at most 500 records.", "code" to "bad_request")
        }
        val now = nowMs()
        val limit = now + Hlc.MAX_AHEAD_MS
        val results = ArrayList<Map<String, Any?>>()
        val rejected = HashSet<Pair<Any?, Any?>>()
        db.immediate { pushRecords(recs, now, limit, results, rejected, canEditProfile) }
        val (accepted, duplicate, ignored) = db.immediate { pushOps(ops, now, limit, rejected) }
        200 to mapOf("controllerId" to controllerId, "epoch" to epoch, "records" to results,
            "ops" to mapOf("accepted" to accepted, "duplicate" to duplicate, "ignored" to ignored))
    }

    private fun pushRecords(recs: List<Map<String, Any?>>, now: Long, limit: Long, results: MutableList<Map<String, Any?>>,
                            rejected: MutableSet<Pair<Any?, Any?>>, canEditProfile: Boolean) {
        for (rec in recs) {
            val key = rec["collection"] to rec["id"]
            val bad = invalid(rec)
            if (bad.isNotEmpty()) {
                results.add(mapOf("collection" to key.first, "id" to key.second, "status" to "rejected", "code" to "bad_request", "error" to bad))
                rejected.add(key); continue
            }
            val stamps = Merge.stamps(rec)
            if (stamps.any { Hlc.wallOf(it) > limit }) {
                results.add(mapOf("collection" to key.first, "id" to key.second, "status" to "rejected", "code" to "clock_ahead",
                    "serverTime" to Hlc.isoMs(now)))
                rejected.add(key); continue
            }
            if (key.first == "profile" && !canEditProfile) {
                results.add(mapOf("collection" to key.first, "id" to key.second, "status" to "rejected", "code" to "level_too_low",
                    "error" to "Changing the station profile needs the Transmit level."))
                rejected.add(key); continue
            }
            db.exec("SAVEPOINT rec")
            try {
                val stored = get(key.first as String, key.second as String)
                val incoming = rec.filterKeys { it != "seq" }
                val merged = Merge.merge(stored?.filterKeys { it != "seq" }, incoming)
                if (stored != null && Merge.same(merged, stored)) {
                    results.add(mapOf("collection" to key.first, "id" to key.second, "status" to "unchanged",
                        "seq" to stored["seq"], "lost" to emptyList<String>()))
                } else {
                    val seq = latestSeq + 1
                    setMeta("latest_seq", seq)
                    store(merged, seq)
                    results.add(mapOf("collection" to key.first, "id" to key.second, "status" to "merged", "seq" to seq,
                        "lost" to lostAgainst(incoming, stored)))
                }
                observe(stamps)
                db.exec("RELEASE rec")
            } catch (e: Throwable) {
                db.exec("ROLLBACK TO rec")
                db.exec("RELEASE rec")
                throw e
            }
        }
    }

    private fun pushOps(ops: List<Map<String, Any?>>, now: Long, limit: Long, rejected: Set<Pair<Any?, Any?>>):
        Triple<List<String>, List<String>, List<String>> {
        val accepted = ArrayList<String>()
        val duplicate = ArrayList<String>()
        val ignored = ArrayList<String>()
        for (op in ops) {
            val key = op["collection"] to op["id"]
            val oid = op["opId"].asStr()
            if (oid.isEmpty() || key in rejected || Hlc.wallOf(op["hlc"] as String?) > limit) continue
            if (db.query("SELECT 1 FROM ops WHERE op_id=?", oid).isNotEmpty()) { duplicate.add(oid); continue }
            val state = get(key.first.asStr(), key.second.asStr())
            if (state == null) { ignored.add(oid); continue }
            db.exec("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at, received_at, received_seq, lost) " +
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", oid, key.first, key.second, op["kind"].asStr().ifEmpty { "update" },
                Json.dump(op["fields"].asMap()), op["hlc"], Json.dump(op["origin"].asMap()), op["createdAt"].asStr(),
                Hlc.isoMs(now), state["seq"], Json.dump(Merge.lostFields(op, state)))
            accepted.add(oid)
        }
        return Triple(accepted, duplicate, ignored)
    }

    private fun invalid(rec: Map<String, Any?>): String {
        if (rec["collection"] !in setOf("logbook", "profile") || rec["id"].asStr().isEmpty()) {
            return "A sync record needs a collection (logbook or profile) and an id."
        }
        if (rec["fields"] !is Map<*, *> || rec["fieldHlc"] !is Map<*, *>) return "A sync record needs fields and fieldHlc objects."
        return try {
            Merge.stamps(rec).forEach { Hlc.parse(it) }
            ""
        } catch (e: Exception) {
            "A sync record carries a stamp that is not a hybrid logical clock."
        }
    }

    private fun lostAgainst(incoming: Record, stored: Record?): List<String> {
        if (stored == null) return emptyList()
        val sh = Merge.fieldHlc(stored)
        return Merge.fieldHlc(incoming).filter { (f, h) ->
            val s = sh[f]
            s != null && s > h && Hlc.nodeOf(s) != Hlc.nodeOf(h)
        }.keys.sorted()
    }

    private fun observe(stamps: List<String>) {
        var w = meta("hlc_wall")!!.toLong()
        var c = meta("hlc_count")!!.toInt()
        val now = Hlc.corrected(nowMs(), 0)
        for (s in stamps) { val st = Hlc.receive(w, c, s, now); w = st.wall; c = st.count }
        setMeta("hlc_wall", w)
        setMeta("hlc_count", c)
    }

    fun pull(since: Long, epoch: String?, limit: Int, collections: List<String>?): Pair<Int, Map<String, Any?>> = synchronized(lock) {
        val latest = latestSeq
        if ((epoch != null && epoch != this.epoch) || since > latest) {
            return 409 to mapOf("error" to "This controller's sync history was restored from a backup; syncing again from the start.",
                "code" to "epoch_changed", "epoch" to this.epoch)
        }
        val lim = limit.coerceIn(1, MAX_BATCH)
        var q = "SELECT $COLS FROM records WHERE seq > ?"
        val args = arrayListOf<Any?>(since)
        if (!collections.isNullOrEmpty()) {
            q += " AND collection IN (" + collections.joinToString(",") { "?" } + ")"
            args.addAll(collections)
        }
        args.add((lim + 1).toLong())
        var rows = db.query("$q ORDER BY seq LIMIT ?", *args.toTypedArray())
        val more = rows.size > lim
        rows = rows.take(lim)
        val recs = rows.map(::row)
        val next = if (recs.isNotEmpty()) recs.last()["seq"] else if (!more) latest else since
        200 to mapOf("controllerId" to controllerId, "epoch" to this.epoch, "latestSeq" to latest, "records" to recs,
            "next" to next, "more" to more)
    }

    fun history(collection: String, id: String): List<Map<String, Any?>> = synchronized(lock) {
        db.query("SELECT op_id, hlc, kind, fields, origin, received_at, lost FROM ops WHERE collection=? AND id=? ORDER BY hlc",
            collection, id).map { r ->
            mapOf("opId" to r[0], "hlc" to r[1], "kind" to r[2], "fields" to Json.parse(r[3] as String),
                "origin" to Json.parse(r[4] as String), "receivedAt" to r[5], "lost" to Json.parse(r[6] as String).asList())
        }
    }

    /** The controller's own write, stamped with its own HLC (§10). */
    fun edit(collection: String, id: String, kind: String, fields: Map<String, Any?>, origin: Map<String, Any?>): Record =
        synchronized(lock) {
            db.immediate {
                val (st, stamp) = Hlc.local(meta("hlc_wall")!!.toLong(), meta("hlc_count")!!.toInt(), node, Hlc.corrected(nowMs(), 0))
                setMeta("hlc_wall", st.wall)
                setMeta("hlc_count", st.count)
                val base = get(collection, id)?.filterKeys { it != "seq" }
                val rec = Merge.applyEdit(base, collection, id, kind, fields, stamp, origin, Hlc.isoMs(nowMs()))
                val seq = latestSeq + 1
                setMeta("latest_seq", seq)
                store(rec, seq)
                db.exec("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at, received_at, received_seq, lost) " +
                    "VALUES (?,?,?,?,?,?,?,?,?,?,'[]')", UUID.randomUUID().toString(), collection, id, kind, Json.dump(fields), stamp,
                    Json.dump(origin), Hlc.isoMs(nowMs()), Hlc.isoMs(nowMs()), seq)
                rec
            }
        }

    fun snapshot(): ByteArray = synchronized(lock) {
        val tmp = File("$path.snap-${UUID.randomUUID()}")
        try {
            db.backupTo(tmp.path)
            tmp.readBytes()
        } finally {
            tmp.delete()
        }
    }

    /** Restore a snapshot and mint a new epoch, as the platform's restore
     *  path must (§6.2). */
    fun restore(snapshot: ByteArray) {
        synchronized(lock) {
            val tmp = File("$path.restore-${UUID.randomUUID()}")
            try {
                tmp.writeBytes(snapshot)
                db.restoreFrom(tmp.path)
            } finally {
                tmp.delete()
            }
        }
        restoredFromBackup()
    }

    override fun close() = db.close()

    companion object {
        const val MAX_BATCH = 500
        private const val COLS = "collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra, seq"
        private val rnd = SecureRandom()
        fun newEpoch() = "e_" + (1..8).map { "0123456789abcdef"[rnd.nextInt(16)] }.joinToString("")

        private val SCHEMA = """
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
            CREATE INDEX IF NOT EXISTS ops_by_record ON ops(collection, id, hlc)
        """.trimIndent()
    }
}

/** The test transport's "the request went out, the answer never came". */
class Dropped(message: String) : Offline(message)

class ReferenceController(path: File, val name: String = "GM4BRI", val controllerId: String = UUID.randomUUID().toString(),
                          val nowMs: () -> Long = Hlc::systemMs, users: Map<String, Map<String, Any?>>? = null) : AutoCloseable {
    val sync = SyncService(path, controllerId) { nowMs() }
    /** username -> {password, totp (optional), transmit} */
    val users: Map<String, Map<String, Any?>> = users ?: mapOf("brian" to mapOf("password" to "pw", "transmit" to true))
    val devices = LinkedHashMap<String, MutableMap<String, Any?>>()
    private val tvPairs = LinkedHashMap<String, MutableMap<String, Any?>>()
    @Volatile var dropNextPushAnswer = false
    @Volatile var offline = false
    val requests: MutableList<Pair<String, String>> = java.util.Collections.synchronizedList(ArrayList())
    private var server: HttpServer? = null

    fun handle(method: String, pathAndQuery: String, headers: Map<String, String>, body: Map<String, Any?>?): Pair<Int, Map<String, Any?>> {
        requests.add(method to pathAndQuery)
        val route = pathAndQuery.substringBefore('?').trimStart('/')
        val q = pathAndQuery.substringAfter('?', "").split("&").filter { it.isNotEmpty() }.associate {
            URLDecoder.decode(it.substringBefore('='), "UTF-8") to URLDecoder.decode(it.substringAfter('=', ""), "UTF-8")
        }
        val b = body ?: emptyMap()
        when {
            method == "GET" && route == ".well-known/openhamshack" -> return 200 to wellKnown()
            method == "POST" && route == "api/shack/token" -> return token(b)
            method == "POST" && route == "api/shack/pair/start" -> return tvStart(b)
            method == "POST" && route == "api/shack/pair/poll" -> return tvPoll(b)
        }
        val auth = headers.entries.firstOrNull { it.key.equals("authorization", ignoreCase = true) }?.value ?: ""
        val dev = if (auth.startsWith("Bearer ")) devices[auth.substring(7)] else null
        if (dev == null) return 401 to mapOf("error" to "Sign in to this controller first.", "code" to "credentials_required")
        if (dev["revoked"] == true) return 401 to mapOf("error" to "This device has been removed from $name.", "code" to "token_revoked")
        return when {
            method == "GET" && route == "api/shack/v1/" -> 200 to sync.info()
            method == "POST" && route == "api/shack/v1/sync/push" -> sync.push(b, dev["transmit"] == true)
            method == "GET" && route == "api/shack/v1/sync/pull" -> sync.pull(q["since"]?.toLong() ?: 0, q["epoch"]?.ifEmpty { null },
                q["limit"]?.toInt() ?: 500, q["collections"]?.split(",")?.filter { it.isNotEmpty() }?.ifEmpty { null })
            method == "POST" && route == "api/shack/v1/pair/approve" -> tvApprove(b, dev)
            else -> 404 to mapOf("error" to "There is nothing at that address on this controller.", "code" to "not_found")
        }
    }

    fun wellKnown(): Map<String, Any?> = mapOf("openhamshack" to 1, "contract" to "1.0", "controllerId" to controllerId,
        "name" to name, "version" to "2.0.0-test", "time" to Hlc.isoMs(nowMs()),
        "station" to mapOf("callsign" to name, "locator" to "", "country" to ""),
        "auth" to mapOf("token" to "api/shack/token", "pair" to "api/shack/pair/start", "totp" to "per-user", "anonymousView" to emptyList<String>()),
        "services" to mapOf("base" to "api/shack/v1/", "profile" to 1, "logbook" to 1, "sync" to 1),
        "bandplan" to mapOf("dataVersion" to "", "schema" to 1), "apps" to emptyList<Any>())

    private val rnd = SecureRandom()
    private fun tokenUrlsafe(n: Int) = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(n).also { rnd.nextBytes(it) })
    private fun hex(n: Int) = (1..n * 2).map { "0123456789ABCDEF"[rnd.nextInt(16)] }.joinToString("")

    @Synchronized
    private fun mint(user: String, name: String, kind: String, clientId: String, maxLevel: String): Pair<String, Map<String, Any?>> {
        for (d in devices.values) if (d["clientId"] == clientId && d["user"] == user && d["revoked"] != true) d["revoked"] = true
        val token = "shackdev_" + tokenUrlsafe(32)
        val dev = mutableMapOf<String, Any?>("id" to "dev_" + hex(6), "user" to user, "name" to name, "kind" to kind,
            "clientId" to clientId, "maxLevel" to maxLevel,
            "transmit" to (maxLevel == "transmit" && users[user]?.get("transmit") == true))
        devices[token] = dev
        return token to dev
    }

    private fun token(b: Map<String, Any?>): Pair<Int, Map<String, Any?>> {
        val user = users[b["username"].asStr()]
        if (b["deviceKind"] == "tv") return 400 to mapOf("error" to "A TV pairs with a code, not a password.", "code" to "use_tv_pairing")
        if (user == null || user["password"] != b["password"]) return 401 to mapOf("error" to "The username or password is wrong.", "code" to "bad_credentials")
        if (user["totp"] != null && b["totp"] != user["totp"]) return 401 to mapOf("error" to "Enter the code from your authenticator app.", "code" to "totp_required")
        val (token, dev) = mint(b["username"].asStr(), b["deviceName"].asStr(), b["deviceKind"].asStr().ifEmpty { "desktop" },
            b["clientId"].asStr(), "transmit")
        return 201 to mapOf("token" to token, "deviceId" to dev["id"], "user" to b["username"], "admin" to false,
            "maxLevel" to "transmit", "controllerId" to controllerId)
    }

    private fun tvStart(b: Map<String, Any?>): Pair<Int, Map<String, Any?>> {
        val pid = "pr_" + hex(3)
        val p = mutableMapOf<String, Any?>("pairId" to pid, "pairSecret" to tokenUrlsafe(32),
            "code" to rnd.nextInt(1_000_000).toString().padStart(6, '0'), "state" to "pending", "body" to b)
        tvPairs[pid] = p
        return 200 to mapOf("pairId" to pid, "pairSecret" to p["pairSecret"], "code" to p["code"], "expiresIn" to 300, "pollEvery" to 2)
    }

    private fun tvApprove(b: Map<String, Any?>, dev: Map<String, Any?>): Pair<Int, Map<String, Any?>> {
        val p = tvPairs.values.firstOrNull { it["code"] == b["code"] && it["state"] == "pending" }
            ?: return 404 to mapOf("error" to "No TV is showing that code.", "code" to "not_found")
        if (b["deny"] == true) { p["state"] = "denied"; return 200 to mapOf("approved" to false) }
        val level = if (b["maxLevel"] == "control") "control" else "view"
        val body = p["body"].asMap()
        val (token, tv) = mint(dev["user"].asStr(), body["deviceName"].asStr(), "tv", body["clientId"].asStr(), level)
        p["state"] = "approved"; p["token"] = token; p["device"] = tv
        return 200 to mapOf("approved" to true)
    }

    private fun tvPoll(b: Map<String, Any?>): Pair<Int, Map<String, Any?>> {
        val p = tvPairs[b["pairId"].asStr()]
        if (p == null || p["pairSecret"] != b["pairSecret"]) return 410 to mapOf("error" to "That pairing has expired.", "code" to "pair_expired")
        if (p["state"] == "approved") {
            tvPairs.remove(p["pairId"])
            val d = p["device"].asMap()
            return 200 to mapOf("state" to "approved", "token" to p["token"], "deviceId" to d["id"], "user" to d["user"], "maxLevel" to d["maxLevel"])
        }
        return 200 to mapOf("state" to p["state"])
    }

    fun records(collection: String = "logbook"): Map<String, Map<String, Any?>> = sync.all(collection).associateBy { it["id"] as String }

    /** Start on a loopback port over the JDK's own HTTP server; the base URL. */
    fun serve(): String {
        val s = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        s.createContext("/") { ex ->
            try {
                val raw = ex.requestBody.readBytes()
                val body = if (raw.isEmpty()) null else Json.parseObject(String(raw, Charsets.UTF_8))
                if (offline) { ex.close(); return@createContext }
                val headers = ex.requestHeaders.entries.associate { it.key to (it.value.firstOrNull() ?: "") }
                val (status, ans) = handle(ex.requestMethod, ex.requestURI.rawPath + (ex.requestURI.rawQuery?.let { "?$it" } ?: ""), headers, body)
                if (ex.requestMethod == "POST" && ex.requestURI.path.startsWith("/api/shack/v1/sync/push") && dropNextPushAnswer) {
                    dropNextPushAnswer = false
                    ex.close()   // applied, and the answer is lost
                    return@createContext
                }
                val out = Json.dump(ans).toByteArray(Charsets.UTF_8)
                ex.responseHeaders.add("Content-Type", "application/json")
                ex.sendResponseHeaders(status, out.size.toLong())
                ex.responseBody.use { it.write(out) }
            } catch (e: Exception) {
                ex.close()
            }
        }
        s.start()
        server = s
        return "http://127.0.0.1:${s.address.port}/"
    }

    override fun close() {
        server?.stop(0)
        sync.close()
    }
}

/** In-process controllers by base URL, and the transport factory a ShackStation
 *  takes to reach them without sockets (testing.py Network). */
class Network {
    val controllers = LinkedHashMap<String, ReferenceController>()
    var calls = 0

    fun factory(): TransportFactory = TransportFactory { base, token -> DirectTransport(this, base, token) }
}

class DirectTransport(private val net: Network, private val baseUrl: String, private val token: String?) : Transport {
    override fun request(method: String, path: String, body: Map<String, Any?>?, query: Map<String, Any?>?): Transport.Response {
        net.calls++
        val ctl = net.controllers[baseUrl]
        if (ctl == null || ctl.offline) throw Offline("$baseUrl does not answer")
        var p = path.trimStart('/')
        val q = query?.filterValues { it != null }
        if (!q.isNullOrEmpty()) p += "?" + q.entries.joinToString("&") { (k, v) ->
            java.net.URLEncoder.encode(k, "UTF-8") + "=" + java.net.URLEncoder.encode(v.toString(), "UTF-8")
        }
        val headers = if (token != null) mapOf("Authorization" to "Bearer $token") else emptyMap()
        // A JSON round trip, so nothing shares mutable objects with the
        // controller that a real wire would not.
        val (status, ans) = ctl.handle(method, p, headers, body?.let { Json.parseObject(Json.dump(it)) })
        if (method == "POST" && p.startsWith("api/shack/v1/sync/push") && ctl.dropNextPushAnswer) {
            ctl.dropNextPushAnswer = false
            throw Dropped("the answer to the push was lost")
        }
        return Transport.Response(status, Json.parseObject(Json.dump(ans)))
    }
}
