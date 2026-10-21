package com.openhamshack.client

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import kotlin.random.Random

/** The merge's algebra and the HLC's ordering: what makes sync safe
 *  whatever order copies meet in (SYNC.md §1 rule 2, §3, §4). A port of
 *  python/shack_client tests/test_merge.py. */
class MergeTest {
    private val nodes = listOf("st_AAAAAAAAAAAA", "st_BBBBBBBBBBBB", "st_CCCCCCCCCCCC")
    private fun h(n: Int, node: String = nodes[0]) = Hlc.fmt(Hlc.CLOCK_FLOOR_MS + n, 0, node)

    private fun randomRecord(rng: Random): MutableMap<String, Any?> {
        val fields = LinkedHashMap<String, Any?>()
        val hlcs = LinkedHashMap<String, String>()
        for (f in listOf("call", "rst_rcvd", "rst_sent", "comment", "qsl_sent").shuffled(rng).take(rng.nextInt(1, 6))) {
            val s = Hlc.fmt(Hlc.CLOCK_FLOOR_MS + rng.nextInt(0, 21), rng.nextInt(0, 4), nodes.random(rng))
            hlcs[f] = s
            if (rng.nextDouble() > 0.2) fields[f] = "$f-${s.takeLast(4)}-${rng.nextInt(0, 10)}"
        }
        val dh = if (rng.nextDouble() < 0.4) Hlc.fmt(Hlc.CLOCK_FLOOR_MS + rng.nextInt(0, 21), 0, nodes.random(rng)) else null
        return linkedMapOf("collection" to "logbook", "id" to "q", "fields" to fields, "fieldHlc" to hlcs, "deletedHlc" to dh,
            "createdAt" to "2026-10-09T10:00:${rng.nextInt(10, 60)}.000Z", "origin" to mapOf("app" to listOf("x", "y", "z").random(rng)))
    }

    /** The same stamp always carries the same value, as real edits do. */
    @Suppress("UNCHECKED_CAST")
    private fun consistent(recs: List<MutableMap<String, Any?>>): List<MutableMap<String, Any?>> {
        val seen = HashMap<Pair<String, String>, Any?>()
        val present = HashMap<Pair<String, String>, Boolean>()
        for (r in recs) {
            val fields = r["fields"] as MutableMap<String, Any?>
            for ((f, s) in Merge.fieldHlc(r)) {
                val key = f to s
                if (key in present) {
                    if (present.getValue(key)) fields[f] = seen[key] else fields.remove(f)
                } else {
                    present[key] = f in fields
                    seen[key] = fields[f]
                }
            }
        }
        return recs
    }

    @Test
    fun mergeIsCommutativeAssociativeIdempotent() {
        for (seed in 0 until 300) {
            val rng = Random(seed)
            val (a, b, c) = consistent(List(3) { randomRecord(rng) })
            assertTrue(Merge.same(Merge.merge(a, b), Merge.merge(b, a)), "commutative, seed $seed")
            assertTrue(Merge.same(Merge.merge(Merge.merge(a, b), c), Merge.merge(a, Merge.merge(b, c))), "associative, seed $seed")
            assertTrue(Merge.same(Merge.merge(a, a), a), "idempotent, seed $seed")
            val want = Merge.merge(Merge.merge(a, b), c)
            for (p in listOf(listOf(a, b, c), listOf(a, c, b), listOf(b, a, c), listOf(b, c, a), listOf(c, a, b), listOf(c, b, a))) {
                assertTrue(Merge.same(Merge.merge(Merge.merge(p[0], p[1]), p[2]), want), "order, seed $seed")
            }
        }
    }

    @Test
    fun editBeatsAnEarlierDeleteAndLosesToALaterOne() {
        val r = Merge.applyEdit(null, "logbook", "q", "create", mapOf("call" to "DL1ABC"), h(1), emptyMap(), "t")
        val deleted = Merge.applyEdit(r, "logbook", "q", "delete", emptyMap(), h(2), emptyMap(), "t")
        assertFalse(Merge.isLive(deleted))
        val back = Merge.applyEdit(deleted, "logbook", "q", "update", mapOf("rst_rcvd" to "57"), h(3), emptyMap(), "t")
        assertTrue(Merge.isLive(back))
        assertEquals(mapOf("call" to "DL1ABC", "rst_rcvd" to "57"), Merge.fields(back))
    }

    @Test
    fun aNullValueClearsAFieldWithItsOwnStamp() {
        val r = Merge.applyEdit(null, "logbook", "q", "create", mapOf("call" to "DL1ABC", "comment" to "x"), h(1), emptyMap(), "t")
        val cleared = Merge.applyEdit(r, "logbook", "q", "update", mapOf("comment" to null), h(2), emptyMap(), "t")
        assertFalse("comment" in Merge.fields(cleared))
        assertEquals(h(2), Merge.fieldHlc(cleared)["comment"])
        // An older copy that still has the comment does not bring it back.
        assertFalse("comment" in Merge.fields(Merge.merge(r, cleared)))
    }

    @Test
    fun unknownEnvelopeKeysSurviveAndTheNewerSideWins() {
        val old = Merge.applyEdit(null, "logbook", "q", "create", mapOf("call" to "A"), h(1), emptyMap(), "t") + mapOf("x_future" to 1L)
        val new = Merge.applyEdit(old, "logbook", "q", "update", mapOf("call" to "B"), h(2), emptyMap(), "t") + mapOf("x_future" to 2L)
        assertEquals(2L, Merge.merge(old, new)["x_future"])
        assertEquals(2L, Merge.merge(new, old)["x_future"])
    }

    @Test
    fun originTieBreaksOnThePythonJsonOfTheOrigin() {
        // Two copies of one creation that disagree on the origin: both
        // orders pick the same one, the one whose sorted JSON sorts first.
        val a = mapOf("collection" to "logbook", "id" to "q", "fields" to emptyMap<String, Any?>(), "fieldHlc" to emptyMap<String, String>(),
            "deletedHlc" to null, "createdAt" to "t", "origin" to mapOf("device" to "b", "app" to "z"))
        val b = a + mapOf("origin" to mapOf("app" to "z", "device" to "a"))
        assertEquals(mapOf("app" to "z", "device" to "a"), Merge.merge(a, b)["origin"])
        assertEquals(mapOf("app" to "z", "device" to "a"), Merge.merge(b, a)["origin"])
    }

    @Test
    fun profileMapsSplitIntoDottedPathsAndJoinBack() {
        val patch = mapOf("locator" to "IO75RL", "conditions" to mapOf("nearLondon" to true),
            "controllerMaps" to mapOf("qo100wb" to mapOf("ajazz-akp03e" to mapOf("pages" to emptyList<Any>()))))
        val split = Merge.splitProfile(patch)
        assertEquals(setOf("locator", "conditions.nearLondon", "controllerMaps.qo100wb.ajazz-akp03e"), split.keys)
        assertEquals(patch, Merge.joinProfile(split))
    }

    @Test
    fun hlcLocalAndReceiveRules() {
        val (s1, a) = Hlc.local(0, 0, "st_A", Hlc.CLOCK_FLOOR_MS)
        assertEquals(Hlc.fmt(Hlc.CLOCK_FLOOR_MS, 0, "st_A"), a)
        // Same millisecond: the count rises.
        val (_, b) = Hlc.local(s1.wall, s1.count, "st_A", Hlc.CLOCK_FLOOR_MS)
        assertTrue(b > a)
        // A remote stamp ahead of the local clock: the next local stamp is after it.
        val remote = Hlc.fmt(Hlc.CLOCK_FLOOR_MS + 7_200_000, 3, "st_B")
        val r = Hlc.receive(s1.wall, s1.count, remote, Hlc.CLOCK_FLOOR_MS)
        val (_, c) = Hlc.local(r.wall, r.count, "st_A", Hlc.CLOCK_FLOOR_MS)
        assertTrue(c > remote)
        // The count never wraps: the wall moves on instead.
        val (s4, _) = Hlc.local(5, 0xFFFF, "st_A", 5)
        assertEquals(Hlc.State(6, 0), s4)
        // The clock floor: a device in 1970 still stamps after it.
        assertEquals(Hlc.CLOCK_FLOOR_MS, Hlc.corrected(0, 0))
    }

    @Test
    fun hlcParsingRefusesWhatIsNotOne() {
        for (bad in listOf("", "000001a1202d7194-0003", "000001A1202D7194-0003-st_X", "x-y-z")) {
            try {
                Hlc.parse(bad)
                throw AssertionError("parsed '$bad'")
            } catch (e: Hlc.HlcError) {
            }
        }
        assertEquals(Hlc.Parts(0x1a1202d7194, 3, "st_K4M2Q7Z9XW3A"), Hlc.parse("000001a1202d7194-0003-st_K4M2Q7Z9XW3A"))
    }
}
