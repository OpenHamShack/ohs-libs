package com.openhamshack.spectrum

import com.openhamshack.client.Json
import com.openhamshack.spectrum.SpectrumClient.Verdict
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The client-side decisions no vector covers: rev, levels, gap. */
class ClientTest {
    private fun stream(name: String, rev: Int) = SpectrumMessage.parse(
        Json.parse(
            """{"type":"stream","stream":"$name","rev":$rev,"centreHz":1,"spanHz":1,"loHz":0,"hiHz":1,
               "binHz":1,"srcBins":1,"rate":1,"units":"dB","calibrated":false}""",
        ),
    )

    private val levels = SpectrumMessage.parse(
        Json.parse("""{"type":"levels","stream":"main","dbFloor":-140,"dbStep":0.5,"noiseDb":-130,"peakDb":-60}"""),
    )

    private fun row(seq: Long, streamChanged: Boolean = false, gap: Boolean = false, stream: Int = 0) =
        Row(stream, seq, 0, 0.0, 1.0, intArrayOf(1), streamChanged = streamChanged, gap = gap)

    @Test
    fun aRowWaitsForItsStreamAndItsLevels() {
        val c = SpectrumClient()
        assertEquals(Verdict.UNKNOWN_STREAM, c.onRow(row(0)).verdict)
        c.onMessage(stream("main", 1))
        assertEquals(Verdict.WAIT_FOR_LEVELS, c.onRow(row(1)).verdict)
        c.onMessage(levels)
        val a = c.onRow(row(2))
        assertEquals(Verdict.DRAW, a.verdict)
        assertEquals(Levels(-140.0, 0.5), a.levels)
    }

    @Test
    fun streamChangedHoldsTheRowUntilTheNewDescriptionArrives() {
        val c = SpectrumClient()
        c.onMessage(stream("main", 1))
        c.onMessage(levels)
        assertEquals(Verdict.DRAW, c.onRow(row(0)).verdict)
        assertEquals(Verdict.WAIT_FOR_STREAM, c.onRow(row(1, streamChanged = true)).verdict)
        assertTrue(c.onMessage(stream("main", 2)))
        assertEquals(Verdict.DRAW, c.onRow(row(2, streamChanged = true)).verdict)
    }

    @Test
    fun aStaleDescriptionIsIgnoredAndStreamsAreNumberedByArrival() {
        val c = SpectrumClient()
        c.onMessage(stream("main", 5))
        c.onMessage(stream("zoom", 1))
        assertFalse(c.onMessage(stream("main", 4)))
        assertEquals(5L, c.stream("main")!!.rev)
        assertEquals("zoom", c.stream(1)!!.stream)
    }

    @Test
    fun gapIsTheFlagOrAJumpInSeqIncludingAcrossTheWrap() {
        val c = SpectrumClient()
        c.onMessage(stream("main", 1))
        c.onMessage(levels)
        assertFalse(c.onRow(row(0xFFFFFFFEL)).gap)
        assertFalse(c.onRow(row(0xFFFFFFFFL)).gap)
        assertFalse(c.onRow(row(0)).gap)
        assertTrue(c.onRow(row(5)).gap)
        assertTrue(c.onRow(row(6, gap = true)).gap)
    }

    @Test
    fun displayLevelsFollowARisingFloorFasterThanAFallingOne() {
        val d = DisplayLevels()
        val first = d.update(-130.0, -60.0, 0)
        assertEquals(-133.0, first.floor, 1e-9)
        assertEquals(79.0, first.range, 1e-9)
        val up = d.update(-120.0, -60.0, 500)   // one attack time constant
        assertEquals(-130.0 + 10 * (1 - Math.exp(-1.0)) - 3.0, up.floor, 1e-9)
        val back = DisplayLevels().apply { update(-120.0, -60.0, 0) }.update(-130.0, -60.0, 500)
        assertEquals(-120.0 - 10 * (1 - Math.exp(-0.5 / 3.0)) - 3.0, back.floor, 1e-9)
        assertEquals(30.0, DisplayLevels().update(-130.0, -128.0, 0).range, 1e-9)
        assertEquals(90.0, DisplayLevels().update(-130.0, 0.0, 0).range, 1e-9)
    }
}
