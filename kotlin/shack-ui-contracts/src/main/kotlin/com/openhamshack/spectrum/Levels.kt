package com.openhamshack.spectrum

import kotlin.math.ceil
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/**
 * Value to dB and back (CLIENT-UI-PLAN.md §2.3): dB = dbFloor + v * dbStep,
 * 0 meaning "at or below the floor". A client holds the last `levels`
 * message and applies it to each row; [Rows]' levelsChanged flag says the
 * window moved with that row.
 */
data class Levels(val dbFloor: Double, val dbStep: Double) {
    fun dequantise(values: IntArray): DoubleArray = DoubleArray(values.size) { dbFloor + values[it] * dbStep }

    fun dequantise(v: Int): Double = dbFloor + v * dbStep

    companion object {
        /** dB to row value: clamp(floor((dB - floor)/step + 0.5), 0, top).
         *  Rounds half up rather than half-to-even so every language's
         *  vectors agree on the .5 cases. */
        fun quantise(db: DoubleArray, dbFloor: Double, dbStep: Double, depth: Int = 8): IntArray {
            val top = (1 shl depth) - 1
            return IntArray(db.size) { floor((db[it] - dbFloor) / dbStep + 0.5).coerceIn(0.0, top.toDouble()).toInt() }
        }
    }
}

/** The pure per-client reduction of §2.1, the twin of reduce.py. Every
 *  boundary is integer arithmetic on bin indices, so a feed adapter in any
 *  language picks the same bins for a column. */
object Reduce {
    /** The source bins [i0, i1) covering loHz..hiHz, widened to whole bins
     *  and clipped to the span. */
    fun crop(srcLoHz: Double, binHz: Double, nBins: Int, loHz: Double?, hiHz: Double?): Pair<Int, Int> {
        var i0 = if (loHz == null) 0 else floor((loHz - srcLoHz) / binHz).toInt()
        var i1 = if (hiHz == null) nBins else ceil((hiHz - srcLoHz) / binHz).toInt()
        i0 = i0.coerceIn(0, nBins - 1)
        i1 = max(i1, i0 + 1).coerceAtMost(nBins)
        return i0 to i1
    }

    class Columns(val loHz: Double, val colHz: Double, val db: DoubleArray)

    /** One client's columns from a source row of dB bins; never more
     *  columns than bins. mean is the power average, not the dB average. */
    fun reduce(
        srcDb: DoubleArray, srcLoHz: Double, binHz: Double, cols: Int,
        loHz: Double? = null, hiHz: Double? = null, mode: String = "peak",
    ): Columns {
        if (mode != "peak" && mode != "mean") throw SpectrumException("reduce", "reduce must be peak or mean, not $mode")
        if (cols < 1) throw SpectrumException("cols", "cols must be at least 1")
        val (i0, i1) = crop(srcLoHz, binHz, srcDb.size, loHz, hiHz)
        val m = i1 - i0
        val n = min(cols, m)
        // Column k takes bins [k*m/n, (k+1)*m/n) of the crop; Long so
        // k*m cannot overflow on a very wide source.
        val out = DoubleArray(n) { k ->
            val a = i0 + (k.toLong() * m / n).toInt()
            val b = i0 + ((k + 1).toLong() * m / n).toInt()
            if (mode == "peak") {
                var best = srcDb[a]
                for (i in a + 1 until b) best = max(best, srcDb[i])
                best
            } else {
                var sum = 0.0
                for (i in a until b) sum += 10.0.pow(srcDb[i] / 10.0)
                10.0 * log10(sum / (b - a))
            }
        }
        return Columns(srcLoHz + i0 * binHz, m * binHz / n, out)
    }
}

/**
 * The server's quantisation window with hysteresis (§2.3), the twin of
 * levels.py. A client never needs it (the window arrives as a `levels`
 * message); it is here so a Kotlin feed adapter or the test fakebox can
 * be a spectrum server, and because the vectors pin it for every port.
 */
class LevelsWindow {
    var floor: Double? = null
        private set
    var top: Double? = null
        private set
    var noiseDb: Double? = null
        private set
    var peakDb: Double? = null
        private set
    private var breachSince: Long? = null

    /** True when the window moved (the server then sends `levels` and
     *  sets levelsChanged on each client's next row). */
    fun update(db: DoubleArray, tMs: Long): Boolean {
        val noise = percentile(db, NOISE_PCT)
        val wantFloor = noise - BELOW_NOISE_DB
        val wantTop = max(wantFloor + MIN_RANGE_DB, percentile(db, PEAK_PCT) + ABOVE_PEAK_DB)
        noiseDb = noise
        peakDb = db.max()
        val f = floor
        val t = top
        if (f == null || t == null) {
            floor = wantFloor
            top = wantTop
            return true
        }
        if (kotlin.math.abs(wantFloor - f) <= HYSTERESIS_DB && kotlin.math.abs(wantTop - t) <= HYSTERESIS_DB) {
            breachSince = null
            return false
        }
        val since = breachSince ?: tMs.also { breachSince = it }
        if (tMs - since < HOLD_MS) return false
        // Where the signal is now, not where it first left: after 2 s the
        // latest row is the best estimate.
        floor = wantFloor
        top = wantTop
        breachSince = null
        return true
    }

    /** dB per step: the window over 2^depth steps, so a 96 dB window is
     *  0.375 dB exactly. */
    fun step(depth: Int = 8): Double = (top!! - floor!!) / (1 shl depth)

    companion object {
        const val NOISE_PCT = 20.0
        const val PEAK_PCT = 99.7
        const val BELOW_NOISE_DB = 10.0
        const val MIN_RANGE_DB = 60.0
        const val ABOVE_PEAK_DB = 6.0
        const val HYSTERESIS_DB = 3.0
        const val HOLD_MS = 2000L

        /** numpy's default: linear between closest ranks (R type 7). */
        fun percentile(values: DoubleArray, pct: Double): Double {
            val s = values.sortedArray()
            val rank = (s.size - 1) * pct / 100.0
            val lo = floor(rank).toInt()
            val hi = ceil(rank).toInt()
            return s[lo] + (s[hi] - s[lo]) * (rank - lo)
        }
    }
}

/**
 * The display's auto-levels (§4.2), which the client runs itself from the
 * `noiseDb`/`peakDb` the server repeats about once a second: floor = an
 * exponential average of noiseDb minus 3 dB, range = clamp(peakDb - floor
 * + 6, 30, 90) dB. Different from the server's quantisation window above:
 * this is faster and only moves what is drawn.
 *
 * "Attack 0.5 s, decay 3 s" is read as: a rising noise floor is followed
 * with a 0.5 s time constant and a falling one with 3 s, so a burst of
 * noise opens the floor at once and the display settles back slowly rather
 * than flickering. (The plan does not say which direction attack means; a
 * peak-meter reading would be the reverse, and [attackS]/[decayS] swap
 * to give it.) The first reading is taken as is.
 */
class DisplayLevels(private val attackS: Double = 0.5, private val decayS: Double = 3.0) {
    private var avg: Double? = null
    private var lastMs: Long = 0

    class Window(val floor: Double, val range: Double)

    fun update(noiseDb: Double, peakDb: Double, tMs: Long): Window {
        val prev = avg
        val next = if (prev == null) {
            noiseDb
        } else {
            val dt = max(0L, tMs - lastMs) / 1000.0
            val tau = if (noiseDb > prev) attackS else decayS
            prev + (noiseDb - prev) * (1.0 - exp(-dt / tau))
        }
        avg = next
        lastMs = tMs
        val floor = next - FLOOR_MARGIN_DB
        return Window(floor, (peakDb - floor + HEADROOM_DB).coerceIn(MIN_RANGE_DB, MAX_RANGE_DB))
    }

    companion object {
        const val FLOOR_MARGIN_DB = 3.0
        const val HEADROOM_DB = 6.0
        const val MIN_RANGE_DB = 30.0
        const val MAX_RANGE_DB = 90.0
    }
}
