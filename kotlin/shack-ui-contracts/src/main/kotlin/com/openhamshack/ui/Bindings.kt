package com.openhamshack.ui

import com.openhamshack.contracts.ContractException

/**
 * `{"bind": "state.dab.service", "format": "freq"}` (ui.schema.json `bind`):
 * a dotted path into the screen's data feeds, no expressions. [format]
 * names a client formatter (freq, db, time, distance); numbers are
 * formatted by the client, in the user's locale, not by the server.
 */
data class Binding(val path: String, val format: String?) {
    val segments: List<String> get() = path.split('.')

    /** The feed this binding reads: the first segment names a key of the
     *  document's `data`. */
    val feed: String get() = segments.first()

    companion object {
        private val PATH = Regex("^[A-Za-z0-9_$-]+(\\.[A-Za-z0-9_$-]+)*$")
        val FORMATS = setOf("freq", "db", "time", "distance")

        /** A node property is a binding when it is an object with a `bind`
         *  key; null for any other value (a literal). A malformed binding
         *  is an error rather than a literal: it is a mistake, not a
         *  property a newer server added. */
        fun from(raw: Any?, where: String): Binding? {
            if (raw !is Map<*, *> || !raw.containsKey("bind")) return null
            val path = raw["bind"] as? String
            if (path == null || !PATH.containsMatchIn(path)) {
                throw ContractException("schema", "bind must be a dotted path", where)
            }
            val format = raw["format"]
            if (raw.containsKey("format") && (format !is String || format !in FORMATS)) {
                throw ContractException("schema", "format must be one of $FORMATS", where)
            }
            return Binding(path, format as String?)
        }
    }
}

object Bindings {
    /** Walks [path] through [data] (feed name to that feed's latest JSON).
     *  Map keys by name, list elements by index; anything missing or of the
     *  wrong shape is null, which a renderer shows as empty: a feed that
     *  has not arrived yet is normal, not an error. */
    fun resolve(binding: Binding, data: Map<String, Any?>): Any? {
        var cur: Any? = data
        for (seg in binding.segments) {
            cur = when (cur) {
                is Map<*, *> -> cur[seg]
                is List<*> -> seg.toIntOrNull()?.let { cur.getOrNull(it) }
                else -> null
            } ?: return null
        }
        return cur
    }

    /** A post's body with bindings replaced by their current values and
     *  the string "$value" by what the control produced (a slider's
     *  position). Applies to nested objects and lists, nothing else. */
    fun resolveBody(body: Any?, value: Any?, data: Map<String, Any?>): Any? = when {
        body == "\$value" -> value
        body is Map<*, *> && body.containsKey("bind") ->
            Binding.from(body, "body")?.let { resolve(it, data) }
        body is Map<*, *> -> body.entries.associate { (k, v) -> k as String to resolveBody(v, value, data) }
        body is List<*> -> body.map { resolveBody(it, value, data) }
        else -> body
    }
}
