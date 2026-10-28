package com.openhamshack.presence

import com.openhamshack.client.Json
import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Vectors
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.DynamicTest.dynamicTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestFactory

/** spec/vectors/presence/messages.json: each message is checked against
 *  the $defs entry its `def` names, as the Python test does. */
class PresenceVectorsTest {
    @TestFactory
    fun messages() = Vectors.cases("presence/messages.json").map { case ->
        dynamicTest(case["id"] as String) {
            val m = case["message"]
            val ok = try {
                when (case["def"]) {
                    "renew" -> Renew.parse(m)
                    "lease" -> Lease.parse(m)
                    "list" -> PresenceList.parse(m)
                    "arrived", "moved", "gone" -> PresenceEvents.parse(case["def"] as String, m)
                    else -> error("unknown def ${case["def"]}")
                }
                true
            } catch (e: ContractException) {
                assertEquals("schema", e.code)
                false
            }
            assertEquals(case["valid"], ok)
        }
    }

    @Test
    fun aRenewalRoundTripsAndOmitsAnAbsentScreen() {
        val r = Renew.parse(Json.parse("""{"client":"remote-7f3a","app":"shack-radio","screen":"radio/dab","holds":["dab","audio"]}"""))
        assertEquals(r, Renew.parse(r.toMap()))
        val bare = Renew("c", "a", null, emptyList())
        assertEquals(false, bare.toMap().containsKey("screen"))
        assertEquals(bare, Renew.parse(bare.toMap()))
    }

    @Test
    fun theLeaseTellsTheClientHowOftenToRenew() {
        assertEquals(Lease(30, 10), Lease.parse(Json.parse("""{"leaseS":30,"renewEveryS":10}""")))
    }
}
