package com.openhamshack.client

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.io.File

/** JSON, the logbook, ADIF and discovery: everything but the sync vectors
 *  and real HTTP. */
class ClientTest {
    private val T = "2026-10-09T10:12:30Z"

    private fun station(dir: File, name: String = "a") =
        ShackStation.open(File(dir, "$name/station.db"), "shack-test", "1.0.0", JdbcSqlite.OPENER, background = false)

    // ------------------------------------------------------------ JSON
    @Test
    fun jsonDumpIsPythonsSortedKeysForm() {
        // json.dumps({"b": 1, "a": [True, None, 1.5, "é\n"]}, sort_keys=True)
        assertEquals("{\"a\": [true, null, 1.5, \"\\u00e9\\n\"], \"b\": 1}",
            Json.dump(mapOf("b" to 1L, "a" to listOf(true, null, 1.5, "é\n"))))
        assertEquals("[1e+16, 1e-05, 10.0, -0.5]", Json.dump(listOf(1e16, 1e-5, 10.0, -0.5)))
        val text = "{\"freq_hz\": 10489750123, \"x\": [1, 2.5, \"a\\\"b\"], \"n\": null}"
        val v = Json.parseObject(text)
        assertEquals(10489750123L, v["freq_hz"])
        assertEquals(listOf(1L, 2.5, "a\"b"), v["x"])
        assertEquals(v, Json.parseObject(Json.dump(v)))
    }

    // ------------------------------------------------------------ logbook
    @Test
    fun addIsIdempotentByUuidAndReportsLikelyDuplicates(@TempDir dir: File) = station(dir).use { st ->
        val a = st.logbook.add(mapOf("call" to " dl1abc ", "start" to T, "freq_hz" to 14_074_000L, "mode" to "FT8"))
        assertEquals("DL1ABC", a.fields["call"])
        assertEquals(a, st.logbook.add(mapOf("call" to "DL1ABC", "start" to T), a.uuid).copy(duplicateOf = emptyList()))
        val b = st.logbook.add(mapOf("call" to "DL1ABC", "start" to "2026-10-09T10:14:00Z", "freq_hz" to 14_075_000L, "mode" to "ft8"))
        assertEquals(listOf(a.uuid), b.duplicateOf)
        assertEquals(2, st.logbook.workedBefore("dl1abc", band = "20m", mode = "FT8").size)
        assertEquals(2, st.logbook.count())
    }

    @Test
    fun aContactNeedsACallAndAStartAndFieldNamesAreAdif(@TempDir dir: File) = station(dir).use { st ->
        assertEquals("call_required", assertThrows(ShackError::class.java) { st.logbook.add(mapOf("start" to T)) }.code)
        assertEquals("start_required", assertThrows(ShackError::class.java) { st.logbook.add(mapOf("call" to "X")) }.code)
        assertEquals("bad_start", assertThrows(ShackError::class.java) { st.logbook.add(mapOf("call" to "X", "start" to "yesterday")) }.code)
        assertEquals("bad_field_name", assertThrows(ShackError::class.java) { st.logbook.add(mapOf("call" to "X", "start" to T, "RST" to "5")) }.code)
    }

    @Test
    fun deleteHidesFromListsButKeepsHistory(@TempDir dir: File) = station(dir).use { st ->
        val q = st.logbook.add(mapOf("call" to "DL1ABC", "start" to T))
        st.logbook.update(q.uuid, mapOf("rst_rcvd" to "57"))
        st.logbook.delete(q.uuid)
        assertTrue(st.logbook.get(q.uuid)!!.deleted)
        assertTrue(st.logbook.query().isEmpty())
        assertEquals(1, st.logbook.query(includeDeleted = true).size)
        assertEquals(listOf("create", "update", "delete"), st.logbook.history(q.uuid).map { it["kind"] })
    }

    @Test
    fun adifRoundTripKeepsIdentityAndReportsDuplicates(@TempDir dir: File) {
        station(dir, "a").use { a ->
            a.logbook.add(mapOf("call" to "DL1ABC", "start" to T, "freq_hz" to 2_400_250_000L, "mode" to "SSB", "rst_rcvd" to "57"))
            val text = a.logbook.exportAdif()
            assertTrue("<FREQ:11>2400.250000 " in text && "<QSO_DATE:8>20261009 " in text && "<TIME_ON:6>101230 " in text, text)
            station(dir, "b").use { b ->
                val r = b.logbook.importAdif(text)
                assertEquals(1, r.added)
                val got = b.logbook.query().single()
                assertEquals(a.logbook.query().single().uuid, got.uuid)
                assertEquals(2_400_250_000L, got.fields["freq_hz"])
                assertEquals("13cm", Adif.bandFor(got.fields["freq_hz"]))
                assertEquals(1, b.logbook.importAdif(text).duplicates.size)
            }
        }
    }

    @Test
    fun profileUpdatesNormaliseAndMergePerPath(@TempDir dir: File) = station(dir).use { st ->
        st.profile.update(mapOf("callsign" to "gm4bri ", "locator" to "io75rl"))
        st.profile.update(mapOf("conditions" to mapOf("nearLondon" to false)))
        val p = st.profile.get()
        assertEquals("GM4BRI", p["callsign"])
        assertEquals("IO75RL", p["locator"])
        assertEquals(mapOf("nearLondon" to false), p["conditions"])
        assertEquals("bad_locator", assertThrows(ShackError::class.java) { st.profile.update(mapOf("locator" to "ZZ99")) }.code)
    }

    @Test
    fun uuid7IsAVersion7Uuid() {
        val u = java.util.UUID.fromString(Store.uuid7(Hlc.CLOCK_FLOOR_MS))
        assertEquals(7, u.version())
        assertEquals(2, u.variant())
        assertEquals(Hlc.CLOCK_FLOOR_MS, u.mostSignificantBits ushr 16)
    }

    @Test
    fun anUnpairedStationMakesNoRequestAndSaysSo(@TempDir dir: File) = station(dir).use { st ->
        assertEquals("unpaired", st.sync.runOnce().state)
        assertNull(st.pairingInfo())
    }

    // ------------------------------------------------------------ discovery
    private val txt = mapOf<String, Any?>("platform" to "1".toByteArray(), "contract" to "1".toByteArray(), "id" to "7f3c2a9e".toByteArray(),
        "name" to "GM4BRI".toByteArray(), "version" to "2.0.0-beta.1".toByteArray(), "kinds" to "qo100wb,icom".toByteArray(),
        "path" to "/.well-known/openhamshack".toByteArray())

    @Test
    fun aControllerAdvert() {
        val c = Discovery.parseAdvert("_openhamshack._tcp.local.", "GM4BRI._openhamshack._tcp.local.", "198.51.100.208", 80, txt)!!
        assertEquals(listOf("GM4BRI", "7f3c2a9e", "http://198.51.100.208:80/", listOf("icom", "qo100wb"), false),
            listOf(c.name, c.id, c.url, c.kinds, c.classic))
        val https = Discovery.parseAdvert("._openhamshack._tcp", "GM4BRI", "198.51.100.208", 80, txt + ("https" to "443"))!!
        assertEquals("https://198.51.100.208:443/", https.url)
    }

    @Test
    fun anOpenhamshackRecordWithoutPlatformIsNotOurs() {
        assertNull(Discovery.parseAdvert(Discovery.SERVICE, "x", "10.0.0.1", 80, mapOf("name" to "x")))
        assertNull(Discovery.parseAdvert("_http._tcp", "x", "10.0.0.1", 80, txt))
    }

    @Test
    fun aClassicBoxIsOneQo100wbApp() {
        val c = Discovery.parseAdvert(Discovery.LEGACY + ".local.", "OpenHamShack._openhamshack._tcp.local.", "198.51.100.50", 8080,
            mapOf("name" to "OpenHamShack workshop", "version" to "0.9.14", "port" to "8080"))!!
        assertEquals(listOf(true, listOf("qo100wb"), "http://198.51.100.50:8080/", "OpenHamShack workshop"), listOf(c.classic, c.kinds, c.url, c.name))
    }

    @Test
    fun theOpenhamshackRecordWinsOverTheDraftLegacyName() {
        val draft = Discovery.parseAdvert(Discovery.LEGACY, "GM4BRI", "198.51.100.208", 80, txt)!!
        val real = Discovery.parseAdvert(Discovery.SERVICE, "GM4BRI", "198.51.100.208", 80, txt)!!
        assertTrue(draft.service == Discovery.LEGACY && !draft.classic)
        assertEquals(listOf(real), Discovery.mergeAdverts(listOf(draft, real)))
        assertEquals(listOf(real), Discovery.mergeAdverts(listOf(real, draft)))
        val classic = Discovery.parseAdvert(Discovery.LEGACY, "Box", "198.51.100.208", 80, mapOf("name" to "Box"))!!
        assertEquals(2, Discovery.mergeAdverts(listOf(draft, real, classic)).size)
        assertFalse(Discovery.mergeAdverts(listOf(draft, real, classic)).contains(draft))
    }

    @Test
    fun urlsAreRelativeToTheBase() {
        assertEquals("http://h/shack/api/shack/token", Urls.join("http://h/shack", "api/shack/token"))
        assertEquals("http://h/shack/api/x?since=0&collections=logbook%2Cprofile",
            Urls.join("http://h/shack/", "/api/x", mapOf("since" to 0, "epoch" to null, "collections" to "logbook,profile")))
    }
}
