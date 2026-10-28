package com.openhamshack.ui

import com.openhamshack.contracts.Fields

/**
 * `GET <base>ui/v1/app` (CLIENT-UI-PLAN.md §5.3, §6.2): what an app offers
 * the launcher. There is no schema file for it yet, so this reads only what
 * the plan names and is lenient about the rest: a descriptor from a newer
 * app must still give its tile. [spectrum] and [overlay] are the contract
 * versions the app serves (null = not offered), which is how a client
 * finds spectrum/1 (§2.5).
 */
data class AppDescriptor(
    val name: String,
    val icon: Map<String, String>,
    val entries: List<Entry>,
    val screens: List<String>,
    val spectrum: Long?,
    val overlay: Long?,
    val minClient: String?,
    val status: Binding?,
) {
    /** A launcher tile: its [screen] is opened when chosen. [raw] keeps the
     *  entry's other properties (icon, status binding) for the tile. */
    data class Entry(val id: String?, val title: String, val screen: String, val status: Binding?, val raw: Map<String, Any?>)

    companion object {
        fun parse(raw: Any?): AppDescriptor {
            val f = Fields.of(raw)
            return AppDescriptor(
                name = f.string("name", 1),
                icon = f.optObj("icon")?.map?.mapNotNull { (k, v) -> (v as? String)?.let { k to it } }?.toMap() ?: emptyMap(),
                entries = f.optList("entries")?.mapIndexed { i, e ->
                    val ef = Fields.of(e, "entries[$i]")
                    Entry(
                        ef.optString("id"), ef.string("title", 1), ef.string("screen", 1),
                        Binding.from(ef.map["status"], "entries[$i].status"), ef.map,
                    )
                } ?: emptyList(),
                screens = f.optList("screens")?.map { it as? String ?: f.fail("screens", "ids are strings") } ?: emptyList(),
                spectrum = f.optLong("spectrum", 1),
                overlay = f.optLong("overlay", 1),
                minClient = f.optString("minClient"),
                status = Binding.from(f.map["status"], "status"),
            )
        }
    }
}
