package com.openhamshack.overlay

import com.openhamshack.client.Json
import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Vectors
import com.openhamshack.overlay.OverlayStore.Result
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.DynamicTest.dynamicTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestFactory

/** spec/vectors/overlay/messages.json: a message parses exactly when the
 *  case says valid (the Python test checks overlay.schema.json the same
 *  way), then the store and label rules that no vector covers. */
class OverlayVectorsTest {
    @TestFactory
    fun messages() = Vectors.cases("overlay/messages.json").map { case ->
        dynamicTest(case["id"] as String) {
            val ok = try {
                OverlayMessage.parse(case["message"])
                true
            } catch (e: ContractException) {
                // A transmit action keeps its own name, as in ui; the rest are schema.
                assertTrue(e.code == "schema" || e.code == "transmit_action", e.code)
                false
            }
            assertEquals(case["valid"], ok)
        }
    }

    private fun msg(rev: Int, op: String, items: String) = OverlayMessage.parse(Json.parse("""{"type":"overlay","rev":$rev,"op":"$op","items":$items}"""))

    private fun item(id: String, extra: String = "") =
        """{"id":"$id","kind":"spot","layer":"spots","loHz":1,"hiHz":2,"label":"L-$id"$extra}"""

    @Test
    fun theStoreFollowsRevsAndAsksForASnapshotOnAGap() {
        val s = OverlayStore()
        assertEquals(Result.RESYNC, s.apply(msg(1, "upsert", "[${item("a")}]")))
        assertEquals(Result.APPLIED, s.apply(msg(41, "snapshot", "[${item("a")},${item("b")}]")))
        assertEquals(Result.APPLIED, s.apply(msg(42, "upsert", "[${item("c")},${item("a")}]")))
        assertEquals(listOf("a", "b", "c"), s.all.map { it.id })
        assertEquals(Result.STALE, s.apply(msg(42, "remove", """[{"id":"a"}]""")))
        assertEquals(Result.RESYNC, s.apply(msg(44, "remove", """[{"id":"a"}]""")))
        assertEquals(42L, s.rev)
        assertEquals(Result.APPLIED, s.apply(msg(43, "remove", """[{"id":"a"},{"id":"nope"}]""")))
        assertEquals(listOf("b", "c"), s.all.map { it.id })
        // A snapshot at a lower rev (a new socket) replaces everything.
        assertEquals(Result.APPLIED, s.apply(msg(1, "snapshot", "[]")))
        assertEquals(emptyList<OverlayItem>(), s.all)
        assertEquals(1L, s.rev)
    }

    @Test
    fun anUpsertReplacesTheItemById() {
        val s = OverlayStore()
        s.apply(msg(1, "snapshot", "[${item("a")}]"))
        s.apply(msg(2, "upsert", "[${item("a", ",\"detail\":\"d\"")}]"))
        assertEquals("d", s["a"]!!.detail)
        assertEquals(1, s.all.size)
    }

    @Test
    fun labelsShowOnceZoomedInEnoughAndActiveHighPrioBreaksTheRule() {
        val s = OverlayStore()
        s.apply(
            msg(
                1, "snapshot",
                "[${item("seg", ",\"readAtHzPerPx\":null")},${item("near", ",\"readAtHzPerPx\":400,\"prio\":10")}," +
                    "${item("hot", ",\"readAtHzPerPx\":50,\"prio\":90,\"active\":true")}," +
                    "${item("cool", ",\"readAtHzPerPx\":50,\"prio\":90")}]",
            ),
        )
        fun ids(hz: Double) = s.labelCandidates(hz, setOf("spots")).map { it.id }
        assertEquals(listOf("hot", "seg"), ids(1000.0))
        assertEquals(listOf("hot", "cool", "seg", "near").sorted(), ids(40.0).sorted())
        assertEquals(listOf("hot", "seg", "near").sorted(), ids(400.0).sorted())
        assertEquals(emptyList<String>(), s.labelCandidates(1.0, emptySet()).filter { it.layer == "spots" }.map { it.id })
    }

    @Test
    fun bandAndDialLayersAreAlwaysOnAndExpiredItemsAreDropped() {
        val s = OverlayStore()
        s.apply(
            msg(
                1, "snapshot",
                """[{"id":"b","kind":"segment","layer":"band","loHz":0,"hiHz":1,"label":"CW","readAtHzPerPx":null},
                   {"id":"x","kind":"spot","layer":"spots","loHz":0,"hiHz":1,"label":"x","expires":1000}]""",
            ),
        )
        assertEquals(listOf("b"), s.labelCandidates(1.0, emptySet()).map { it.id })
        s.prune(999)
        assertEquals(2, s.all.size)
        s.prune(1000)
        assertEquals(listOf("b"), s.all.map { it.id })
        assertFalse(s["b"]!!.expired(Long.MAX_VALUE))
    }

    @Test
    fun anItemsActionsAreTypedAndNeverTransmit() {
        val m = msg(
            1, "snapshot",
            "[" + item("a", ""","actions":[{"id":"tune","label":"Tune","do":"post","path":"api/set","body":{"vfo":1},"level":"control"}]""") + "]",
        )
        val a = m.items[0].actions[0]
        assertTrue(a is com.openhamshack.ui.Action.Post)
        assertEquals("tune", a.id)
    }
}
