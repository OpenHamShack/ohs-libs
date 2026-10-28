package com.openhamshack.spectrum

/**
 * "Latest wins" (CLIENT-UI-PLAN.md §2.4) with no socket in it, the twin of
 * sender.py: at most one pending row per client per stream; a row offered
 * while the client's socket has not drained replaces the pending one, and
 * the next row to go out carries gap. The transport is injected: [send]
 * puts bytes on the client's socket and the transport calls [drained]
 * once that write has gone.
 */
class LatestWins<C : Any>(private val send: (C, ByteArray) -> Unit) {
    private val busy = HashSet<C>()
    private val pending = HashMap<C, java.util.TreeMap<Int, Row>>()
    private val gap = HashSet<Pair<C, Int>>()
    private val seq = HashMap<Pair<C, Int>, Long>()
    var dropped = 0
        private set

    /** The sequence is stamped here, per client and stream, so a dropped
     *  row also shows as a jump in seq. */
    fun offer(client: C, row: Row) {
        val key = client to row.stream
        val n = seq[key] ?: 0L
        seq[key] = (n + 1) and 0xFFFFFFFFL
        val stamped = row.copy(seq = n)
        if (client !in busy) {
            write(client, stamped)
            return
        }
        val held = pending.getOrPut(client) { java.util.TreeMap() }
        if (held.containsKey(row.stream)) {
            dropped++
            gap.add(key)
        }
        held[row.stream] = stamped
    }

    /** The last write has gone: send what is pending, one row per stream,
     *  in stream order. */
    fun drained(client: C) {
        busy.remove(client)
        pending.remove(client)?.values?.forEach { write(client, it) }
    }

    fun forget(client: C) {
        busy.remove(client)
        pending.remove(client)
        gap.removeAll { it.first == client }
        seq.keys.removeAll { it.first == client }
    }

    private fun write(client: C, row: Row) {
        val key = client to row.stream
        val out = if (gap.remove(key)) row.copy(gap = true) else row
        busy.add(client)
        send(client, Rows.encode(out))
    }
}
