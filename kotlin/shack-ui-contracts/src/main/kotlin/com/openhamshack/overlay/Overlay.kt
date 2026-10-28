package com.openhamshack.overlay

import com.openhamshack.contracts.Fields
import com.openhamshack.ui.Action

/**
 * One thing drawn on a waterfall (overlay.schema.json, CLIENT-UI-PLAN.md
 * §3.2). Labels are already translated by the server; [role] is a semantic
 * colour role that the client's theme maps, never an RGB value.
 *
 * [readAtHzPerPx] null means "always" (a band segment) and so does an
 * absent field, which the server fills by kind but a client that sees none
 * should not hide the item for.
 */
data class OverlayItem(
    val id: String,
    val kind: String,
    val layer: String,
    val loHz: Double,
    val hiHz: Double,
    val centreHz: Double?,
    val label: String,
    val short: String?,
    val detail: String?,
    val prio: Int,
    val readAtHzPerPx: Double?,
    val role: String?,
    val active: Boolean,
    val expires: Long?,
    val source: String?,
    /** The first is the item's OK/tap action. Never a transmit action:
     *  [Action] cannot hold one. */
    val actions: List<Action>,
) {
    /** band and dial are always drawn; the rest are the client's toggles. */
    val alwaysOn: Boolean get() = layer == "band" || layer == "dial"

    /** The label shows once the view is at or below [readAtHzPerPx] Hz a
     *  pixel (zoomed in enough), or always when that is null; an active
     *  item of prio 80 or more may break the zoom rule (§3.2, §4.4). */
    fun labelVisible(hzPerPx: Double): Boolean =
        readAtHzPerPx == null || hzPerPx <= readAtHzPerPx || (active && prio >= ACTIVE_BREAKS_ZOOM_PRIO)

    fun expired(nowMs: Long): Boolean = expires != null && nowMs >= expires

    companion object {
        const val ACTIVE_BREAKS_ZOOM_PRIO = 80
        val KINDS = setOf(
            "segment", "dial", "passband", "tx", "channel", "station", "session", "spot", "bookmark", "beacon",
            "alert", "signal",
        )
        val LAYERS = setOf("band", "dial", "channels", "stations", "sessions", "spots", "bookmarks", "beacons", "alerts", "signals")
        private val ROLE = Regex("^[a-z][A-Za-z0-9]*(\\.[a-z][A-Za-z0-9-]*)*$")

        fun parse(raw: Any?, where: String = "items"): OverlayItem {
            val f = Fields.of(raw, where)
            fun oneOf(key: String, set: Set<String>) = f.string(key).also { if (it !in set) f.fail(key, "$it is not one of $set") }
            val readAt = if (f.has("readAtHzPerPx") && f.map["readAtHzPerPx"] == null) null else f.optNumber("readAtHzPerPx", exclusiveMin = 0.0)
            val expires = if (f.has("expires") && f.map["expires"] == null) null else f.optLong("expires", 0)
            return OverlayItem(
                id = f.string("id", 1),
                kind = oneOf("kind", KINDS),
                layer = oneOf("layer", LAYERS),
                loHz = f.number("loHz", 0.0),
                hiHz = f.number("hiHz", 0.0),
                centreHz = f.optNumber("centreHz", 0.0),
                label = f.string("label"),
                short = f.optString("short"),
                detail = f.optString("detail"),
                prio = f.optLong("prio", 0, 100)?.toInt() ?: 0,
                readAtHzPerPx = readAt,
                role = f.optString("role", pattern = ROLE),
                active = f.optBool("active") ?: false,
                expires = expires,
                source = f.optString("source"),
                actions = f.optList("actions")?.mapIndexed { i, a ->
                    // An overlay action additionally needs an id and a label.
                    Fields.of(a, "$where.actions[$i]").also { it.string("id"); it.string("label") }
                    Action.parse(a, "$where.actions[$i]")
                } ?: emptyList(),
            )
        }
    }
}

enum class OverlayOp { SNAPSHOT, UPSERT, REMOVE }

/** One `overlay` text frame. A remove carries only ids, in [removed];
 *  the other ops carry [items]. */
data class OverlayMessage(val rev: Long, val op: OverlayOp, val items: List<OverlayItem>, val removed: List<String>) {
    companion object {
        fun parse(raw: Any?): OverlayMessage {
            val f = Fields.of(raw)
            if (f.string("type") != "overlay") f.fail("type", "not an overlay message")
            val rev = f.long("rev", 0)
            val op = f.enum("op", mapOf("snapshot" to OverlayOp.SNAPSHOT, "upsert" to OverlayOp.UPSERT, "remove" to OverlayOp.REMOVE))
            val items = f.list("items")
            return if (op == OverlayOp.REMOVE) {
                OverlayMessage(rev, op, emptyList(), items.mapIndexed { i, it -> Fields.of(it, "items[$i]").string("id", 1) })
            } else {
                OverlayMessage(rev, op, items.mapIndexed { i, it -> OverlayItem.parse(it, "items[$i]") }, emptyList())
            }
        }
    }
}

/**
 * The client's overlay state, kept in step with a socket by `rev`
 * (§3.1): the rev counts changes per socket, a snapshot comes on subscribe
 * and after any gap, and a client that sees a rev it did not expect asks
 * again (GET api/overlay/v1 gives the same snapshot).
 */
class OverlayStore {
    enum class Result {
        /** Applied; [OverlayStore.rev] moved. */
        APPLIED,
        /** A repeat or reorder (rev at or below ours): ignored. */
        STALE,
        /** rev skipped ahead: changes were missed, so nothing was applied
         *  and the caller must fetch a snapshot. */
        RESYNC,
    }

    private val items = LinkedHashMap<String, OverlayItem>()

    /** null until the first snapshot: until then upserts and removes cannot
     *  be placed in any order and ask for one. */
    var rev: Long? = null
        private set

    val all: List<OverlayItem> get() = items.values.toList()

    operator fun get(id: String) = items[id]

    fun apply(m: OverlayMessage): Result {
        if (m.op == OverlayOp.SNAPSHOT) {
            // A snapshot is accepted at any rev: a new socket starts its
            // count again, and a snapshot is what resyncs.
            items.clear()
            m.items.forEach { items[it.id] = it }
            rev = m.rev
            return Result.APPLIED
        }
        val cur = rev ?: return Result.RESYNC
        if (m.rev <= cur) return Result.STALE
        if (m.rev != cur + 1) return Result.RESYNC
        if (m.op == OverlayOp.UPSERT) m.items.forEach { items[it.id] = it } else m.removed.forEach { items.remove(it) }
        rev = m.rev
        return Result.APPLIED
    }

    /** Drops items past their `expires` (box clock, like a row's capture
     *  time); the server sends a remove too, but a client that missed it
     *  should not keep drawing a spot that has timed out. */
    fun prune(nowMs: Long) {
        items.values.removeAll { it.expired(nowMs) }
    }

    /** Items to consider for labels at this zoom: layers that are on (band
     *  and dial always), labels whose zoom rule is met, highest prio first. */
    fun labelCandidates(hzPerPx: Double, layersOn: Set<String>): List<OverlayItem> =
        items.values.filter { (it.alwaysOn || it.layer in layersOn) && it.labelVisible(hzPerPx) }
            .sortedByDescending { it.prio }
}
