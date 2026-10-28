package com.openhamshack.ui

import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Fields

/** A property of a node: a literal JSON value, or a binding to resolve
 *  against the screen's data each time the data changes. */
sealed class Prop {
    data class Literal(val value: Any?) : Prop()
    data class Bound(val binding: Binding) : Prop()
}

/** A feed the screen binds to, relative to the app's base: `ws:` an
 *  existing event socket, `get:` a JSON resource re-fetched on its feed's
 *  change event, or every [pollS] seconds. */
data class Feed(val src: String, val pollS: Double?) {
    val isSocket get() = src.startsWith("ws:")
    val path get() = src.substringAfter(':')
}

/**
 * One node of the screen tree, typed for what a renderer must decide and
 * open for what it merely displays. [props] holds every other property
 * (label, text, value, min, max...) as a [Prop], so a newer server's extra
 * property is carried and ignored rather than rejected (§5.3).
 *
 * [known] is false for a component this client's vocabulary lacks (a
 * newer server's, or a native component with an unknown name); a renderer
 * draws [fallback] in its place, or "needs a newer OpenHamShack app" when
 * there is none. [fallback] is parsed for known nodes too, since a client
 * that lacks a native component's version reaches for it.
 */
data class Node(
    val c: String,
    val known: Boolean,
    val children: List<Node>,
    val action: Action?,
    val fallback: Node?,
    val tv: Boolean?,
    val phone: Boolean?,
    val next: String?,
    val nativeName: String?,
    val nativeVersion: Long?,
    val nativeProps: Map<String, Any?>,
    val props: Map<String, Prop>,
) {
    val isNative get() = c == "native"

    /** What to draw on a client that cannot draw this node: itself when it
     *  can, else its fallback chain, else null (the "needs a newer app"
     *  message). */
    fun renderable(): Node? = if (known) this else fallback?.renderable()

    /** Whether the node is shown on this form factor (tv:false drops it on
     *  a TV, phone:false on a phone). */
    fun shownOn(tv: Boolean): Boolean = if (tv) this.tv != false else this.phone != false
}

data class Document(
    val ui: String,
    val screen: String,
    val rev: String?,
    val title: String?,
    val data: Map<String, Feed>,
    val root: Node,
) {
    companion object {
        val COMPONENTS = setOf(
            "column", "row", "grid", "list", "tabs", "section", "card", "sheet", "spacer",
            "text", "value", "badge", "chip", "image", "meter", "nowPlaying", "empty",
            "button", "toggle", "slider", "stepper", "choice", "textField", "frequencyField", "settingsForm",
            "native",
        )
        val NATIVE = setOf("waterfall", "map", "audio", "video", "radioFace", "sstvPicture", "decodeList", "ptt", "enableTx", "camera")

        /** The only components that may key a radio: their lease,
         *  heartbeat and time-outs are client code with tests, so a
         *  document only places them. */
        val TRANSMIT_NATIVE = setOf("ptt", "enableTx")

        private val VERSION = Regex("^1\\.[0-9]+$")
        private val FEED = Regex("^(ws|get):(?![a-zA-Z][a-zA-Z0-9+.-]*:)(?!/)")

        /**
         * The server's check (validate.py's validate): the named errors
         * first, over the whole tree, then the structure. An unknown
         * component is an error because a server must not serve one; send
         * a fallback instead.
         */
        fun validate(raw: Any?): Document = parse(raw, strict = true)

        /**
         * A client's reading of a document. Differs from [validate] only in
         * what a newer server may send: an unknown component, or an
         * unknown native name, becomes a node with known = false (so its
         * fallback is drawn) instead of an error. Everything about
         * transmit stays an error: a document that places a transmit
         * action is refused whole, never partly drawn.
         */
        fun parse(raw: Any?, strict: Boolean = false): Document {
            if (raw is Map<*, *> && raw["root"] is Map<*, *>) checkNode(raw["root"], "root", strict)
            val f = Fields.of(raw)
            val ui = f.string("ui", pattern = VERSION)
            val screen = f.string("screen", 1)
            val data = f.optObj("data")?.let { d ->
                d.map.keys.associateWith { k ->
                    when (val v = d.map[k]) {
                        is String -> Feed(feed(v, d, k), null)
                        is Map<*, *> -> {
                            val o = d.obj(k)
                            Feed(feed(o.map["src"], o, "src"), o.optNumber("poll", exclusiveMin = 0.0))
                        }
                        else -> d.fail(k, "a feed is a string or {src, poll}")
                    }
                }
            } ?: emptyMap()
            return Document(ui, screen, f.optString("rev"), f.optString("title"), data, node(f.map["root"], "root", strict))
        }

        private fun feed(v: Any?, f: Fields, key: String): String {
            if (v !is String || !FEED.containsMatchIn(v)) f.fail(key, "a feed is ws: or get: and relative")
            return v
        }

        /** validate.py's check_node: the transmit and vocabulary rules,
         *  named. [strict] adds the vocabulary rules a client relaxes. */
        private fun checkNode(raw: Any?, where: String, strict: Boolean) {
            if (raw !is Map<*, *>) throw ContractException("schema", "a node is an object", where)
            val c = raw["c"]
            if (c !in COMPONENTS && strict) {
                throw ContractException("unknown_component", "$c is not in the ui/1 vocabulary", where)
            }
            if (c == "native") {
                val name = raw["name"]
                if (name !in NATIVE && strict) {
                    throw ContractException("unknown_native", "$name is not a ui/1 native component", where)
                }
                if (name in TRANSMIT_NATIVE && raw.containsKey("action")) {
                    throw TransmitException(
                        "transmit_component_action",
                        "$name takes no generic action: its transmit behaviour is client code", where,
                    )
                }
            }
            if (raw.containsKey("action")) Action.check(raw["action"], "$where.action")
            if (raw["fallback"] is Map<*, *>) checkNode(raw["fallback"], "$where.fallback", strict)
            (raw["children"] as? List<*>)?.forEachIndexed { i, ch -> checkNode(ch, "$where.children[$i]", strict) }
        }

        private fun node(raw: Any?, where: String, strict: Boolean): Node {
            val f = Fields.of(raw, where)
            val c = f.string("c")
            var known = c in COMPONENTS
            if (!known && strict) f.fail("c", "$c is not a component")
            var nativeName: String? = null
            var nativeVersion: Long? = null
            var nativeProps: Map<String, Any?> = emptyMap()
            if (c == "native") {
                nativeName = f.string("name")
                if (nativeName !in NATIVE) {
                    if (strict) f.fail("name", "$nativeName is not a native component")
                    known = false
                }
                nativeVersion = f.optLong("v", 1)
                nativeProps = f.optObj("props")?.map ?: emptyMap()
            }
            val children = f.optList("children")?.mapIndexed { i, ch -> node(ch, "$where.children[$i]", strict) } ?: emptyList()
            val next = f.optString("next", pattern = Regex("^(?![a-zA-Z][a-zA-Z0-9+.-]*:)(?!/)"))
            val structural = setOf("c", "children", "action", "fallback", "tv", "phone", "next", "name", "v", "props")
            val props = f.map.filterKeys { it !in structural }.mapValues { (k, v) ->
                Binding.from(v, "$where.$k")?.let { Prop.Bound(it) as Prop } ?: Prop.Literal(v)
            }
            return Node(
                c, known, children,
                if (f.has("action")) Action.parse(f.map["action"], "$where.action") else null,
                if (f.has("fallback")) node(f.map["fallback"], "$where.fallback", strict) else null,
                f.optBool("tv"), f.optBool("phone"), next, nativeName, nativeVersion, nativeProps, props,
            )
        }
    }
}
