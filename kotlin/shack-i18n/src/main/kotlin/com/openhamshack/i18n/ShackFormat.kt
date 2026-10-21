package com.openhamshack.i18n

import java.math.BigDecimal
import java.math.RoundingMode

/**
 * Locale formatting for frequencies and numbers (I18N.md §4), the Kotlin
 * port of python/shack_i18n fmt.py. spec/vectors/i18n/format.json is the
 * contract between the ports; this is one implementation of it.
 *
 * Decisions the vectors pin, so a port cannot quietly differ:
 *
 * - Arithmetic is decimal (BigDecimal), never binary floating point, and
 *   ties round half away from zero (HALF_UP on a magnitude). A Double
 *   argument is read through its shortest decimal form first
 *   (`Double.toString`), so `number(2.675, decimals = 2)` is `2.68` as a
 *   person expects.
 * - Frequencies are never grouped: "10,489.750" and "10.489,750" each read
 *   as the other in half the world (§4.1).
 * - The unit follows a no-break space so a line break can never separate a
 *   number from its unit.
 *
 * The decimal, group and minus symbols come from the same CLDR-derived table
 * as the Python library ([Symbols]), not from `DecimalFormatSymbols`, even
 * though I18N.md §4 suggests the platform's data. The platform's data is
 * whatever CLDR the device shipped with: Android 9 (the Fire TV) predates
 * CLDR 34's switch of French grouping to U+202F, so the same call would
 * give different text on two devices in one shack and the shared vectors
 * could not pin it. A table updated with the library is one answer
 * everywhere.
 */
object ShackFormat {
    /** A value the helpers refuse. [code] is the machine code the vectors
     *  compare and a UI translates through its catalog (with [params]); the
     *  message is an English fallback. */
    class FormatError(val code: String, message: String, val params: Map<String, Any?> = emptyMap()) :
        IllegalArgumentException(message)

    /** Hz per unit. */
    val UNITS: Map<String, Long> = linkedMapOf("Hz" to 1L, "kHz" to 1_000L, "MHz" to 1_000_000L, "GHz" to 1_000_000_000L)

    // The most decimals a unit can show is its Hz resolution: anything finer
    // would show digits an integer Hz input cannot have.
    private val MAX_DECIMALS = mapOf("Hz" to 0, "kHz" to 3, "MHz" to 6, "GHz" to 9)

    // §4.1: MHz 3 by default (kHz resolution), kHz 1. GHz is only for a
    // 10 GHz display and keeps kHz resolution as MHz does.
    private val DEFAULT_DECIMALS = mapOf("Hz" to 0, "kHz" to 1, "MHz" to 3, "GHz" to 6)
    private const val AUTO_MHZ_FROM = 30_000_000L

    /** §4.1 fixes U+2212 for signed shifts in every locale, unlike [number],
     *  which takes the locale's own minus sign. */
    private const val SHIFT_MINUS = "−"
    private const val NBSP = " "

    private fun decimal(x: Number): BigDecimal = when (x) {
        is BigDecimal -> x
        is Double -> {
            if (!x.isFinite()) throw FormatError("not-finite", "$x is not a finite number.", mapOf("value" to x))
            BigDecimal(x.toString())
        }
        is Float -> decimal(x.toString().toDouble())
        is java.math.BigInteger -> BigDecimal(x)
        else -> BigDecimal.valueOf(x.toLong())
    }

    private fun checkUnit(unit: String) {
        if (unit !in UNITS) {
            throw FormatError("bad-unit", "\"$unit\" is not a unit these helpers know; use one of ${UNITS.keys.joinToString(", ")}.",
                mapOf("unit" to unit))
        }
    }

    private fun checkDecimals(decimals: Int, unit: String) {
        val top = MAX_DECIMALS.getValue(unit)
        if (decimals !in 0..top) {
            throw FormatError("bad-decimals", "A value in $unit can show between 0 and $top decimal places, not $decimals.",
                mapOf("unit" to unit, "decimals" to decimals))
        }
    }

    /** The absolute value to fixed decimals, half away from zero, with
     *  optional grouping (only over 9999: §4.2). */
    private fun digits(value: BigDecimal, decimals: Int, decimalSep: String, groupSep: String? = null): String {
        val q = value.abs().setScale(decimals, RoundingMode.HALF_UP).toPlainString()
        var whole = q.substringBefore('.')
        val frac = if ('.' in q) q.substringAfter('.') else ""
        if (groupSep != null && whole.length > 4) {
            val head = whole.length % 3
            val first = if (head == 0) 3 else head
            whole = whole.take(first) + (first until whole.length step 3).joinToString("") { groupSep + whole.substring(it, it + 3) }
        }
        return if (decimals > 0) whole + decimalSep + frac else whole
    }

    /** A frequency for display: `frequency(14074000, "en")` is
     *  `"14074.0 kHz"`. [unit] `auto` chooses kHz below 30 MHz and MHz
     *  from 30 MHz up; [decimals] null takes the unit's default, and
     *  trailing zeros are kept so a dial does not change width as it tunes. */
    fun frequency(hz: Number, locale: String = "en", unit: String = "auto", decimals: Int? = null): String {
        val value = decimal(hz)
        if (value.signum() < 0) {
            throw FormatError("negative-frequency", "A frequency cannot be negative; use offset() for a shift.", mapOf("hz" to value))
        }
        val u = if (unit == "auto") (if (value >= BigDecimal.valueOf(AUTO_MHZ_FROM)) "MHz" else "kHz") else unit
        checkUnit(u)
        val d = decimals ?: DEFAULT_DECIMALS.getValue(u)
        checkDecimals(d, u)
        return digits(scaled(value, u), d, Symbols.of(locale).decimal) + NBSP + u
    }

    private fun scaled(value: BigDecimal, unit: String) = value.divide(BigDecimal.valueOf(UNITS.getValue(unit)))

    /** A signed shift or tone: `"+1500 Hz"`, `"−600 kHz"`.
     *  Zero carries no sign. [decimals] null shows as many decimals as the
     *  value needs, because shifts are round numbers (600 kHz, 7.6 MHz). */
    fun offset(hz: Number, unit: String = "Hz", locale: String = "en", decimals: Int? = null): String {
        val value = decimal(hz)
        checkUnit(unit)
        val s = scaled(value, unit)
        val d = decimals ?: minOf(maxOf(0, s.stripTrailingZeros().scale()), MAX_DECIMALS.getValue(unit))
        checkDecimals(d, unit)
        val text = digits(s, d, Symbols.of(locale).decimal)
        val roundedZero = text.all { it == '0' || it == '.' || it == ',' }
        val sign = if (roundedZero) "" else if (value.signum() > 0) "+" else SHIFT_MINUS
        return sign + text + NBSP + unit
    }

    /** A plain number (dB, W, MER, a count) in the locale's symbols, grouped
     *  only over 9999 so a year or 1500 W is never split. */
    fun number(x: Number, locale: String = "en", decimals: Int = 0): String {
        if (decimals !in 0..12) {
            throw FormatError("bad-decimals", "A number can show between 0 and 12 decimal places, not $decimals.", mapOf("decimals" to decimals))
        }
        val value = decimal(x)
        val sym = Symbols.of(locale)
        val text = digits(value, decimals, sym.decimal, sym.group)
        val negative = value.signum() < 0 && text.any { it in '1'..'9' }
        return (if (negative) sym.minus else "") + text
    }

    /** A standing-wave ratio, one decimal: `swr(1.5)` is `"1.5:1"`. */
    fun swr(x: Number, locale: String = "en"): String {
        val value = decimal(x)
        if (value < BigDecimal.ONE) throw FormatError("bad-swr", "An SWR cannot be below 1:1, and $x is.", mapOf("value" to value))
        return digits(value, 1, Symbols.of(locale).decimal) + ":1"
    }

    private val UNIT_BY_LOWER = UNITS.keys.associateBy { it.lowercase(java.util.Locale.ROOT) }
    private val TYPED = Regex("^([0-9.,]+)(\\p{L}*)$")
    private const val UNITS_SENTENCE = "Hz, kHz, MHz or GHz"

    /** What a person typed, as integer Hz. Either `.` or `,` is the decimal
     *  separator, whatever the locale (frequencies are never grouped, so
     *  there is no grouping symbol to confuse it with); spaces of any kind
     *  are ignored; a typed unit (any case) wins over the field's [unit];
     *  with neither, the text is refused rather than guessed. Finer than
     *  1 Hz is refused, not rounded. */
    fun parseFrequency(text: String, locale: String = "en", unit: String? = null): Long {
        if (unit != null && unit != "auto") checkUnit(unit)
        val fieldUnit = if (unit == "auto") null else unit
        val compact = text.filterNot { it.isWhitespace() || it == ' ' || it == ' ' || it == ' ' }
        if (compact.isEmpty()) throw FormatError("empty", "The frequency is empty; type a number such as 14074 kHz.")
        val m = TYPED.matchEntire(compact)
        if (m == null || m.groupValues[1].none { it.isDigit() }) {
            throw FormatError("not-a-number", "\"$text\" is not a frequency; type digits with at most one decimal separator and an optional unit ($UNITS_SENTENCE).",
                mapOf("text" to text))
        }
        val num = m.groupValues[1]
        val typed = m.groupValues[2]
        if (num.count { it == '.' || it == ',' } > 1) {
            throw FormatError("two-separators", "\"$text\" has more than one decimal separator; type the frequency with a single \".\" or \",\" and no digit grouping.",
                mapOf("text" to text))
        }
        val chosen = when {
            typed.isNotEmpty() -> UNIT_BY_LOWER[typed.lowercase(java.util.Locale.ROOT)]
                ?: throw FormatError("unknown-unit", "\"$typed\" is not a frequency unit; use $UNITS_SENTENCE.", mapOf("unit" to typed))
            fieldUnit != null -> fieldUnit
            else -> throw FormatError("no-unit", "\"$text\" has no unit; add $UNITS_SENTENCE.", mapOf("text" to text))
        }
        val n = num.replace(',', '.')
        val whole = n.substringBefore('.').ifEmpty { "0" }
        val frac = (if ('.' in n) n.substringAfter('.') else "").ifEmpty { "0" }
        val hz = BigDecimal("$whole.$frac").multiply(BigDecimal.valueOf(UNITS.getValue(chosen)))
        if (hz.stripTrailingZeros().scale() > 0) {
            throw FormatError("too-precise", "\"$text\" is finer than 1 Hz; use fewer decimal places.", mapOf("text" to text))
        }
        return hz.toBigIntegerExact().toLong()
    }
}

/**
 * Decimal, group and minus symbols per locale: the same CLDR-derived table
 * as python/shack_i18n _cldr.py (where the values came from and how they
 * were checked is written there). Add a locale to both, escaping anything
 * that is not printable ASCII so a reviewer can see which space is which.
 */
data class Symbols(val decimal: String, val group: String, val minus: String) {
    companion object {
        private const val HYPHEN = "-"
        private const val MINUS = "−"
        private const val NBSP = " "
        private const val NNBSP = " "

        val TABLE: Map<String, Symbols> = mapOf(
            "en" to Symbols(".", ",", HYPHEN), "en-GB" to Symbols(".", ",", HYPHEN), "en-US" to Symbols(".", ",", HYPHEN),
            "de" to Symbols(",", ".", HYPHEN), "de-AT" to Symbols(",", NBSP, HYPHEN), "de-CH" to Symbols(".", "’", HYPHEN),
            "fr" to Symbols(",", NNBSP, HYPHEN), "fr-CA" to Symbols(",", NBSP, HYPHEN),
            "es" to Symbols(",", ".", HYPHEN), "es-MX" to Symbols(".", ",", HYPHEN),
            "it" to Symbols(",", ".", HYPHEN), "nl" to Symbols(",", ".", HYPHEN),
            "nb" to Symbols(",", NBSP, MINUS), "nn" to Symbols(",", NBSP, MINUS), "no" to Symbols(",", NBSP, MINUS),
            "sv" to Symbols(",", NBSP, MINUS), "da" to Symbols(",", ".", HYPHEN), "fi" to Symbols(",", NBSP, MINUS),
            "pl" to Symbols(",", NBSP, HYPHEN),
            // CLDR's plain "pt" is Brazilian Portuguese; Portugal is the variant.
            "pt" to Symbols(",", ".", HYPHEN), "pt-BR" to Symbols(",", ".", HYPHEN), "pt-PT" to Symbols(",", NBSP, HYPHEN),
            "ja" to Symbols(".", ",", HYPHEN), "zh" to Symbols(".", ",", HYPHEN),
            "ru" to Symbols(",", NBSP, HYPHEN), "uk" to Symbols(",", NBSP, HYPHEN), "cs" to Symbols(",", NBSP, HYPHEN),
        )

        /** Symbols for a BCP 47 or POSIX tag: exact tag, then language, then
         *  en. POSIX spellings (`de_DE.UTF-8`) lose the encoding and
         *  modifier; an unlisted region takes its language's symbols. A
         *  `java.util.Locale` gives its tag with `toLanguageTag()`. */
        fun of(locale: String?): Symbols {
            if (locale.isNullOrEmpty()) return TABLE.getValue("en")
            val tag = locale.substringBefore('.').substringBefore('@').replace('_', '-')
            val parts = tag.split('-').filter { it.isNotEmpty() }
            if (parts.isEmpty()) return TABLE.getValue("en")
            val lang = parts[0].lowercase(java.util.Locale.ROOT)
            val region = parts.drop(1).firstOrNull { it.length == 2 && it.all(Char::isLetter) }?.uppercase(java.util.Locale.ROOT)
            if (region != null) TABLE["$lang-$region"]?.let { return it }
            return TABLE[lang] ?: TABLE.getValue("en")
        }
    }
}
