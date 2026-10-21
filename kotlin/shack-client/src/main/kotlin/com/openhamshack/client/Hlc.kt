package com.openhamshack.client

import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException

/**
 * Hybrid logical clocks (SYNC.md §3; Kulkarni et al., 2014), a port of
 * python/shack_client hlc.py.
 *
 * A stamp orders by plain string comparison: wall time, then count, then
 * node. That is what makes last-edit-wins safe without a central clock:
 * after a store has seen an edit, everything it does next sorts after it,
 * whatever its own clock says (the receive rule), and two edits never share
 * a stamp (the node differs, or the count rises).
 *
 *     000001a1202d7194-0003-st_K4M2Q7Z9XW3A
 *     wall ms, 16 hex   count  store id (node)
 *
 * State lives with the caller (the store keeps it in its meta table, inside
 * the transaction of the edit it stamps): these are pure functions.
 */
object Hlc {
    /** No stamp is ever earlier than the library's build date, so a device
     *  with no RTC that boots believing it is 1970 (§3.3, T9) still stamps
     *  after every edit made before this library existed. The same date as
     *  the Python library's; raise both with each release. */
    val CLOCK_FLOOR_MS: Long = Instant.parse("2026-10-09T00:00:00Z").toEpochMilli()

    /** The controller refuses edits more than this far ahead of its clock
     *  (§3.3): a stamp days in the future would beat every real edit for days. */
    const val MAX_AHEAD_MS: Long = 10 * 60 * 1000

    class HlcError(message: String) : IllegalArgumentException(message)

    data class Parts(val wall: Long, val count: Int, val node: String)

    data class State(val wall: Long, val count: Int)

    private val RE = Regex("^([0-9a-f]{16})-([0-9a-f]{4})-([A-Za-z0-9_]+)$")

    fun fmt(wall: Long, count: Int, node: String): String =
        java.lang.Long.toHexString(wall).padStart(16, '0') + "-" + Integer.toHexString(count).padStart(4, '0') + "-" + node

    fun parse(s: String?): Parts {
        val m = RE.matchEntire(s ?: "") ?: throw HlcError("'$s' is not a hybrid logical clock")
        return Parts(m.groupValues[1].toLong(16), m.groupValues[2].toInt(16), m.groupValues[3])
    }

    fun wallOf(s: String?): Long = if (s.isNullOrEmpty()) 0 else parse(s).wall

    fun nodeOf(s: String): String = s.substringAfterLast('-')

    fun systemMs(): Long = System.currentTimeMillis()

    /** `now()` of §3.2: the system clock plus the measured offset, never
     *  below the clock floor. */
    fun corrected(nowMs: Long, offsetMs: Long): Long = maxOf(nowMs + offsetMs, CLOCK_FLOOR_MS)

    /** A local edit: the new state and its stamp. */
    fun local(wall: Long, count: Int, node: String, now: Long): Pair<State, String> {
        var w = maxOf(wall, now)
        var c = if (w == wall) count + 1 else 0
        if (c > 0xFFFF) {
            // 65536 edits within one millisecond of one store: move the wall
            // on rather than wrap the count, which would break the ordering.
            w += 1
            c = 0
        }
        return State(w, c) to fmt(w, c, node)
    }

    /** Seeing a remote stamp: everything this store does next is later. */
    fun receive(wall: Long, count: Int, remote: String, now: Long): State {
        val r = parse(remote)
        var w = maxOf(wall, r.wall, now)
        var c = when {
            w == wall && w == r.wall -> maxOf(count, r.count) + 1
            w == wall -> count + 1
            w == r.wall -> r.count + 1
            else -> 0
        }
        if (c > 0xFFFF) {
            w += 1
            c = 0
        }
        return State(w, c)
    }

    private val ISO_MS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC)

    /** ISO 8601 UTC with milliseconds and Z (SERVICES.md §1). */
    fun isoMs(ms: Long): String = ISO_MS.format(Instant.ofEpochMilli(ms))

    /** Milliseconds from an ISO 8601 time; a time without an offset is UTC
     *  (Python's `parse_iso_ms`). Throws IllegalArgumentException. */
    fun parseIsoMs(s: String): Long {
        val t = s.trim()
        try {
            return OffsetDateTime.parse(t.replace("z", "Z")).toInstant().toEpochMilli()
        } catch (_: DateTimeParseException) {
        }
        try {
            return java.time.LocalDateTime.parse(t).toInstant(ZoneOffset.UTC).toEpochMilli()
        } catch (_: DateTimeParseException) {
        }
        try {
            return java.time.LocalDate.parse(t).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()
        } catch (e: DateTimeParseException) {
            throw IllegalArgumentException("'$s' is not an ISO date and time")
        }
    }
}
