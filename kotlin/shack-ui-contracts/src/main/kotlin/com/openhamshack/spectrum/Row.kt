package com.openhamshack.spectrum

import com.openhamshack.contracts.ContractException
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * The spectrum/1 binary row (CLIENT-UI-PLAN.md §2.3), the twin of
 * python/shack_spectrum row.py: a 32-byte little-endian header, then n
 * columns of uint8 or uint16.
 *
 * A reader checks magic and version and refuses anything else rather than
 * guessing, because a misread header draws a plausible but wrong
 * waterfall, which is worse than none. A frame is exactly one row, so the
 * length must match exactly (trailing bytes mean the layouts disagree).
 * Flag bits 4-7 are ignored, so a later spectrum/1 may add one.
 */
class RowException(code: String, detail: String) : ContractException(code, detail)

/**
 * [values] holds the raw column values (0..255, or 0..65535 when
 * [depth] is 16) as Int so one array type serves both depths. [seq] is the
 * unsigned 32-bit sequence and [tMs] the unsigned 64-bit capture time; both
 * are Long, which holds every value a box will ever send (a time past
 * 2^63 ms would wrap negative, and is 292 million years away).
 */
class Row(
    val stream: Int,
    val seq: Long,
    val tMs: Long,
    val loHz: Double,
    val colHz: Double,
    val values: IntArray,
    val depth: Int = 8,
    val levelsChanged: Boolean = false,
    val streamChanged: Boolean = false,
    val gap: Boolean = false,
) {
    val flags: Int
        get() = (if (depth == 16) Rows.DEPTH16 else 0) or (if (levelsChanged) Rows.LEVELS_CHANGED else 0) or
            (if (streamChanged) Rows.STREAM_CHANGED else 0) or (if (gap) Rows.GAP else 0)

    fun copy(seq: Long = this.seq, gap: Boolean = this.gap) =
        Row(stream, seq, tMs, loHz, colHz, values, depth, levelsChanged, streamChanged, gap)
}

object Rows {
    const val HEADER = 32
    const val VERSION = 1
    const val DEPTH16 = 0x01
    const val LEVELS_CHANGED = 0x02
    const val STREAM_CHANGED = 0x04
    const val GAP = 0x08

    fun decode(data: ByteArray): Row {
        if (data.size < HEADER) throw RowException("length", "${data.size} bytes is shorter than the header")
        val b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)
        if (data[0] != 'O'.code.toByte() || data[1] != 'S'.code.toByte()) throw RowException("magic", "not OS")
        val version = data[2].toInt() and 0xFF
        if (version != VERSION) throw RowException("version", "version $version; this reader knows $VERSION")
        val flags = data[3].toInt() and 0xFF
        val stream = b.getShort(4).toInt() and 0xFFFF
        val n = b.getShort(6).toInt() and 0xFFFF
        val seq = b.getInt(8).toLong() and 0xFFFFFFFFL
        val tMs = b.getLong(12)
        val loHz = b.getDouble(20)
        // colHz is a float32 on the wire; widening is exact, and the
        // vectors compare the widened value.
        val colHz = b.getFloat(28).toDouble()
        val wide = flags and DEPTH16 != 0
        val size = if (wide) 2 else 1
        val want = HEADER + n * size
        if (data.size != want) throw RowException("length", "${data.size} bytes for $n columns of $size; want $want")
        val values = IntArray(n) { i ->
            if (wide) b.getShort(HEADER + 2 * i).toInt() and 0xFFFF else data[HEADER + i].toInt() and 0xFF
        }
        return Row(
            stream, seq, tMs, loHz, colHz, values, if (wide) 16 else 8,
            levelsChanged = flags and LEVELS_CHANGED != 0,
            streamChanged = flags and STREAM_CHANGED != 0,
            gap = flags and GAP != 0,
        )
    }

    fun encode(row: Row): ByteArray {
        if (row.depth != 8 && row.depth != 16) throw RowException("depth", "depth must be 8 or 16")
        if (row.values.size > 0xFFFF) throw RowException("columns", "at most 65535 columns")
        val size = row.depth / 8
        val limit = (1 shl row.depth) - 1
        if (row.values.any { it < 0 || it > limit }) throw RowException("depth", "a value does not fit ${row.depth} bits")
        val b = ByteBuffer.allocate(HEADER + row.values.size * size).order(ByteOrder.LITTLE_ENDIAN)
        b.put('O'.code.toByte()).put('S'.code.toByte()).put(VERSION.toByte()).put(row.flags.toByte())
        b.putShort(row.stream.toShort()).putShort(row.values.size.toShort()).putInt(row.seq.toInt())
        b.putLong(row.tMs).putDouble(row.loHz).putFloat(row.colHz.toFloat())
        for (v in row.values) if (size == 2) b.putShort(v.toShort()) else b.put(v.toByte())
        return b.array()
    }
}
