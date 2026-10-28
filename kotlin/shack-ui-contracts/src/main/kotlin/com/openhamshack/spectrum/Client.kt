package com.openhamshack.spectrum

/**
 * What a client does with a socket's frames, with no socket in it: keeps
 * the stream descriptions (their order numbers the streams), the last
 * levels per stream, and says what each row needs before it may be drawn.
 *
 * The decisions (CLIENT-UI-PLAN.md §2.2, §2.3):
 *  - a `stream` message with a higher rev replaces the description; a lower
 *    or equal rev is stale and ignored, because messages are not reordered
 *    on one socket but a reconnect replays them;
 *  - a row with streamChanged follows a new rev: it is held back
 *    ([Verdict.WaitForStream]) until a description with a rev above the one
 *    it was drawn against has arrived;
 *  - a row is also held back until the stream has `levels`, since without a
 *    window the values mean nothing ([Verdict.WaitForLevels]);
 *  - gap (the flag, or a jump in seq) tells the renderer to draw a break
 *    rather than pretend the rows were consecutive.
 */
class SpectrumClient {
    enum class Verdict { DRAW, WAIT_FOR_STREAM, WAIT_FOR_LEVELS, UNKNOWN_STREAM }

    class Accepted(val verdict: Verdict, val row: Row, val levels: Levels?, val gap: Boolean)

    private val order = ArrayList<String>()
    private val streams = HashMap<String, SpectrumMessage.StreamDescription>()
    private val levels = HashMap<String, SpectrumMessage.LevelsMessage>()
    private val lastSeq = HashMap<Int, Long>()
    // The rev a stream had when its last row was accepted, so a
    // streamChanged row can tell whether the new description has arrived.
    private val drawnRev = HashMap<String, Long>()

    fun stream(name: String) = streams[name]

    fun stream(index: Int): SpectrumMessage.StreamDescription? = order.getOrNull(index)?.let { streams[it] }

    fun levels(name: String) = levels[name]?.levels

    /** Feeds a parsed text frame; returns true when it changed state. */
    fun onMessage(m: SpectrumMessage): Boolean = when (m) {
        is SpectrumMessage.StreamDescription -> {
            val old = streams[m.stream]
            if (old != null && m.rev <= old.rev) {
                false
            } else {
                if (old == null) order.add(m.stream)
                streams[m.stream] = m
                true
            }
        }
        is SpectrumMessage.LevelsMessage -> {
            levels[m.stream] = m
            true
        }
        else -> false
    }

    fun onRow(row: Row): Accepted {
        val desc = stream(row.stream) ?: return Accepted(Verdict.UNKNOWN_STREAM, row, null, row.gap)
        val prev = lastSeq.put(row.stream, row.seq)
        // seq is u32 and wraps; a jump is anything but the next value.
        val jumped = prev != null && row.seq != ((prev + 1) and 0xFFFFFFFFL)
        val gap = row.gap || jumped
        val seen = drawnRev[desc.stream]
        if (row.streamChanged && seen != null && desc.rev <= seen) {
            return Accepted(Verdict.WAIT_FOR_STREAM, row, null, gap)
        }
        val lv = levels(desc.stream) ?: return Accepted(Verdict.WAIT_FOR_LEVELS, row, null, gap)
        drawnRev[desc.stream] = desc.rev
        return Accepted(Verdict.DRAW, row, lv, gap)
    }

    /** A reconnect starts the socket's numbering again. */
    fun reset() {
        order.clear()
        streams.clear()
        levels.clear()
        lastSeq.clear()
        drawnRev.clear()
    }
}
