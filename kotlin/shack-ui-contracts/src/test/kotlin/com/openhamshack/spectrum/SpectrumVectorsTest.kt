package com.openhamshack.spectrum

import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Vectors
import com.openhamshack.contracts.dbl
import com.openhamshack.contracts.list
import com.openhamshack.contracts.map
import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.DynamicTest.dynamicTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestFactory

/**
 * spec/vectors/spectrum/ (CLIENT-UI-PLAN.md §2), with the pass criteria of
 * python/shack_spectrum/tests/test_vectors.py: rows decode to the stated
 * header and values or refuse with the stated code, and encoding what was
 * decoded gives the file back (minus the flags this version does not
 * know); decimation, levels and gaps agree to the stated tolerances; every
 * control message is accepted exactly when the case says valid.
 */
class SpectrumVectorsTest {
    @TestFactory
    fun rows() = Vectors.cases("spectrum/rows.json").map { case ->
        dynamicTest(case["id"] as String) {
            val data = Vectors.file("spectrum/" + case["file"]).readBytes()
            val e = case["expect"].map()
            if (e.containsKey("error")) {
                val err = assertThrows(RowException::class.java) { Rows.decode(data) }
                assertEquals(e["error"], err.code)
                return@dynamicTest
            }
            val r = Rows.decode(data)
            assertEquals((e["stream"] as Number).toInt(), r.stream)
            assertEquals((e["seq"] as Number).toLong(), r.seq)
            assertEquals((e["tMs"] as Number).toLong(), r.tMs)
            assertEquals(e["loHz"].dbl(), r.loHz)
            assertEquals(e["colHz"].dbl(), r.colHz)
            assertEquals((e["depth"] as Number).toInt(), r.depth)
            assertEquals(e["levelsChanged"], r.levelsChanged)
            assertEquals(e["streamChanged"], r.streamChanged)
            assertEquals(e["gap"], r.gap)
            assertEquals(e["values"].list().map { (it as Number).toInt() }, r.values.toList())
            val want = data.copyOf()
            want[3] = (want[3].toInt() and 0x0F).toByte()
            assertArrayEquals(want, Rows.encode(r))
        }
    }

    @TestFactory
    fun decimation(): List<org.junit.jupiter.api.DynamicTest> {
        val head = Vectors.head("spectrum/decimation.json")
        val src = head["source"].map()
        val db = src["db"].list().map { it.dbl() }.toDoubleArray()
        return head["cases"].list().map { c ->
            val case = c.map()
            dynamicTest(case["id"] as String) {
                val sub = case["subscribe"].map()
                val e = case["expect"].map()
                val got = Reduce.reduce(
                    db, src["loHz"].dbl(), src["binHz"].dbl(), (sub["cols"] as Number).toInt(),
                    sub["loHz"]?.dbl(), sub["hiHz"]?.dbl(), sub["reduce"] as String,
                )
                assertEquals(e["loHz"].dbl(), got.loHz, 1e-6)
                assertEquals(e["colHz"].dbl(), got.colHz, Math.abs(e["colHz"].dbl()) * 1e-12)
                val want = e["db"].list().map { it.dbl() }
                assertEquals(want.size, got.db.size)
                want.forEachIndexed { i, w -> assertEquals(w, got.db[i], 1e-9, "column $i") }
                val lv = case["levels"].map()
                val depth = (sub["depth"] as Number).toInt()
                assertEquals(
                    e["values"].list().map { (it as Number).toInt() },
                    Levels.quantise(got.db, lv["dbFloor"].dbl(), lv["dbStep"].dbl(), depth).toList(),
                )
            }
        }
    }

    @TestFactory
    fun levels() = Vectors.cases("spectrum/levels.json").map { case ->
        dynamicTest(case["id"] as String) {
            val w = LevelsWindow()
            case["rows"].list().forEachIndexed { i, r ->
                val row = r.map()
                val e = row["expect"].map()
                val changed = w.update(row["db"].list().map { it.dbl() }.toDoubleArray(), (row["tMs"] as Number).toLong())
                assertEquals(e["changed"], changed, "row $i")
                assertEquals(e["dbFloor"].dbl(), w.floor!!, 1e-9, "row $i floor")
                assertEquals(e["dbTop"].dbl(), w.top!!, 1e-9, "row $i top")
                assertEquals(e["dbStep8"].dbl(), w.step(8), 1e-9, "row $i step")
                assertEquals(e["noiseDb"].dbl(), w.noiseDb!!, 1e-9, "row $i noise")
                assertEquals(e["peakDb"].dbl(), w.peakDb!!, 1e-9, "row $i peak")
            }
        }
    }

    @TestFactory
    fun gaps() = Vectors.cases("spectrum/gaps.json").map { case ->
        dynamicTest(case["id"] as String) {
            val sent = ArrayList<Map<String, Any?>>()
            val s = LatestWins<String> { client, payload ->
                val r = Rows.decode(payload)
                sent.add(mapOf("client" to client, "stream" to r.stream.toLong(), "seq" to r.seq, "tMs" to r.tMs, "gap" to r.gap))
            }
            for (op in case["ops"].list()) {
                val o = op.map()
                if (o.containsKey("offer")) {
                    val x = o["offer"].map()
                    s.offer(x["client"] as String, Row((x["stream"] as Number).toInt(), 0, (x["tMs"] as Number).toLong(), 0.0, 1.0, intArrayOf(0)))
                } else {
                    s.drained(o["drained"] as String)
                }
            }
            val want = case["expect"].map()["sent"].list().map { m ->
                m.map().mapValues { (_, v) -> if (v is Number) v.toLong() else v }
            }
            assertEquals(want, sent)
        }
    }

    @TestFactory
    fun controlMessages() = Vectors.cases("spectrum/messages.json").map { case ->
        dynamicTest(case["id"] as String) {
            val ok = try {
                SpectrumMessage.parse(case["message"])
                true
            } catch (e: ContractException) {
                assertEquals("schema", e.code)
                false
            }
            assertEquals(case["valid"], ok)
        }
    }

    @Test
    fun streamDescriptionKeepsWhatTheClientNeeds() {
        val m = Vectors.cases("spectrum/messages.json").first { it["id"] == "stream-plan" }["message"]
        val d = SpectrumMessage.parse(m) as SpectrumMessage.StreamDescription
        assertEquals("main", d.stream)
        assertEquals(7L, d.rev)
        assertEquals(1171.875, d.binHz)
        assertEquals(1023L to 1025L, d.dcBins)
        assertEquals(listOf("ais-1"), d.pinnedBy)
        assertEquals("Wide", d.label)
        assertTrue(!d.mayPrintDbm)
    }

    @Test
    fun levelsApplyAsDbFloorPlusValueTimesStep() {
        val lv = Levels(-142.0, 0.375)
        assertEquals(-142.0, lv.dequantise(0))
        assertEquals(-142.0 + 255 * 0.375, lv.dequantise(255))
        assertEquals(listOf(0, 255), Levels.quantise(doubleArrayOf(-200.0, 0.0), -142.0, 0.375).toList())
    }

    @Test
    fun subscribeBuildsTheFrameTheSchemaAccepts() {
        val frame = SpectrumMessage.Subscribe("main", 1280, fps = 15.0, loHz = 144.1e6, hiHz = 144.5e6).toMap()
        assertEquals(SpectrumMessage.Subscribe("main", 1280, 15.0, 144.1e6, 144.5e6), SpectrumMessage.parse(frame))
    }

    @Test
    fun encodeRefusesAValueThatDoesNotFitItsDepth() {
        assertThrows(RowException::class.java) { Rows.encode(Row(0, 0, 0, 0.0, 1.0, intArrayOf(256))) }
    }

    @Test
    fun aSlowClientNeverHasMoreThanOneRowWaitingPerStream() {
        val sent = ArrayList<Row>()
        val s = LatestWins<String> { _, b -> sent.add(Rows.decode(b)) }
        for (t in 0 until 1000) s.offer("tv", Row(0, 0, t.toLong(), 0.0, 1.0, intArrayOf(1)))
        s.drained("tv")
        assertEquals(listOf(0L to false, 999L to true), sent.map { it.tMs to it.gap })
        assertEquals(998, s.dropped)
    }
}
