package com.openhamshack.ui

import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Fields

/** §5.6 broken: something tried to make a generic action transmit, or put
 *  one beside a native ptt/enableTx. Always an error, in lenient parsing
 *  too: the renderer refuses the document rather than the action. */
class TransmitException(code: String, detail: String, where: String) : ContractException(code, detail, where)

enum class Level { VIEW, CONTROL }

/**
 * A ui/1 action (CLIENT-UI-PLAN.md §5.5, ui.schema.json `action`). There is
 * deliberately no TRANSMIT level: the type cannot hold one, so a renderer
 * built on it cannot be handed a way to key a radio by a document. Parsing
 * a `level: transmit` fails before the type is reached.
 *
 * [id] and [label] are optional here and required on an overlay item's
 * actions, where the first is the item's tap action and the rest are listed
 * by name.
 */
sealed class Action {
    abstract val id: String?
    abstract val label: String?

    /** [body] is any JSON value; it may use "$value" and `{"bind": ...}`
     *  (see [Bindings.resolveBody]). [level] is what the proxy will require
     *  of the token. */
    data class Post(
        val path: String, val level: Level, val body: Any?, val hasBody: Boolean,
        override val id: String?, override val label: String?,
    ) : Action()

    data class Open(
        val screen: String, val params: Map<String, Any?>,
        override val id: String?, override val label: String?,
    ) : Action()

    data class Back(override val id: String?, override val label: String?) : Action()

    data class Local(
        val key: String, val value: Any?,
        override val id: String?, override val label: String?,
    ) : Action()

    /** `verb` is `component.verb`, e.g. waterfall.zoomIn. */
    data class Native(val verb: String, override val id: String?, override val label: String?) : Action()

    /** Wraps another action with a question; Stay is focused. */
    data class Confirm(
        val text: String, val action: Action,
        override val id: String?, override val label: String?,
    ) : Action()

    companion object {
        val DO = setOf("post", "open", "back", "local", "native", "confirm")
        private val RELPATH = Regex("^(?![a-zA-Z][a-zA-Z0-9+.-]*:)(?!/)")
        private val VERB = Regex("^[A-Za-z]+\\.[A-Za-z]+$")
        private val LEVELS = setOf("view", "control", "transmit")

        /** Relative to the app's base: never absolute, never a scheme,
         *  because one process is served under a path prefix on one address
         *  and at a subdomain's root on another. */
        fun isRelativePath(p: String) = RELPATH.containsMatchIn(p)

        /** The named checks of validate.py's check_action, on the raw
         *  action: unknown_action, then transmit_action, recursing into a
         *  confirm. */
        fun check(raw: Any?, where: String) {
            if (raw !is Map<*, *>) throw ContractException("schema", "an action is an object", where)
            val d = raw["do"]
            if (d !in DO) throw ContractException("unknown_action", "do $d is not one of ${DO.sorted()}", where)
            if (raw["level"] == "transmit") {
                throw TransmitException(
                    "transmit_action",
                    "no generic action may transmit; place the native ptt or enableTx component instead", where,
                )
            }
            if (d == "confirm" && raw["action"] is Map<*, *>) check(raw["action"], "$where.action")
        }

        fun parse(raw: Any?, where: String = "action"): Action {
            // The named errors come first so a transmit action reads as
            // "transmit", not as a schema path.
            check(raw, where)
            val f = Fields.of(raw, where)
            val id = f.optString("id")
            val label = f.optString("label")
            // A level of any kind is checked wherever it appears, not only
            // on a post, as the schema does.
            val level = f.optEnum("level")?.also { if (it !in LEVELS) f.fail("level", "$it is not a level") }
            return when (f.string("do")) {
                "post" -> {
                    val path = f.string("path", pattern = RELPATH)
                    if (level == null) f.fail("level", "every post declares its level")
                    Post(path, if (level == "view") Level.VIEW else Level.CONTROL, f.map["body"], f.has("body"), id, label)
                }
                "open" -> Open(f.string("screen", 1), f.optObj("params")?.map ?: emptyMap(), id, label)
                "back" -> Back(id, label)
                "local" -> Local(f.string("key", 1), f.map["value"], id, label)
                "native" -> Native(f.string("verb", pattern = VERB), id, label)
                else -> {
                    val text = f.string("text", 1)
                    if (!f.has("action")) f.fail("action", "is required")
                    Confirm(text, parse(f.map["action"], "$where.action"), id, label)
                }
            }
        }
    }
}
