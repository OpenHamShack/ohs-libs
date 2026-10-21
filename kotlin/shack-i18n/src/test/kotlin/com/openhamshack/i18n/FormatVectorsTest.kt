package com.openhamshack.i18n

import com.openhamshack.client.Json
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.DynamicTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestFactory
import java.io.File

/**
 * spec/vectors/i18n/format.json (I18N.md §5 item 3), read from the
 * repository and never copied: the Python, Kotlin, Swift and JS helpers
 * must give exactly these results, byte for byte. An expected `error` is a
 * machine code; the English sentence is not compared.
 */
class FormatVectorsTest {
    @Suppress("UNCHECKED_CAST")
    private val vectors = Json.parseObject(File(System.getProperty("shack.spec"), "vectors/i18n/format.json").readText())

    @Suppress("UNCHECKED_CAST")
    private fun cases(section: String) = vectors[section] as List<Map<String, Any?>>

    /** Runs [call] and compares its text, or its error code. */
    private fun expect(case: Map<String, Any?>, want: String, call: () -> Any): DynamicTest =
        DynamicTest.dynamicTest(Json.dump(case.filterKeys { it != "note" })) {
            val got: Any = try {
                call()
            } catch (e: ShackFormat.FormatError) {
                "error:" + e.code
            }
            val expected: Any = if ("error" in case) "error:" + case["error"] else case.getValue(want)!!
            assertEquals(expected, got, case["note"]?.toString() ?: "")
        }

    private fun Any?.int(): Int? = (this as Number?)?.toInt()

    @TestFactory
    fun frequency() = cases("frequency").map { c ->
        expect(c, "text") { ShackFormat.frequency(c["hz"] as Number, c["locale"] as String, c["unit"] as String, c["decimals"].int()) }
    }

    @TestFactory
    fun offset() = cases("offset").map { c ->
        expect(c, "text") { ShackFormat.offset(c["hz"] as Number, c["unit"] as String, c["locale"] as String, c["decimals"].int()) }
    }

    @TestFactory
    fun parseFrequency() = cases("parse_frequency").map { c ->
        expect(c, "hz") { ShackFormat.parseFrequency(c["text"] as String, c["locale"] as String, c["unit"] as String?) }
    }

    @TestFactory
    fun number() = cases("number").map { c ->
        expect(c, "text") { ShackFormat.number(c["x"] as Number, c["locale"] as String, c["decimals"].int()!!) }
    }

    @TestFactory
    fun swr() = cases("swr").map { c ->
        expect(c, "text") { ShackFormat.swr(c["x"] as Number, c["locale"] as String) }
    }

    @Test
    fun theVectorsAreTheVersionThisPortWasWrittenFor() {
        assertEquals(1L, vectors["version"])
    }

    @Test
    fun aJavaLocaleTagFindsItsSymbols() {
        assertEquals(",", Symbols.of(java.util.Locale.GERMANY.toLanguageTag()).decimal)
        assertEquals("’", Symbols.of("de_CH.UTF-8@euro").group)
    }
}
