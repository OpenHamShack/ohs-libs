package com.openhamshack.ui

import com.openhamshack.client.Json
import com.openhamshack.contracts.ContractException
import com.openhamshack.contracts.Vectors
import com.openhamshack.contracts.list
import com.openhamshack.contracts.map
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.DynamicTest.dynamicTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestFactory

/**
 * spec/vectors/ui/documents.json (CLIENT-UI-PLAN.md §5), with the pass
 * criteria of python/shack_ui/tests/test_vectors.py: Document.validate
 * accepts the ok cases and refuses the others with the named code (and a
 * TransmitException exactly for the codes starting "transmit"). Python also
 * runs the bare schema; there is no schema engine here, so the client's
 * lenient parse is held to the equivalent: it refuses every case the schema
 * refuses, except the two a client reads as "a newer server's component".
 */
class UiVectorsTest {
    private val lenient = setOf("unknown_component", "unknown_native")

    @TestFactory
    fun documents() = Vectors.cases("ui/documents.json").map { case ->
        dynamicTest(case["id"] as String) {
            val want = case["expect"].map()["error"] as String?
            if (want == null) {
                Document.validate(case["document"])
                Document.parse(case["document"])
            } else {
                val err = assertThrows(ContractException::class.java) { Document.validate(case["document"]) }
                assertEquals(want, err.code)
                assertEquals(want.startsWith("transmit"), err is TransmitException)
                if (want in lenient) {
                    Document.parse(case["document"])
                } else {
                    assertThrows(ContractException::class.java) { Document.parse(case["document"]) }
                }
            }
        }
    }

    private fun doc(json: String) = Json.parse(json)

    @Test
    fun theUnknownComponentGetsItsFallbackAndTheKnownOnesStayThemselves() {
        val d = Document.parse(
            doc(
                """{"ui":"1.2","screen":"s","root":{"c":"column","children":[
                {"c":"marquee","text":"hi","fallback":{"c":"text","text":"old"}},
                {"c":"marquee"},
                {"c":"native","name":"hologram","v":3,"fallback":{"c":"text","text":"x"}}]}}""",
            ),
        )
        val kids = d.root.children
        assertTrue(d.root.known)
        assertEquals("text", kids[0].renderable()!!.c)
        assertNull(kids[1].renderable())
        assertEquals("text", kids[2].renderable()!!.c)
    }

    @Test
    fun unknownPropertiesAreCarriedAndBindingsAreTyped() {
        val d = Document.parse(
            doc(
                """{"ui":"1.0","screen":"s","title":"T","data":{"state":"ws:api/events","st":{"src":"get:api/status","poll":5}},
                "root":{"c":"value","sparkle":true,"value":{"bind":"status.vfo","format":"freq"}}}""",
            ),
        )
        assertEquals(Prop.Literal(true), d.root.props["sparkle"])
        assertEquals(Prop.Bound(Binding("status.vfo", "freq")), d.root.props["value"])
        assertEquals(Feed("get:api/status", 5.0), d.data["st"])
        assertTrue(d.data["state"]!!.isSocket)
    }

    @Test
    fun aMalformedBindingOrFeedIsRefused() {
        for (bad in listOf(
            """{"c":"value","value":{"bind":"a b"}}""",
            """{"c":"value","value":{"bind":"a.b","format":"hex"}}""",
        )) {
            assertThrows(ContractException::class.java) { Document.parse(doc("""{"ui":"1.0","screen":"s","root":$bad}""")) }
        }
        assertThrows(ContractException::class.java) {
            Document.parse(doc("""{"ui":"1.0","screen":"s","data":{"x":"https://evil/"},"root":{"c":"spacer"}}"""))
        }
    }

    @Test
    fun aTransmitActionIsRefusedEvenWhenTheClientIsLenient() {
        val e = assertThrows(TransmitException::class.java) {
            Document.parse(
                doc(
                    """{"ui":"1.0","screen":"s","root":{"c":"marquee","fallback":{"c":"button",
                    "action":{"do":"post","path":"api/tx","level":"transmit"}}}}""",
                ),
            )
        }
        assertEquals("transmit_action", e.code)
    }

    @Test
    fun bindingsResolveThroughMapsAndListsAndMissingIsNull() {
        val data = Json.parseObject("""{"state":{"dab":{"service":"BBC R4","list":[{"n":1},{"n":2}]}}}""")
        assertEquals("BBC R4", Bindings.resolve(Binding("state.dab.service", null), data))
        assertEquals(2L, Bindings.resolve(Binding("state.dab.list.1.n", null), data))
        assertNull(Bindings.resolve(Binding("state.dab.nope.x", null), data))
        assertNull(Bindings.resolve(Binding("stations.x", null), data))
    }

    @Test
    fun aPostBodyTakesTheControlsValueAndBindings() {
        val a = Action.parse(
            doc("""{"do":"post","path":"api/set","level":"control","body":{"volume":"${'$'}value","vfo":{"bind":"s.vfo"},"k":[1,"${'$'}value"]}}"""),
        ) as Action.Post
        assertEquals(Level.CONTROL, a.level)
        val body = Bindings.resolveBody(a.body, 0.5, Json.parseObject("""{"s":{"vfo":145500000}}"""))
        assertEquals(Json.parse("""{"volume":0.5,"vfo":145500000,"k":[1,0.5]}"""), body)
    }

    @Test
    fun actionsParseToTheirTypes() {
        val doc = Vectors.cases("ui/documents.json").first { it["id"] == "every-action" }["document"].map()
        val acts = Document.parse(doc).root.children.map { it.action }
        assertTrue(acts[0] is Action.Open && (acts[0] as Action.Open).params == mapOf("band" to "III"))
        assertTrue(acts[1] is Action.Back)
        assertEquals(Action.Local("layers.spots", false, null, null), acts[2])
        assertEquals("waterfall.zoomIn", (acts[3] as Action.Native).verb)
        assertTrue((acts[4] as Action.Confirm).action is Action.Post)
    }

    @Test
    fun formFactorFlagsDropANode() {
        val n = Document.parse(doc("""{"ui":"1.0","screen":"s","root":{"c":"text","tv":false}}""")).root
        assertTrue(!n.shownOn(tv = true))
        assertTrue(n.shownOn(tv = false))
    }

    @Test
    fun appDescriptorKeepsWhatTheLauncherNeeds() {
        val d = AppDescriptor.parse(
            doc(
                """{"name":"Radio","icon":{"png":"ui/v1/icon.png","mask":"ui/v1/mask.png"},
                "entries":[{"id":"dab","title":"DAB Radio","screen":"radio/dab","status":{"bind":"state.dab.service"},"future":1}],
                "screens":["radio/home"],"spectrum":1,"overlay":1,"minClient":"1.0","status":{"bind":"state.mode"},"extra":{}}""",
            ),
        )
        assertEquals("Radio", d.name)
        assertEquals("radio/dab", d.entries[0].screen)
        assertNotNull(d.entries[0].status)
        assertEquals(1L, d.spectrum)
        assertEquals(Binding("state.mode", null), d.status)
        assertNull(AppDescriptor.parse(doc("""{"name":"X"}""")).spectrum)
    }
}
