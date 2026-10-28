package com.openhamshack.spectrum

import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Fields

/** Misuse of this library (a bug for the app's developer), as opposed to a
 *  bad message from the box, which is a [ContractException] with code
 *  `schema`. */
class SpectrumException(code: String, detail: String) : ContractException(code, detail)

/**
 * The JSON control messages of a spectrum socket (spectrum.schema.json).
 * [parse] accepts exactly what the schema's oneOf accepts, so a client
 * ignoring an invalid message behaves as the vectors say. Unknown
 * properties are ignored, and an unknown `type` is refused: the closed set
 * is what `oneOf` means, and a newer message needs spectrum/2.
 */
sealed class SpectrumMessage {
    /** What a client sends: only `subscribe` is ever built here. */
    data class Subscribe(
        val stream: String,
        val cols: Int,
        val fps: Double? = null,
        val loHz: Double? = null,
        val hiHz: Double? = null,
        val reduce: String = "peak",
        val depth: Int = 8,
    ) : SpectrumMessage() {
        /** The text frame; defaults are left out so the server applies
         *  its own (per-client fps cap, whole span). */
        fun toMap(): Map<String, Any?> = buildMap {
            put("type", "subscribe")
            put("stream", stream)
            put("cols", cols.toLong())
            fps?.let { put("fps", it) }
            loHz?.let { put("loHz", it) }
            hiHz?.let { put("hiHz", it) }
            put("reduce", reduce)
            put("depth", depth.toLong())
        }
    }

    /** One stream's description. The order of `stream` messages on a
     *  socket numbers the streams: the row's stream index is that order
     *  (see [StreamTable]). */
    data class StreamDescription(
        val stream: String,
        val rev: Long,
        val centreHz: Double,
        val spanHz: Double,
        val loHz: Double,
        val hiHz: Double,
        val binHz: Double,
        val rbwHz: Double?,
        val srcBins: Int,
        val rate: Double,
        val units: String,
        val calibrated: Boolean,
        val dcBins: Pair<Long, Long>?,
        val pinnedBy: List<String>,
        val label: String?,
    ) : SpectrumMessage() {
        /** Only `dBm` may be printed as dBm; `dB` is an engine's own
         *  uncalibrated scale and `rel` is Icom's 0..160 mapped to dB. */
        val mayPrintDbm: Boolean get() = units == "dBm"
    }

    data class LevelsMessage(
        val stream: String,
        val dbFloor: Double,
        val dbStep: Double,
        val noiseDb: Double,
        val peakDb: Double,
    ) : SpectrumMessage() {
        val levels: Levels get() = Levels(dbFloor, dbStep)
    }

    /** `code` is an open string: a client shows an unknown one as a
     *  generic failure. `busy` is the ninth client. */
    data class Error(val code: String, val stream: String?, val message: String?) : SpectrumMessage()

    companion object {
        private val REDUCE = mapOf("peak" to "peak", "mean" to "mean")
        private val UNITS = mapOf("dBm" to "dBm", "dB" to "dB", "rel" to "rel")

        fun parse(value: Any?): SpectrumMessage {
            val f = Fields.of(value)
            return when (f.optEnum("type")) {
                "subscribe" -> Subscribe(
                    stream = f.string("stream", 1),
                    cols = f.long("cols", 1, Int.MAX_VALUE.toLong()).toInt(),
                    fps = f.optNumber("fps", exclusiveMin = 0.0),
                    loHz = f.optNumber("loHz", 0.0),
                    hiHz = f.optNumber("hiHz", 0.0),
                    reduce = if (f.has("reduce")) f.enum("reduce", REDUCE) else "peak",
                    depth = if (f.has("depth")) {
                        f.long("depth").also { if (it != 8L && it != 16L) f.fail("depth", "depth is 8 or 16") }.toInt()
                    } else 8,
                )
                "stream" -> StreamDescription(
                    stream = f.string("stream", 1),
                    rev = f.long("rev", 0),
                    centreHz = f.number("centreHz", 0.0),
                    spanHz = f.number("spanHz", exclusiveMin = 0.0),
                    loHz = f.number("loHz", 0.0),
                    hiHz = f.number("hiHz", 0.0),
                    binHz = f.number("binHz", exclusiveMin = 0.0),
                    rbwHz = f.optNumber("rbwHz", exclusiveMin = 0.0),
                    srcBins = f.long("srcBins", 1, Int.MAX_VALUE.toLong()).toInt(),
                    rate = f.number("rate", exclusiveMin = 0.0),
                    units = f.enum("units", UNITS),
                    calibrated = f.bool("calibrated"),
                    dcBins = f.optList("dcBins")?.let { l ->
                        if (l.size != 2) f.fail("dcBins", "two bins: first and last")
                        val a = Fields.asLong(l[0]) ?: f.fail("dcBins", "integers")
                        val b = Fields.asLong(l[1]) ?: f.fail("dcBins", "integers")
                        if (a < 0 || b < 0) f.fail("dcBins", "not negative")
                        a to b
                    },
                    pinnedBy = f.optObj("pinned")?.optList("by")?.map { it as? String ?: f.fail("pinned", "strings") }
                        ?: emptyList(),
                    label = f.optString("label"),
                )
                "levels" -> LevelsMessage(
                    stream = f.string("stream", 1),
                    dbFloor = f.number("dbFloor"),
                    dbStep = f.number("dbStep", exclusiveMin = 0.0),
                    noiseDb = f.number("noiseDb"),
                    peakDb = f.number("peakDb"),
                )
                "error" -> Error(f.string("code", 1), f.optString("stream"), f.optString("message"))
                else -> f.fail("type", "not a spectrum/1 message")
            }
        }
    }
}
