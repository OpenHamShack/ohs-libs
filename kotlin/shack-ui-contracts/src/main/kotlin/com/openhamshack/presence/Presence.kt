package com.openhamshack.presence

import com.openhamshack.contracts.Fields

/**
 * The platform's client presence service (presence.schema.json,
 * CLIENT-UI-PLAN.md §7.2): one lease per client, renewed every 10 s and
 * expiring after 30 s, that apps hang client-scoped holds (audio, DAB,
 * AIS) off. The renewal is the whole state, not a delta, so a lost one
 * costs nothing.
 *
 * Endpoints (relative to the platform base): POST api/shack/v1/presence
 * takes [Renew] and answers [Lease]; DELETE .../presence/<client> is the
 * goodbye (best effort, 1 s time-out, no body); GET .../presence answers
 * [PresenceList]; GET .../presence/events is SSE whose event name is
 * arrived, moved or gone. Each message is checked against its own shape,
 * since the event name already says which.
 */
data class Renew(
    val client: String,
    val app: String,
    val screen: String?,
    val holds: List<String>,
    val deviceId: String? = null,
    val deviceName: String? = null,
    val kind: String? = null,
    val user: String? = null,
    val since: String? = null,
    val lastSeen: String? = null
) {
    fun toMap(): Map<String, Any?> = buildMap {
        put("client", client)
        put("app", app)
        screen?.let { put("screen", it) }
        put("holds", holds)
        deviceId?.let { put("deviceId", it) }
        deviceName?.let { put("deviceName", it) }
        kind?.let { put("kind", it) }
        user?.let { put("user", it) }
        since?.let { put("since", it) }
        lastSeen?.let { put("lastSeen", it) }
    }

    companion object {
        fun parse(raw: Any?, where: String = ""): Renew {
            val f = Fields.of(raw, where)
            val holds = f.optList("holds")?.map { it as? String ?: f.fail("holds", "names are strings") } ?: emptyList()
            if (holds.any { it.isEmpty() }) f.fail("holds", "a hold has a name")
            if (holds.toSet().size != holds.size) f.fail("holds", "holds are unique")
            return Renew(
                f.string("client", 1, 128),
                f.string("app", 1),
                f.optString("screen"),
                holds,
                f.optString("deviceId"),
                f.optString("deviceName"),
                f.optString("kind"),
                f.optString("user"),
                f.optString("since"),
                f.optString("lastSeen")
            )
        }
    }
}

data class Previous(val client: String, val app: String?, val screen: String?, val holds: List<String>?) {
    companion object {
        fun parse(raw: Map<String, Any?>, where: String = ""): Previous {
            val f = Fields(raw, where)
            val holds = f.optList("holds")?.map { it as? String ?: f.fail("holds", "names are strings") } ?: emptyList()
            if (holds.any { it.isEmpty() }) f.fail("holds", "a hold has a name")
            if (holds.isNotEmpty() && holds.toSet().size != holds.size) f.fail("holds", "holds are unique")
            return Previous(
                f.string("client", 1, 128),
                f.optString("app"),
                f.optString("screen"),
                holds.ifEmpty { null }
            )
        }
    }
}

data class Lease(val leaseS: Long, val renewEveryS: Long) {
    companion object {
        fun parse(raw: Any?): Lease {
            val f = Fields.of(raw)
            return Lease(f.long("leaseS", 1), f.long("renewEveryS", 1))
        }
    }
}

data class PresenceList(val clients: List<Renew>) {
    companion object {
        fun parse(raw: Any?): PresenceList {
            val f = Fields.of(raw)
            return PresenceList(f.list("clients").mapIndexed { i, c -> Renew.parse(c, "clients[$i]") })
        }
    }
}

/** An SSE `moved` event: the client's new location with optional previous location. */
data class Moved(
    val client: String,
    val app: String,
    val screen: String?,
    val holds: List<String>,
    val deviceId: String? = null,
    val deviceName: String? = null,
    val kind: String? = null,
    val user: String? = null,
    val since: String? = null,
    val lastSeen: String? = null,
    val previous: Previous? = null
) {
    companion object {
        fun parse(raw: Any?, where: String = ""): Moved {
            val f = Fields.of(raw, where)
            val holds = f.optList("holds")?.map { it as? String ?: f.fail("holds", "names are strings") } ?: emptyList()
            if (holds.any { it.isEmpty() }) f.fail("holds", "a hold has a name")
            if (holds.toSet().size != holds.size) f.fail("holds", "holds are unique")
            val prevObj = f.optObj("previous")?.let { Previous.parse(it.map, "$where.previous") }
            return Moved(
                f.string("client", 1, 128),
                f.string("app", 1),
                f.optString("screen"),
                holds,
                f.optString("deviceId"),
                f.optString("deviceName"),
                f.optString("kind"),
                f.optString("user"),
                f.optString("since"),
                f.optString("lastSeen"),
                prevObj
            )
        }
    }
}

/** An SSE `gone` event: why is goodbye or expired. */
data class Gone(
    val client: String,
    val why: String,
    val app: String? = null,
    val screen: String? = null,
    val holds: List<String>? = null,
    val deviceId: String? = null,
    val user: String? = null
) {
    companion object {
        fun parse(raw: Any?): Gone {
            val f = Fields.of(raw)
            val why = f.string("why")
            if (why != "goodbye" && why != "expired") f.fail("why", "why is goodbye or expired")
            val holds = f.optList("holds")?.map { it as? String ?: f.fail("holds", "names are strings") }
            if (holds != null && holds.any { it.isEmpty() }) f.fail("holds", "a hold has a name")
            if (holds != null && holds.toSet().size != holds.size) f.fail("holds", "holds are unique")
            return Gone(
                f.string("client", 1, 128),
                why,
                f.optString("app"),
                f.optString("screen"),
                holds,
                f.optString("deviceId"),
                f.optString("user")
            )
        }
    }
}

/** An SSE event by its name; arrived and moved carry presence data, gone carries goodbye data. */
object PresenceEvents {
    fun parse(event: String, raw: Any?): Any = when (event) {
        "arrived" -> Renew.parse(raw)
        "moved" -> Moved.parse(raw)
        "gone" -> Gone.parse(raw)
        else -> throw com.openhamshack.contracts.ContractException("schema", "unknown presence event $event")
    }
}
