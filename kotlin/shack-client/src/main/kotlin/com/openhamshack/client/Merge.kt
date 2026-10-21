package com.openhamshack.client

/**
 * The merge every replica runs (SYNC.md §4), a port of python/shack_client
 * merge.py: client stores and the controller run the same function from the
 * same vectors, so they cannot drift.
 *
 * A record is the wire shape of SYNC.md §6.1, held as a JSON map:
 *
 *     {"collection": "logbook", "id": "<uuid>", "fields": {...},
 *      "fieldHlc": {...}, "deletedHlc": null | "<hlc>",
 *      "createdAt": "<iso>", "origin": {...}}
 *
 * plus any envelope keys a later contract version adds, carried through
 * untouched (§8 T19). Kept as a map rather than a data class for that
 * reason: a typed record would drop what it does not know.
 *
 * State-based and per field: for each field, the value with the greater
 * HLC. Commutative, associative and idempotent (MergeTest checks all three
 * on generated records).
 */
typealias Record = Map<String, Any?>

object Merge {
    val ENVELOPE = setOf("collection", "id", "fields", "fieldHlc", "deletedHlc", "createdAt", "origin")

    /** The profile's two maps whose entries merge one by one (§4.2): two
     *  devices adding different pad layouts, or answering different licence
     *  conditions, must both be kept. Value: how many levels are split. */
    val PROFILE_SPLIT = mapOf("conditions" to 1, "controllerMaps" to 2)

    fun fields(r: Record): Map<String, Any?> = r["fields"].asMap()

    @Suppress("UNCHECKED_CAST")
    fun fieldHlc(r: Record): Map<String, String> = r["fieldHlc"].asMap() as Map<String, String>

    fun deletedHlc(r: Record): String? = (r["deletedHlc"] as? String)?.takeIf { it.isNotEmpty() }

    /** Envelope keys this version does not know, to carry through. */
    fun extraOf(r: Record): Map<String, Any?> = r.filterKeys { it !in ENVELOPE && it != "seq" }

    fun maxHlc(vararg hlcs: String?): String? = hlcs.filterNotNull().filter { it.isNotEmpty() }.maxOrNull()

    /** Every stamp a record carries: its fields' and its tombstone's. */
    fun stamps(r: Record): List<String> = fieldHlc(r).values.toList() + listOfNotNull(deletedHlc(r))

    /** Deleted only while the tombstone is later than every field edit: a
     *  later edit brings a contact back (§5, edit beats delete). */
    fun isLive(r: Record): Boolean {
        val d = deletedHlc(r) ?: return true
        return fieldHlc(r).values.any { it > d }
    }

    /** The merged record. Either side may be null (a record one replica
     *  has never seen). */
    fun merge(a: Record?, b: Record?): MutableMap<String, Any?> {
        @Suppress("UNCHECKED_CAST")
        if (a == null) return deepCopy(b) as MutableMap<String, Any?>
        @Suppress("UNCHECKED_CAST")
        if (b == null) return deepCopy(a) as MutableMap<String, Any?>
        val ha = fieldHlc(a)
        val hb = fieldHlc(b)
        val fields = LinkedHashMap<String, Any?>()
        val hlcs = LinkedHashMap<String, String>()
        for (f in (ha.keys + hb.keys).sorted()) {
            val x = ha[f]
            val y = hb[f]
            // Equal stamps mean the same edit seen twice: either side's value
            // is the same value. Ties never happen between different edits.
            val src = if (y == null || (x != null && x >= y)) a else b
            hlcs[f] = fieldHlc(src).getValue(f)
            val sf = fields(src)
            if (sf.containsKey(f)) fields[f] = deepCopy(sf[f])
        }
        val ca = a["createdAt"].asStr()
        val cb = b["createdAt"].asStr()
        // The origin is the earlier creation's; a tie (two copies of one
        // creation) breaks on the origin's sorted JSON, so the merge stays
        // commutative even for copies that disagree. The JSON is Python's
        // (Json.dump), so a Kotlin client and the Python controller pick the
        // same side.
        val first = listOf(a, b).minWith(compareBy<Record>({ it["createdAt"].asStr().isEmpty() },
            { it["createdAt"].asStr() }, { Json.dump(it["origin"].asMap()) }))
        val out = LinkedHashMap<String, Any?>()
        out["collection"] = a["collection"]
        out["id"] = a["id"]
        out["fields"] = fields
        out["fieldHlc"] = hlcs
        out["deletedHlc"] = maxHlc(deletedHlc(a), deletedHlc(b))
        out["createdAt"] = listOf(ca, cb).filter { it.isNotEmpty() }.minOrNull() ?: ""
        out["origin"] = deepCopy(first["origin"].asMap())
        // Unknown envelope keys: the side with the later state wins each, so
        // a newer client's addition survives an older one's push.
        val (newer, older) = if (latest(a) >= latest(b)) a to b else b to a
        extraOf(older).forEach { (k, v) -> out[k] = deepCopy(v) }
        extraOf(newer).forEach { (k, v) -> out[k] = deepCopy(v) }
        return out
    }

    private fun latest(r: Record): String = maxHlc(deletedHlc(r), *fieldHlc(r).values.toTypedArray()) ?: ""

    /** Whether two states are the same for sync purposes (a merge that
     *  returns the stored state changed nothing and takes no new seq). */
    fun same(a: Record?, b: Record?): Boolean {
        if (a == null || b == null) return a === b
        return fields(a) == fields(b) && fieldHlc(a) == fieldHlc(b) && deletedHlc(a) == deletedHlc(b) &&
            a["createdAt"].asStr() == b["createdAt"].asStr() && a["origin"].asMap() == b["origin"].asMap() &&
            extraOf(a) == extraOf(b)
    }

    /** A local edit as a record state: the same merge, with the edit as a
     *  one-op record. A null value clears a field (an edit with its own
     *  HLC: SERVICES.md §3.2 PATCH). */
    fun applyEdit(rec: Record?, collection: String, id: String, kind: String, fields: Map<String, Any?>,
                  hlc: String, origin: Map<String, Any?>, nowIso: String): MutableMap<String, Any?> {
        val oneFields = LinkedHashMap<String, Any?>()
        val oneHlc = LinkedHashMap<String, String>()
        var deleted: String? = null
        if (kind == "delete") {
            deleted = hlc
        } else {
            for ((f, v) in fields) {
                oneHlc[f] = hlc
                if (v != null) oneFields[f] = v
            }
        }
        val one = mapOf("collection" to collection, "id" to id, "fields" to oneFields, "fieldHlc" to oneHlc,
            "deletedHlc" to deleted,
            "createdAt" to (if (rec == null) nowIso else rec["createdAt"] ?: nowIso),
            "origin" to (if (rec == null) origin else rec["origin"] ?: origin))
        return merge(rec, one)
    }

    /** The fields an arriving op set that a later-HLC edit from another
     *  store had already set (SERVICES.md §3.2 `lost`). */
    fun lostFields(op: Map<String, Any?>, state: Record): List<String> {
        val hlc = op["hlc"].asStr()
        val node = Hlc.nodeOf(hlc)
        val sh = fieldHlc(state)
        return op["fields"].asMap().keys.sorted().filter { f ->
            val h = sh[f]
            h != null && h > hlc && Hlc.nodeOf(h) != node
        }
    }

    // ------------------------------------------------------------ profile paths

    /** A profile patch as record fields: top-level keys, except the two maps
     *  of §4.2, which become dotted paths ("conditions.nearLondon",
     *  "controllerMaps.qo100wb.ajazz-akp03e"). */
    fun splitProfile(partial: Map<String, Any?>): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        for ((k, v) in partial) {
            val depth = PROFILE_SPLIT[k]
            if (depth != null && v is Map<*, *>) {
                flatten(v.asMap(), depth, emptyList()) { path, leaf -> out["$k." + path.joinToString(".")] = leaf }
            } else {
                out[k] = v
            }
        }
        return out
    }

    private fun flatten(d: Map<String, Any?>, depth: Int, prefix: List<String>, emit: (List<String>, Any?) -> Unit) {
        for ((k, v) in d) {
            if (depth > 1 && v is Map<*, *>) flatten(v.asMap(), depth - 1, prefix + k, emit) else emit(prefix + k, v)
        }
    }

    /** Record fields back into the SERVICES.md §2.1 shape. */
    @Suppress("UNCHECKED_CAST")
    fun joinProfile(fields: Map<String, Any?>): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        for (k in fields.keys.sorted()) {
            val head = k.substringBefore('.')
            val rest = if ('.' in k) k.substringAfter('.') else ""
            val depth = PROFILE_SPLIT[head]
            if (depth != null && rest.isNotEmpty()) {
                var node = out.getOrPut(head) { LinkedHashMap<String, Any?>() } as MutableMap<String, Any?>
                val parts = rest.split(".", limit = depth)
                for (p in parts.dropLast(1)) node = node.getOrPut(p) { LinkedHashMap<String, Any?>() } as MutableMap<String, Any?>
                node[parts.last()] = fields[k]
            } else {
                out[k] = fields[k]
            }
        }
        return out
    }
}
