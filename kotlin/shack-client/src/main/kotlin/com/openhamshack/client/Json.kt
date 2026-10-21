package com.openhamshack.client

import java.math.BigDecimal
import java.math.BigInteger

/**
 * JSON as plain Kotlin values, the way the Python reference holds it as
 * dicts: objects are `Map<String, Any?>`, arrays `List<Any?>`, numbers
 * `Long` (or `BigInteger` past 64 bits) and `Double`, plus `String`,
 * `Boolean` and `null`.
 *
 * Written here rather than taken from kotlinx.serialization or org.json for
 * two reasons. A record is an open bag of fields (SYNC.md §4: a field this
 * library does not know is kept and sent back, T19), so a typed schema
 * would only get in the way. And [dump] must produce exactly what Python's
 * `json.dumps(value, sort_keys=True)` produces: the merge breaks a
 * createdAt tie on the origin's sorted JSON (merge.py), and the controller
 * that a Kotlin client converges with is Python, so the two encodings have
 * to agree byte for byte or the same two copies would merge to different
 * origins. Android's own org.json orders keys by hash and escapes `/`.
 */
object Json {
    class ParseError(message: String) : IllegalArgumentException(message)

    fun parse(text: String): Any? {
        val p = Parser(text)
        p.ws()
        val v = p.value()
        p.ws()
        if (p.i != text.length) throw ParseError("unexpected text at offset ${p.i}")
        return v
    }

    /** An object, or an empty map for anything else (a controller that
     *  answered with a bare value or nothing: transport.py `_json`). */
    @Suppress("UNCHECKED_CAST")
    fun parseObject(text: String): Map<String, Any?> = parse(text) as? Map<String, Any?> ?: emptyMap()

    /** Python's `json.dumps(v, sort_keys=True)`: `", "` and `": "` as
     *  separators, keys sorted by code point, non-ASCII escaped as \uXXXX. */
    fun dump(value: Any?): String = StringBuilder().also { write(it, value) }.toString()

    private fun write(sb: StringBuilder, v: Any?) {
        when (v) {
            null -> sb.append("null")
            is String -> str(sb, v)
            is Boolean -> sb.append(if (v) "true" else "false")
            is Int, is Long, is Short, is Byte, is BigInteger -> sb.append(v.toString())
            is Double -> sb.append(double(v))
            is Float -> sb.append(double(v.toDouble()))
            is BigDecimal -> sb.append(v.toPlainString())
            is Map<*, *> -> {
                sb.append('{')
                // Python sorts str keys by code point; Kotlin compares UTF-16
                // units. They differ only above U+FFFF, which no field name
                // or origin key reaches (field names are [a-z0-9_]).
                val keys = v.keys.map { it as String }.sorted()
                keys.forEachIndexed { n, k ->
                    if (n > 0) sb.append(", ")
                    str(sb, k)
                    sb.append(": ")
                    write(sb, v[k])
                }
                sb.append('}')
            }
            is List<*> -> {
                sb.append('[')
                v.forEachIndexed { n, x ->
                    if (n > 0) sb.append(", ")
                    write(sb, x)
                }
                sb.append(']')
            }
            is Array<*> -> write(sb, v.toList())
            else -> throw IllegalArgumentException("${v::class.java.name} is not a JSON value")
        }
    }

    /** Python's float repr for the values a logbook holds: shortest
     *  round-trip digits, `.0` on integral values, exponent form outside
     *  1e-4..1e16 as Python writes it (`1e+16`). */
    private fun double(d: Double): String {
        if (d.isNaN() || d.isInfinite()) throw IllegalArgumentException("$d is not a JSON number")
        val bd = BigDecimal(d.toString())
        val abs = bd.abs()
        if (abs.signum() != 0 && (abs < BigDecimal("1e-4") || abs >= BigDecimal("1e16"))) {
            val s = bd.stripTrailingZeros()
            val unscaled = s.unscaledValue().abs().toString()
            val exp = unscaled.length - 1 - s.scale()
            val mant = if (unscaled.length == 1) unscaled else unscaled[0] + "." + unscaled.substring(1)
            return (if (s.signum() < 0) "-" else "") + mant + "e" + (if (exp < 0) "-" else "+") +
                Math.abs(exp).toString().padStart(2, '0')
        }
        val plain = bd.stripTrailingZeros().toPlainString()
        return if (plain.contains('.')) plain else "$plain.0"
    }

    private fun str(sb: StringBuilder, s: String) {
        sb.append('"')
        for (ch in s) {
            when {
                ch == '"' -> sb.append("\\\"")
                ch == '\\' -> sb.append("\\\\")
                ch == '\n' -> sb.append("\\n")
                ch == '\r' -> sb.append("\\r")
                ch == '\t' -> sb.append("\\t")
                ch == '\b' -> sb.append("\\b")
                ch == '\u000c' -> sb.append("\\f")
                ch < ' ' || ch.code > 0x7f -> sb.append("\\u").append(Integer.toHexString(ch.code).padStart(4, '0'))
                else -> sb.append(ch)
            }
        }
        sb.append('"')
    }

    private class Parser(val s: String) {
        var i = 0

        fun ws() {
            while (i < s.length && (s[i] == ' ' || s[i] == '\n' || s[i] == '\r' || s[i] == '\t')) i++
        }

        fun value(): Any? {
            if (i >= s.length) throw ParseError("unexpected end of JSON")
            return when (val c = s[i]) {
                '{' -> obj()
                '[' -> arr()
                '"' -> string()
                't' -> lit("true", true)
                'f' -> lit("false", false)
                'n' -> lit("null", null)
                else -> if (c == '-' || c in '0'..'9') number() else throw ParseError("unexpected '$c' at offset $i")
            }
        }

        private fun lit(word: String, v: Any?): Any? {
            if (!s.startsWith(word, i)) throw ParseError("unexpected text at offset $i")
            i += word.length
            return v
        }

        private fun obj(): Map<String, Any?> {
            val out = LinkedHashMap<String, Any?>()
            i++
            ws()
            if (i < s.length && s[i] == '}') { i++; return out }
            while (true) {
                ws()
                if (i >= s.length || s[i] != '"') throw ParseError("expected a key at offset $i")
                val k = string()
                ws()
                if (i >= s.length || s[i] != ':') throw ParseError("expected ':' at offset $i")
                i++
                ws()
                out[k] = value()
                ws()
                if (i < s.length && s[i] == ',') { i++; continue }
                if (i < s.length && s[i] == '}') { i++; return out }
                throw ParseError("expected ',' or '}' at offset $i")
            }
        }

        private fun arr(): List<Any?> {
            val out = ArrayList<Any?>()
            i++
            ws()
            if (i < s.length && s[i] == ']') { i++; return out }
            while (true) {
                ws()
                out.add(value())
                ws()
                if (i < s.length && s[i] == ',') { i++; continue }
                if (i < s.length && s[i] == ']') { i++; return out }
                throw ParseError("expected ',' or ']' at offset $i")
            }
        }

        private fun string(): String {
            val sb = StringBuilder()
            i++
            while (true) {
                if (i >= s.length) throw ParseError("unterminated string")
                val c = s[i++]
                when (c) {
                    '"' -> return sb.toString()
                    '\\' -> {
                        if (i >= s.length) throw ParseError("unterminated string")
                        when (val e = s[i++]) {
                            '"' -> sb.append('"'); '\\' -> sb.append('\\'); '/' -> sb.append('/')
                            'b' -> sb.append('\b'); 'f' -> sb.append('\u000c'); 'n' -> sb.append('\n')
                            'r' -> sb.append('\r'); 't' -> sb.append('\t')
                            'u' -> {
                                if (i + 4 > s.length) throw ParseError("bad \\u escape")
                                sb.append(s.substring(i, i + 4).toInt(16).toChar())
                                i += 4
                            }
                            else -> throw ParseError("bad escape \\$e")
                        }
                    }
                    else -> sb.append(c)
                }
            }
        }

        private fun number(): Any {
            val start = i
            if (s[i] == '-') i++
            while (i < s.length && s[i] in '0'..'9') i++
            var real = false
            if (i < s.length && s[i] == '.') { real = true; i++; while (i < s.length && s[i] in '0'..'9') i++ }
            if (i < s.length && (s[i] == 'e' || s[i] == 'E')) {
                real = true
                i++
                if (i < s.length && (s[i] == '+' || s[i] == '-')) i++
                while (i < s.length && s[i] in '0'..'9') i++
            }
            val lit = s.substring(start, i)
            if (lit == "-" || lit.isEmpty()) throw ParseError("bad number at offset $start")
            if (real) return lit.toDouble()
            return lit.toLongOrNull() ?: BigInteger(lit)
        }
    }
}

// Small accessors for the dynamic maps, so the ports read like the Python.
@Suppress("UNCHECKED_CAST")
internal fun Any?.asMap(): Map<String, Any?> = this as? Map<String, Any?> ?: emptyMap()

@Suppress("UNCHECKED_CAST")
internal fun Any?.asList(): List<Any?> = this as? List<Any?> ?: emptyList()

internal fun Any?.asStr(): String = this as? String ?: ""

internal fun Any?.asLong(default: Long = 0): Long = when (this) {
    is Number -> this.toLong()
    is String -> this.toLongOrNull() ?: default
    else -> default
}

/** A deep copy through the JSON model, so nothing a caller holds can be
 *  changed by a later merge (merge.py deep-copies for the same reason). */
internal fun deepCopy(v: Any?): Any? = when (v) {
    is Map<*, *> -> LinkedHashMap<String, Any?>().also { m -> v.forEach { (k, x) -> m[k as String] = deepCopy(x) } }
    is List<*> -> v.map { deepCopy(it) }
    else -> v
}
