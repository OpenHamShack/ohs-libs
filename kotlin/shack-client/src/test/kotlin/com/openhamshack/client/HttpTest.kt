package com.openhamshack.client

import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.io.File

/** The client against the reference controller over real HTTP
 *  (HttpURLConnection to the JDK's HttpServer on loopback): pairing by
 *  password and TOTP, by TV code, a dropped answer, well-known resolution
 *  and the background thread. A port of tests/test_http.py. */
class HttpTest {
    @TempDir lateinit var dir: File
    private lateinit var ctl: ReferenceController
    private lateinit var url: String
    private val T = "2026-10-09T10:12:30Z"

    @BeforeEach
    fun up() {
        ctl = ReferenceController(File(dir, "services.db"),
            users = mapOf("brian" to mapOf("password" to "pw", "transmit" to true, "totp" to "123456")))
        url = ctl.serve()
    }

    @AfterEach
    fun down() = ctl.close()

    private fun station(name: String = "a", background: Boolean = false) =
        ShackStation.open(File(dir, "$name/station.db"), "remote", "1.0.0", JdbcSqlite.OPENER, background = background)

    @Test
    fun passwordPairingAsksForTotpThenSyncs() = station().use { st ->
        st.logbook.add(mapOf("call" to "DL1ABC", "start" to T))
        assertEquals("totp_required", assertThrows(PairingError::class.java) { st.pair(url, "brian", "pw", deviceName = "Tab") }.code)
        assertEquals("bad_credentials", assertThrows(PairingError::class.java) { st.pair(url, "brian", "wrong", "123456") }.code)
        val p = st.pair(url, "brian", "pw", "123456", deviceName = "Tab") as PairResult.Paired
        assertEquals(ctl.controllerId, p.pairing.controllerId)
        assertEquals("GM4BRI", p.pairing.name)
        assertEquals(1, ctl.records().size)
        assertEquals("idle", st.sync.status().state)
        assertTrue(st.pairingInfo()!!["token"].asStr().startsWith("shackdev_"))
        assertEquals("Tab", ctl.devices.values.single()["name"])
    }

    @Test
    fun aTvIsRefusedAPassword() = station().use { st ->
        assertEquals("use_tv_pairing", assertThrows(PairingError::class.java) {
            st.pair(url, "brian", "pw", "123456", deviceKind = "tv")
        }.code)
    }

    @Test
    fun pairingAgainRevokesThePreviousToken() = station().use { st ->
        st.pair(url, "brian", "pw", "123456")
        val first = st.pairingInfo()!!["token"].asStr()
        st.pair(url, "brian", "pw", "123456")
        val (status, ans) = HttpTransport(url, first).get("api/shack/v1/")
        assertEquals(401 to "token_revoked", status to ans["code"])
        assertEquals("idle", st.sync.runOnce().state)
    }

    @Test
    fun aDroppedAnswerOverHttpIsRetriedHarmlessly() = station().use { st ->
        st.pair(url, "brian", "pw", "123456")
        val q = st.logbook.add(mapOf("call" to "DL1ABC", "start" to T))
        ctl.dropNextPushAnswer = true
        // The JDK's HttpURLConnection resends a buffered POST once by itself
        // when the connection drops before the answer (Android's OkHttp-based
        // one may too), so the first round can already end idle; Python's
        // urllib reports it offline. Either way the push reached the
        // controller twice, and the merge makes the second one harmless.
        val first = st.sync.runOnce().state
        assertTrue(first == "offline" || first == "idle", first)
        assertEquals("idle", st.sync.runOnce().state)
        assertTrue(ctl.requests.count { it.second.startsWith("/api/shack/v1/sync/push") } >= 2)
        assertEquals(1, ctl.records().size)
        assertEquals(1, ctl.sync.history("logbook", q.uuid).size)
    }

    @Test
    fun tvPairingByCodeApprovedFromAPhone() {
        station("phone").use { phone ->
            phone.pair(url, "brian", "pw", "123456", deviceKind = "phone")
            station("tv").use { tv ->
                var shown: TvPairState.Code? = null
                val (state, result) = tv.pairTv(url, "Living room TV", onCode = { code ->
                    shown = code
                    // The person reads the code off the TV and approves it on the phone.
                    val (s, _) = phone.transport().post("api/shack/v1/pair/approve", mapOf("code" to code.code, "maxLevel" to "control"))
                    assertEquals(200, s)
                }, sleep = {})
                assertEquals(6, shown!!.code.length)
                val tok = (state as TvPairState.Approved).token
                assertEquals("control", tok.maxLevel)
                assertEquals(ctl.controllerId, tok.controllerId)
                assertTrue(result is PairResult.Paired)
                assertEquals("control", tv.pairingInfo()!!["maxLevel"])
                assertEquals("idle", tv.sync.status().state)
                // The token is never printed.
                assertTrue("shackdev_" !in tok.toString())
            }
        }
    }

    @Test
    fun tvPairingDeniedAndExpired() = station("tv").use { tv ->
        station("phone").use { phone ->
            phone.pair(url, "brian", "pw", "123456")
            val (denied, _) = tv.pairTv(url, "TV", onCode = { c ->
                phone.transport().post("api/shack/v1/pair/approve", mapOf("code" to c.code, "deny" to true))
            }, sleep = {})
            assertEquals(TvPairState.Denied, denied)
        }
        val t = HttpTransport(url)
        val p = Pairing.tvStart(t, "TV", "tv-1", "remote")
        assertEquals("pending" to null, Pairing.tvPoll(t, p))
        assertEquals("expired", Pairing.tvPoll(t, p.copy(pairSecret = "wrong")).first)
    }

    @Test
    fun wellKnownResolvesAgainstTheBase() {
        val wk = Discovery.wellKnown(url)
        assertEquals(ctl.controllerId, wk["controllerId"])
        assertEquals(url + "api/shack/token", wk["auth"].asMap()["token"])
        assertEquals(url + "api/shack/v1/", wk["services"].asMap()["base"])
    }

    @Test
    fun wellKnownOfNothingIsOffline() {
        assertThrows(Offline::class.java) { Discovery.wellKnown("http://127.0.0.1:9/") }
    }

    @Test
    fun backgroundThreadSyncsAfterALocalWriteAndUnpairStopsIt() {
        val st = station(background = true)
        try {
            st.sync.debounceMs = 50
            val seen = java.util.concurrent.CopyOnWriteArrayList<SyncStatus>()
            st.sync.addListener { seen.add(it) }
            st.pair(url, "brian", "pw", "123456")
            st.logbook.add(mapOf("call" to "DL1ABC", "start" to T))
            val deadline = System.currentTimeMillis() + 5_000
            while (ctl.records().isEmpty() && System.currentTimeMillis() < deadline) Thread.sleep(50)
            assertEquals(1, ctl.records().size)
            assertTrue(seen.any { it.state == "idle" })
            // Unpair stops the thread (waiting for a round in flight to
            // finish), and from then on nothing reaches the controller.
            st.unpair()
            val before = ctl.requests.size
            st.logbook.add(mapOf("call" to "F4XYZ", "start" to T))
            Thread.sleep(300)
            assertEquals(before, ctl.requests.size)
            assertEquals(2, st.logbook.count())
            assertEquals("unpaired", st.sync.status().state)
        } finally {
            st.close()
        }
    }

    @Test
    fun aControllerSideChangeReachesTheApp() = station().use { st ->
        val seen = java.util.concurrent.CopyOnWriteArrayList<Pair<String?, List<String>>>()
        st.onChange { c, ids -> seen.add(c to ids) }
        st.pair(url, "brian", "pw", "123456")
        val q = st.logbook.add(mapOf("call" to "DL1ABC", "start" to T))
        st.sync.runOnce()
        ctl.sync.edit("logbook", q.uuid, "update", mapOf("qsl_rcvd" to "Y"), mapOf("app" to "controller-ui"))
        st.sync.runOnce()
        assertEquals("Y", st.logbook.get(q.uuid)!!.fields["qsl_rcvd"])
        assertTrue(("logbook" to listOf(q.uuid)) in seen)
        assertTrue(ctl.sync.history("logbook", q.uuid).last()["hlc"].asStr().endsWith(ctl.sync.node))
    }
}
