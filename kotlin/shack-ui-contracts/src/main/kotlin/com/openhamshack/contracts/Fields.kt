package com.openhamshack.contracts

/**
 * A message or document that must not be used. [code] is the name the
 * shared vectors use (schema, transmit_action, unknown_component, magic,
 * ...), so a test compares codes and never English; [where] is a path into
 * the document for a server author. Like the Python errors this is a bug
 * report for a developer, not a sentence for an operator.
 */
open class ContractException(val code: String, detail: String, val where: String = "") :
    IllegalArgumentException("$code${if (where.isEmpty()) "" else " at $where"}: $detail")

/**
 * Typed access to a parsed JSON object that fails as the schemas fail.
 *
 * The contracts are JSON Schema files, and the Kotlin client has no schema
 * validator (none that runs on Android without a dependency). Each model's
 * parser therefore restates the schema's rules through these accessors,
 * and the vectors (valid: true/false) are what keep the two in step.
 * Unknown keys are never looked at, which is the "newer server may add a
 * field" rule every contract shares.
 */
class Fields(val map: Map<String, Any?>, val where: String = "") {
    companion object {
        @Suppress("UNCHECKED_CAST")
        fun of(value: Any?, where: String = ""): Fields {
            if (value !is Map<*, *>) throw ContractException("schema", "an object is expected", where)
            return Fields(value as Map<String, Any?>, where)
        }

        /** JSON has one number type: 640.0 is a valid "integer" to JSON
         *  Schema, so a whole Double counts. */
        fun asLong(v: Any?): Long? = when (v) {
            is Long -> v
            is Int -> v.toLong()
            is Double -> if (v == Math.floor(v) && !v.isInfinite()) v.toLong() else null
            is java.math.BigInteger -> if (v.bitLength() < 64) v.toLong() else null
            else -> null
        }

        fun asDouble(v: Any?): Double? = when (v) {
            is Long -> v.toDouble()
            is Int -> v.toDouble()
            is Double -> v
            is java.math.BigInteger -> v.toDouble()
            else -> null
        }
    }

    fun has(key: String) = map.containsKey(key)

    private fun at(key: String) = if (where.isEmpty()) key else "$where.$key"

    fun fail(key: String, detail: String): Nothing = throw ContractException("schema", detail, at(key))

    private fun required(key: String): Any? {
        if (!map.containsKey(key)) fail(key, "is required")
        return map[key]
    }

    fun string(key: String, minLength: Int = 0, maxLength: Int = Int.MAX_VALUE, pattern: Regex? = null): String {
        val v = required(key) as? String ?: fail(key, "must be a string")
        if (v.length < minLength || v.length > maxLength) fail(key, "has the wrong length")
        if (pattern != null && !pattern.containsMatchIn(v)) fail(key, "does not match $pattern")
        return v
    }

    fun optString(key: String, minLength: Int = 0, pattern: Regex? = null): String? =
        if (has(key)) string(key, minLength, pattern = pattern) else null

    fun long(key: String, min: Long = Long.MIN_VALUE, max: Long = Long.MAX_VALUE): Long {
        val v = asLong(required(key)) ?: fail(key, "must be an integer")
        if (v < min || v > max) fail(key, "is out of range")
        return v
    }

    fun optLong(key: String, min: Long = Long.MIN_VALUE, max: Long = Long.MAX_VALUE): Long? =
        if (has(key)) long(key, min, max) else null

    fun number(key: String, min: Double = Double.NEGATIVE_INFINITY, exclusiveMin: Double = Double.NEGATIVE_INFINITY): Double {
        val v = asDouble(required(key)) ?: fail(key, "must be a number")
        if (v < min || v <= exclusiveMin) fail(key, "is out of range")
        return v
    }

    fun optNumber(key: String, min: Double = Double.NEGATIVE_INFINITY, exclusiveMin: Double = Double.NEGATIVE_INFINITY): Double? =
        if (has(key)) number(key, min, exclusiveMin) else null

    fun bool(key: String): Boolean = required(key) as? Boolean ?: fail(key, "must be a boolean")

    fun optBool(key: String): Boolean? = if (has(key)) bool(key) else null

    fun <T> enum(key: String, values: Map<String, T>): T {
        val v = required(key) as? String ?: fail(key, "must be a string")
        return values[v] ?: fail(key, "$v is not one of ${values.keys}")
    }

    fun optEnum(key: String): String? = if (has(key)) string(key) else null

    fun obj(key: String): Fields = Fields.of(required(key), at(key))

    fun optObj(key: String): Fields? = if (has(key)) obj(key) else null

    @Suppress("UNCHECKED_CAST")
    fun list(key: String): List<Any?> = required(key) as? List<Any?> ?: fail(key, "must be an array")

    fun optList(key: String): List<Any?>? = if (has(key)) list(key) else null
}
