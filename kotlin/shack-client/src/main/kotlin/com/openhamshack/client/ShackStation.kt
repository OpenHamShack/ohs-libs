package com.openhamshack.client

import java.io.File
import java.time.Instant
import java.util.Locale
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The API apps use (SYNC.md §9.2), a port of python/shack_client
 * station.py: one station per store, with [logbook], [profile] and [sync].
 *
 *     val station = ShackStation.open(File(context.filesDir, "station.db"), app = "remote", opener = androidSqlite)
 *     val qso = station.logbook.add(mapOf("call" to "DL1ABC", "start" to "2026-10-09T10:12:30Z"))
 *     station.profile.update(mapOf("locator" to "IO75RL"))
 *     val result = station.pair(baseUrl, "brian", password, deviceName = "Tab S9")
 *
 * Every call here reads or writes the local store and returns: no call a
 * UI makes does network I/O on its thread, except [pair], [pairTv] and
 * [resolve], which exist to talk to a controller and which an app runs off
 * its UI thread like any other request.
 *
 * Errors a person may see carry a machine `code` for the app's own catalog
 * (I18N.md: no user-visible text in library code that the app cannot
 * translate); the English message is the fallback.
 */
class ShackError(val code: String, message: String) : IllegalArgumentException(message)

data class Qso(
    val uuid: String,
    val fields: Map<String, Any?>,
    val createdAt: String,
    val origin: Map<String, Any?>,
    val deleted: Boolean,
    val hlc: String,
    /** Contacts this one probably duplicates (same call, band, mode within
     *  300 s): advice, not a rule (SERVICES.md §3.1). Filled on [Logbook.add]. */
    val duplicateOf: List<String> = emptyList(),
) {
    companion object {
        fun of(rec: Record): Qso = Qso(rec["id"] as String, Merge.fields(rec), rec["createdAt"].asStr(),
            rec["origin"].asMap(), !Merge.isLive(rec), Merge.stamps(rec).maxOrNull() ?: "")
    }
}

/** A finished pairing. */
data class PairingInfo(val controllerId: String, val name: String, val baseUrl: String, val deviceId: String,
                       val user: String, val maxLevel: String)

/** What [ShackStation.pair] returns. */
sealed class PairResult {
    data class Paired(val pairing: PairingInfo) : PairResult()

    /** §7: this device and the controller both have a log and different
     *  callsigns. The app asks: merge, or use the controller's log. */
    data class NeedsDecision(val pairing: PairingInfo, val localCallsign: String, val controllerCallsign: String,
                             val localCount: Int, val controllerHasLog: Boolean) : PairResult()
}

/**
 * Where the pairing (base URL, device token, controller) is kept. The
 * default is pairing.json beside the store, mode 0600, as the Python
 * library does on a desktop; Android supplies one that encrypts it under an
 * Android Keystore key and Apple keeps it in the Keychain (SYNC.md §2.1).
 */
interface PairingVault {
    fun read(): Map<String, Any?>?
    fun write(info: Map<String, Any?>)
    fun delete()
}

class FileVault(private val file: File) : PairingVault {
    override fun read(): Map<String, Any?>? = try {
        if (file.exists()) Json.parseObject(file.readText()) else null
    } catch (e: Exception) {
        null
    }

    override fun write(info: Map<String, Any?>) {
        val tmp = File(file.path + ".tmp")
        tmp.delete()
        tmp.createNewFile()
        // Owner-only before the token is written into it.
        tmp.setReadable(false, false); tmp.setReadable(true, true)
        tmp.setWritable(false, false); tmp.setWritable(true, true)
        tmp.writeText(Json.dump(info))
        if (!tmp.renameTo(file)) {
            file.delete()
            tmp.renameTo(file)
        }
    }

    override fun delete() {
        file.delete()
    }
}

class Logbook internal constructor(private val st: ShackStation) {
    private val fieldName = Regex("^[a-z][a-z0-9_]*$")

    private fun clean(fields: Map<String, Any?>, require: Boolean): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        for ((k, v) in fields) {
            if (!fieldName.matches(k)) {
                throw ShackError("bad_field_name", "'$k' is not a logbook field name: fields are lower-case ADIF names")
            }
            out[k] = v
        }
        val call = out["call"]
        // Locale-independent upper case (Locale.ROOT): the Turkish dotted I
        // must never reach a callsign (I18N.md §4.2).
        if (call != null) out["call"] = call.toString().trim().uppercase(Locale.ROOT)
        if (require) {
            if (out["call"].asStr().isEmpty()) throw ShackError("call_required", "A contact needs a callsign")
            if (out["start"].asStr().isEmpty()) throw ShackError("start_required", "A contact needs a start time")
        }
        val start = out["start"]
        if (start != null && start != "") {
            try {
                Hlc.parseIsoMs(start.toString())
            } catch (e: IllegalArgumentException) {
                throw ShackError("bad_start", "The start time '$start' is not an ISO date and time")
            }
        }
        return out
    }

    /** Adds a contact; idempotent by UUID (SERVICES.md §3.2 POST): the same
     *  contact again is the stored contact, not a second one. */
    fun add(fields: Map<String, Any?>, uuid: String? = null): Qso {
        val f = clean(fields, require = true)
        val rid = (uuid ?: Store.uuid7(st.store.now())).lowercase(Locale.ROOT)
        if (st.store.get("logbook", rid) != null) return get(rid)!!
        val q = Qso.of(st.store.edit("logbook", rid, "create", f, st.origin()))
        val out = q.copy(duplicateOf = duplicates(q))
        st.localChange("logbook", listOf(rid))
        return out
    }

    /** Several contacts in one transaction; a `uuid` key in an item is its id. */
    fun addMany(items: List<Map<String, Any?>>): List<Qso> {
        val edits = items.map { fields ->
            val f = clean(fields.filterKeys { it != "uuid" }, require = true)
            val rid = fields["uuid"].asStr().lowercase(Locale.ROOT).ifEmpty { Store.uuid7(st.store.now()) }
            Store.Edit("logbook", rid, "create", f, st.origin())
        }
        val recs = st.store.editMany(edits)
        st.localChange("logbook", recs.map { it["id"] as String })
        return recs.map(Qso::of)
    }

    fun update(uuid: String, fields: Map<String, Any?>): Qso {
        if (st.store.get("logbook", uuid) == null) throw ShackError("no_such_contact", "There is no contact with that id in this logbook")
        val q = Qso.of(st.store.edit("logbook", uuid, "update", clean(fields, require = false), st.origin()))
        st.localChange("logbook", listOf(uuid))
        return q
    }

    fun delete(uuid: String) {
        if (st.store.get("logbook", uuid) == null) throw ShackError("no_such_contact", "There is no contact with that id in this logbook")
        st.store.edit("logbook", uuid, "delete", emptyMap(), st.origin())
        st.localChange("logbook", listOf(uuid))
    }

    fun get(uuid: String): Qso? = st.store.get("logbook", uuid)?.let(Qso::of)

    /** Newest first. Deleted contacts are skipped unless asked for (lists
     *  and lookups skip them; history and sync do not: §5). */
    fun query(call: String? = null, band: String? = null, mode: String? = null, since: String? = null,
              until: String? = null, limit: Int = 100, includeDeleted: Boolean = false): List<Qso> {
        val sql = StringBuilder("SELECT id FROM qso_index WHERE 1=1")
        val args = ArrayList<Any?>()
        for ((col, v) in listOf("call" to call?.uppercase(Locale.ROOT), "band" to band, "mode" to mode?.uppercase(Locale.ROOT))) {
            if (!v.isNullOrEmpty()) { sql.append(" AND $col=?"); args.add(v) }
        }
        if (!since.isNullOrEmpty()) { sql.append(" AND start>=?"); args.add(since) }
        if (!until.isNullOrEmpty()) { sql.append(" AND start<=?"); args.add(until) }
        if (!includeDeleted) sql.append(" AND deleted=0")
        sql.append(" ORDER BY start DESC, id LIMIT ?")
        args.add(limit.toLong())
        return st.store.query(sql.toString(), *args.toTypedArray()).mapNotNull { get(it[0] as String) }
    }

    fun count(): Int = st.store.query("SELECT COUNT(*) FROM qso_index WHERE deleted=0").first()[0].asLong().toInt()

    fun workedBefore(call: String, band: String? = null, mode: String? = null): List<Qso> =
        query(call = call, band = band, mode = mode, limit = 1000)

    private fun startMs(f: Map<String, Any?>): Long? = try {
        Hlc.parseIsoMs(f["start"].asStr())
    } catch (e: IllegalArgumentException) {
        null
    }

    private fun bandOf(f: Map<String, Any?>) = f["band"].asStr().ifEmpty { Adif.bandFor(f["freq_hz"]) }

    private fun duplicates(q: Qso): List<String> {
        val t = startMs(q.fields) ?: return emptyList()
        return query(call = q.fields["call"].asStr(), band = bandOf(q.fields).ifEmpty { null },
            mode = q.fields["mode"]?.toString(), limit = 50)
            .filter { it.uuid != q.uuid }
            .filter { o -> startMs(o.fields)?.let { Math.abs(it / 1000 - t / 1000) <= DUPLICATE_WINDOW_S } ?: false }
            .map { it.uuid }
    }

    /** Every edit made to this contact on this device, oldest first. The
     *  controller's history (SERVICES.md §3.2) adds every other device's. */
    fun history(uuid: String): List<Map<String, Any?>> =
        st.store.query("SELECT op_id, hlc, kind, fields, origin, created_at FROM ops WHERE collection='logbook' AND id=? ORDER BY hlc", uuid)
            .map { r -> linkedMapOf("opId" to r[0], "hlc" to r[1], "kind" to r[2], "fields" to Json.parse(r[3] as String),
                "origin" to Json.parse(r[4] as String), "createdAt" to r[5]) }

    fun exportAdif(programVersion: String = ""): String {
        val recs = st.store.all("logbook").filter(Merge::isLive)
            .sortedWith(compareBy({ Merge.fields(it)["start"].asStr() }, { it["id"] as String }))
        return Adif.write(recs, programVersion.ifEmpty { st.appVersion })
    }

    data class ImportReport(val added: Int, val duplicates: List<Map<String, Any?>>, val errors: List<Map<String, Any?>>)

    /** SERVICES.md §3.2 import: every record keeps its APP_SHACK_UUID or
     *  gets a fresh v7 UUID; duplicates are reported, never skipped silently. */
    fun importAdif(text: String): ImportReport {
        val dups = ArrayList<Map<String, Any?>>()
        val errors = ArrayList<Map<String, Any?>>()
        val edits = ArrayList<Store.Edit>()
        val pending = ArrayList<Qso>()
        for ((line, rec) in Adif.parse(text)) {
            val fields: Map<String, Any?>
            val uid: String
            try {
                val t = Adif.toFields(rec)
                fields = clean(t.first, require = true)
                uid = t.second
            } catch (e: IllegalArgumentException) {
                errors.add(mapOf("line" to line, "error" to (e.message ?: ""), "code" to ((e as? ShackError)?.code ?: "bad_record")))
                continue
            }
            if (uid.isNotEmpty() && st.store.get("logbook", uid) != null) {
                dups.add(mapOf("line" to line, "of" to uid))
                continue
            }
            val rid = uid.ifEmpty { Store.uuid7(st.store.now()) }
            val probe = Qso(rid, fields, "", emptyMap(), false, "")
            val same = duplicates(probe).ifEmpty { pending.filter { near(it, probe) }.map { it.uuid } }
            if (same.isNotEmpty()) {
                dups.add(mapOf("line" to line, "of" to same[0]))
                continue
            }
            pending.add(probe)
            edits.add(Store.Edit("logbook", rid, "create", fields, st.origin()))
        }
        if (edits.isNotEmpty()) {
            st.store.editMany(edits)
            st.localChange("logbook", edits.map { it.id })
        }
        return ImportReport(edits.size, dups, errors)
    }

    private fun near(a: Qso, b: Qso): Boolean {
        val fa = a.fields
        val fb = b.fields
        if (fa["call"] != fb["call"] || fa["mode"].asStr().uppercase(Locale.ROOT) != fb["mode"].asStr().uppercase(Locale.ROOT)) return false
        if (bandOf(fa) != bandOf(fb)) return false
        val ta = startMs(fa) ?: return false
        val tb = startMs(fb) ?: return false
        return Math.abs(ta - tb) <= DUPLICATE_WINDOW_S * 1000
    }

    companion object {
        const val DUPLICATE_WINDOW_S = 300L   // SERVICES.md §3.1
    }
}

/** The station profile: one record (collection "profile", id "station"),
 *  SERVICES.md §2.1, merged per field with the two maps split into dotted
 *  paths (SYNC.md §4.2). */
class Profile internal constructor(private val st: ShackStation) {
    private val locatorRe = Regex("^[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2}([0-9]{2})?)?$")

    fun get(): Map<String, Any?> {
        val out = linkedMapOf<String, Any?>("schema" to 1L)
        st.store.get("profile", ID)?.let { out.putAll(Merge.joinProfile(Merge.fields(it))) }
        return out
    }

    fun update(partial: Map<String, Any?>): Map<String, Any?> {
        val p = LinkedHashMap(partial)
        p["callsign"]?.let { p["callsign"] = it.toString().trim().uppercase(Locale.ROOT) }
        val loc = p["locator"]?.toString()?.trim()
        if (!loc.isNullOrEmpty()) {
            if (!locatorRe.matches(loc)) throw ShackError("bad_locator", "$loc is not a Maidenhead locator (4, 6 or 8 characters)")
            p["locator"] = loc.take(4).uppercase(Locale.ROOT) + loc.drop(4).take(2).uppercase(Locale.ROOT) + loc.drop(6)
        }
        p.remove("schema")
        p.remove("updatedAt")
        st.store.edit("profile", ID, "update", Merge.splitProfile(p), st.origin())
        st.localChange("profile", listOf(ID))
        return get()
    }

    companion object {
        const val ID = "station"
    }
}

class ShackStation(
    val store: Store,
    val app: String,
    val appVersion: String = "",
    private val transports: TransportFactory = HttpTransport.FACTORY,
    private val background: Boolean = true,
    vault: PairingVault? = null,
) : AutoCloseable {
    val vault: PairingVault = vault ?: FileVault(File(store.path.absoluteFile.parentFile, "pairing.json"))
    val logbook = Logbook(this)
    val profile = Profile(this)
    val sync = SyncClient(this)
    private val listeners = CopyOnWriteArrayList<(String?, List<String>) -> Unit>()
    @Volatile private var watcher: Thread? = null
    @Volatile private var closed = false

    init {
        if (background && pairingInfo() != null) sync.start()
    }

    companion object {
        fun open(path: File, app: String, appVersion: String = "", opener: SqlOpener,
                 clock: () -> Long = Hlc::systemMs, transports: TransportFactory = HttpTransport.FACTORY,
                 background: Boolean = true, vault: PairingVault? = null): ShackStation =
            ShackStation(Store(path, opener, clock), app, appVersion, transports, background, vault)
    }

    // ---------------------------------------------------------- identity
    fun pairingInfo(): Map<String, Any?>? = vault.read()

    fun origin(): Map<String, Any?> {
        val p = pairingInfo() ?: emptyMap()
        return linkedMapOf("app" to app, "device" to p["deviceId"].asStr().ifEmpty { store.storeId }, "user" to p["user"].asStr())
    }

    /** A UUID per installation (REMOTE-API §7 `remote`), kept in the store. */
    fun clientId(): String = store.meta("client_id") ?: store.tx {
        store.meta("client_id") ?: UUID.randomUUID().toString().also { store.setMeta("client_id", it) }
    }

    fun transport(): Transport {
        val p = pairingInfo() ?: throw Offline("not paired")
        return transports.create(p["baseUrl"].asStr(), p["token"].asStr())
    }

    // ---------------------------------------------------------- change notification
    /** [listener] (collection, ids) after local or synced changes in this
     *  process; (null, []) when another process changed the store (noticed
     *  by PRAGMA data_version, once a second). */
    fun onChange(listener: (String?, List<String>) -> Unit) {
        listeners.add(listener)
        if (watcher == null) {
            watcher = Thread({ watchLoop() }, "shack-store-watch").apply { isDaemon = true; start() }
        }
    }

    private fun watchLoop() {
        var seen = try { store.dataVersion() } catch (e: Exception) { return }
        while (!closed) {
            try { Thread.sleep(1000) } catch (e: InterruptedException) { return }
            if (closed) return
            val now = try { store.dataVersion() } catch (e: Exception) { return }
            if (now != seen) {
                seen = now
                listeners.forEach { it(null, emptyList()) }
            }
        }
    }

    internal fun notify(keys: List<Pair<String, String>>) {
        keys.groupBy({ it.first }, { it.second }).forEach { (coll, ids) -> listeners.forEach { it(coll, ids) } }
    }

    internal fun localChange(collection: String, ids: List<String>) {
        listeners.forEach { it(collection, ids) }
        sync.localWrite()
    }

    // ---------------------------------------------------------- pairing
    /** Pair with a controller by password (CONTROLLER-CONTRACT.md §4.2)
     *  and, when nothing needs deciding, do the first full exchange (§7).
     *  Throws [PairingError] or [Offline]. Network: run it off the UI thread. */
    fun pair(baseUrl: String, username: String, password: String, totp: String? = null, deviceName: String = "",
             deviceKind: String = "phone"): PairResult {
        val t = transports.create(baseUrl, null)
        val tok = Pairing.exchangePassword(t, username, password, totp, deviceName.ifEmpty { app }, deviceKind, clientId(), app)
        return finishPairing(baseUrl, t, tok)
    }

    /** Pair a TV by code (§4.3): [onCode] gets the six digits to show; this
     *  then polls until a person approves, denies, or the code expires.
     *  Returns the outcome; Approved means paired (and synced when nothing
     *  needed deciding). [sleep] is for tests. */
    fun pairTv(baseUrl: String, deviceName: String, onCode: (TvPairState.Code) -> Unit,
               sleep: (Long) -> Unit = { Thread.sleep(it) }): Pair<TvPairState, PairResult?> {
        val t = transports.create(baseUrl, null)
        val p = Pairing.tvStart(t, deviceName, clientId(), app)
        onCode(TvPairState.Code(p.code, p.expiresIn))
        val deadline = System.nanoTime() + p.expiresIn * 1_000_000_000L
        while (System.nanoTime() < deadline) {
            val (state, tok) = Pairing.tvPoll(t, p)
            when (state) {
                "approved" -> {
                    val token = if (tok!!.controllerId.isEmpty())
                        tok.copy(controllerId = Discovery.wellKnown(baseUrl, t)["controllerId"].asStr()) else tok
                    return TvPairState.Approved(token) to finishPairing(baseUrl, t, token)
                }
                "denied" -> return TvPairState.Denied to null
                "expired" -> return TvPairState.Expired to null
            }
            sleep((p.pollEvery * 1000).toLong())
        }
        return TvPairState.Expired to null
    }

    private fun finishPairing(baseUrl: String, t: Transport, tok: Token): PairResult {
        var name = ""
        var stationCall = ""
        try {
            val (status, wk) = t.get(".well-known/openhamshack")
            if (status == 200) {
                name = wk["name"].asStr()
                stationCall = wk["station"].asMap()["callsign"].asStr()
            }
        } catch (e: Exception) {
        }
        vault.write(linkedMapOf("controllerId" to tok.controllerId, "baseUrl" to baseUrl, "token" to tok.token,
            "deviceId" to tok.deviceId, "user" to tok.user, "maxLevel" to tok.maxLevel, "name" to name,
            "pairedAt" to store.nowIso()))
        val pairing = PairingInfo(tok.controllerId, name, baseUrl, tok.deviceId, tok.user, tok.maxLevel)
        // A first exchange pushes everything (§7): records confirmed by
        // another controller are not dirty, but this one lacks them.
        if (store.cursor(tok.controllerId) == null) store.markAllDirty()
        val decision = needsDecision(pairing, stationCall)
        if (decision != null) {
            store.tx { store.setMeta("needs_decision", tok.controllerId) }
            return decision
        }
        sync.runOnce()
        if (background) sync.start()
        return PairResult.Paired(pairing)
    }

    /** §7: a full exchange when the controller's log is empty or its
     *  callsign matches; also when this device has no log at all. */
    private fun needsDecision(pairing: PairingInfo, stationCall: String): PairResult.NeedsDecision? {
        // A controller this store has synced with before already holds the
        // union: re-pairing with it is as a first pairing, minus the question.
        if (store.cursor(pairing.controllerId) != null ||
            pairing.controllerId in Json.parse(store.meta("synced_with") ?: "[]").asList()) return null
        val localN = logbook.count()
        if (localN == 0) return null
        val t = transport()
        val (s1, logs) = t.get("api/shack/v1/sync/pull", mapOf("since" to 0, "limit" to 1, "collections" to "logbook"))
        SyncClient.check(s1, logs)
        if (logs["records"].asList().isEmpty()) return null
        // The controller's station callsign: its public advert
        // (/.well-known station.callsign), else its synced profile.
        var theirs = stationCall
        if (theirs.isEmpty()) {
            val (s2, prof) = t.get("api/shack/v1/sync/pull", mapOf("since" to 0, "limit" to 5, "collections" to "profile"))
            SyncClient.check(s2, prof)
            theirs = prof["records"].asList().map { it.asMap() }.firstOrNull { it["id"] == Profile.ID }
                ?.let { Merge.fields(it)["callsign"].asStr() } ?: ""
        }
        val mine = profile.get()["callsign"].asStr()
        if (theirs.isNotEmpty() && mine.isNotEmpty() && theirs.equals(mine, ignoreCase = true)) return null
        return PairResult.NeedsDecision(pairing, mine, theirs, localN, true)
    }

    enum class Choice { MERGE, USE_CONTROLLER }

    /** Answer a NeedsDecision: merge this device's log with the
     *  controller's, or archive this one (station-<date>.db and an .adi
     *  beside it) and take the controller's. There is no "keep two logs":
     *  one store is one station. Network: off the UI thread. */
    fun resolve(result: PairResult, choice: Choice): PairingInfo {
        if (choice == Choice.USE_CONTROLLER) {
            val day = store.nowIso().take(10)
            val dir = store.path.absoluteFile.parentFile
            var base = "station-$day"
            var n = 1
            while (File(dir, "$base.db").exists()) {
                n += 1
                base = "station-$day-$n"
            }
            File(dir, "$base.adi").writeText(logbook.exportAdif(), Charsets.UTF_8)
            store.archiveTo(File(dir, "$base.db"))
            store.wipeReplica()
        }
        store.deleteMeta("needs_decision")
        sync.runOnce()
        if (background) sync.start()
        return when (result) {
            is PairResult.NeedsDecision -> result.pairing
            is PairResult.Paired -> result.pairing
        }
    }

    /** §7: forget the pairing and that controller's cursor. Records, ops
     *  and history stay exactly as they are; no network request. */
    fun unpair() {
        val p = pairingInfo()
        sync.stop()
        if (p != null) store.dropCursor(p["controllerId"].asStr())
        vault.delete()
        store.deleteMeta("needs_decision")
        sync.state = "unpaired"
        sync.profileRejected = false
    }

    override fun close() {
        closed = true
        watcher?.interrupt()
        sync.stop()
        store.close()
    }
}

/** An [Instant] as the logbook's ISO form. */
fun Instant.toShackIso(): String = Hlc.isoMs(toEpochMilli())
