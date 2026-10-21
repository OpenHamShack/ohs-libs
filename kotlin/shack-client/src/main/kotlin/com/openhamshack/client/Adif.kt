package com.openhamshack.client

import java.time.LocalDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * ADIF 3.1.4 import and export for the logbook (SERVICES.md §3.3), a port
 * of python/shack_client adif.py.
 *
 * Stored QSO fields are ADIF names in lower case, so most of the mapping is
 * "upper-case the name". The exceptions are the timestamps (`start` and
 * `end` replace QSO_DATE+TIME_ON and QSO_DATE_OFF+TIME_OFF), the two
 * frequencies (integer Hz on the wire, MHz with six decimals in ADIF) and
 * the record's identity (APP_SHACK_UUID, APP_SHACK_ORIGIN), written on every
 * record so a file exported here and imported elsewhere is the same
 * contacts, not copies.
 */
object Adif {
    /** ADIF 3.1.4's Band enumeration in Hz: a QSO's `band` when it has a
     *  frequency and no band. The same table as data/bandplan/vocab.json
     *  "bands" and adif.py; carried here so the client needs no band-plan
     *  dependency. */
    val BANDS: List<Triple<String, Long, Long>> = listOf(
        Triple("2190m", 135_700, 137_800), Triple("630m", 472_000, 479_000), Triple("560m", 501_000, 504_000),
        Triple("160m", 1_800_000, 2_000_000), Triple("80m", 3_500_000, 4_000_000), Triple("60m", 5_060_000, 5_450_000),
        Triple("40m", 7_000_000, 7_300_000), Triple("30m", 10_100_000, 10_150_000), Triple("20m", 14_000_000, 14_350_000),
        Triple("17m", 18_068_000, 18_168_000), Triple("15m", 21_000_000, 21_450_000), Triple("12m", 24_890_000, 24_990_000),
        Triple("10m", 28_000_000, 29_700_000), Triple("8m", 40_000_000, 45_000_000), Triple("6m", 50_000_000, 54_000_000),
        Triple("5m", 54_000_001, 69_900_000), Triple("4m", 70_000_000, 71_000_000), Triple("2m", 144_000_000, 148_000_000),
        Triple("1.25m", 222_000_000, 225_000_000), Triple("70cm", 420_000_000, 450_000_000),
        Triple("33cm", 902_000_000, 928_000_000), Triple("23cm", 1_240_000_000, 1_300_000_000),
        Triple("13cm", 2_300_000_000, 2_450_000_000), Triple("9cm", 3_300_000_000, 3_500_000_000),
        Triple("6cm", 5_650_000_000, 5_925_000_000), Triple("3cm", 10_000_000_000, 10_500_000_000),
        Triple("1.25cm", 24_000_000_000, 24_250_000_000), Triple("6mm", 47_000_000_000, 47_200_000_000),
        Triple("4mm", 75_500_000_000, 81_000_000_000), Triple("2.5mm", 119_980_000_000, 123_000_000_000),
        Triple("2mm", 134_000_000_000, 149_000_000_000), Triple("1mm", 241_000_000_000, 250_000_000_000),
    )

    fun bandFor(hz: Any?): String {
        val f = when (hz) {
            is Number -> hz.toLong()
            is String -> hz.trim().toLongOrNull() ?: return ""
            else -> return ""
        }
        return BANDS.firstOrNull { f >= it.second && f <= it.third }?.first ?: ""
    }

    private val TAG = Regex("<([A-Za-z0-9_]+)(?::(\\d+)(?::[A-Za-z])?)?>")

    /** (line number, {FIELD: value}) for every record of an .adi file.
     *  Field lengths are honoured, so a value may contain '<' or a newline. */
    fun parse(text: String): List<Pair<Int, Map<String, String>>> {
        val eoh = Regex("<eoh>", RegexOption.IGNORE_CASE).find(text)
        var i = eoh?.range?.last?.plus(1) ?: if (text.trimStart().startsWith("<")) 0 else text.length
        val out = ArrayList<Pair<Int, Map<String, String>>>()
        var cur = LinkedHashMap<String, String>()
        var curLine: Int? = null
        while (true) {
            val t = TAG.find(text, i) ?: break
            val name = t.groupValues[1].uppercase(java.util.Locale.ROOT)
            if (name == "EOR") {
                if (cur.isNotEmpty()) out.add((curLine ?: 1) to cur)
                cur = LinkedHashMap()
                curLine = null
                i = t.range.last + 1
                continue
            }
            if (t.groups[2] == null) {
                i = t.range.last + 1
                continue
            }
            val n = t.groupValues[2].toInt()
            val from = t.range.last + 1
            val value = text.substring(from, minOf(text.length, from + n))
            if (curLine == null) curLine = text.substring(0, t.range.first).count { it == '\n' } + 1
            cur[name] = value
            i = from + n
            if (i >= text.length) break
        }
        return out
    }

    private val IN = DateTimeFormatter.ofPattern("yyyyMMddHHmmss")
    private val OUT = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")

    private fun iso(date: String, time: String): String {
        val t = (time.ifEmpty { "0000" }).padEnd(6, '0').take(6)
        try {
            return LocalDateTime.parse(date + t, IN).format(OUT)
        } catch (e: java.time.format.DateTimeParseException) {
            throw IllegalArgumentException("'$date $time' is not an ADIF date and time")
        }
    }

    /** An ADIF record as stored fields, plus its APP_SHACK_UUID and
     *  APP_SHACK_ORIGIN (empty when absent). */
    fun toFields(adif: Map<String, String>): Triple<Map<String, Any?>, String, String> {
        val a = LinkedHashMap(adif)
        val out = LinkedHashMap<String, Any?>()
        val uid = (a.remove("APP_SHACK_UUID") ?: "").lowercase(java.util.Locale.ROOT)
        val origin = a.remove("APP_SHACK_ORIGIN") ?: ""
        val date = a.remove("QSO_DATE") ?: ""
        val on = a.remove("TIME_ON") ?: ""
        val dateOff = a.remove("QSO_DATE_OFF") ?: ""
        val off = a.remove("TIME_OFF") ?: ""
        if (date.isNotEmpty()) out["start"] = iso(date, on)
        if (off.isNotEmpty()) {
            // TIME_OFF without QSO_DATE_OFF ends on the start date
            // (icom/app/logbook.py adif_to_record).
            val d = dateOff.ifEmpty { date }
            if (d.isNotEmpty()) out["end"] = iso(d, off)
        }
        for ((src, dst) in listOf("FREQ" to "freq_hz", "FREQ_RX" to "freq_rx_hz")) {
            val v = a.remove(src) ?: continue
            val mhz = v.trim().toBigDecimalOrNull()
            if (mhz != null) out[dst] = mhz.movePointRight(6).setScale(0, java.math.RoundingMode.HALF_EVEN).toLong()
            else out[dst.removeSuffix("_hz")] = v
        }
        for ((k, v) in a) out[k.lowercase(java.util.Locale.ROOT)] = v
        return Triple(out, uid, origin)
    }

    private fun field(name: String, value: Any?): String {
        val s = value.toString()
        return "<$name:${s.length}>$s "
    }

    private val D = DateTimeFormatter.ofPattern("yyyyMMdd")
    private val T = DateTimeFormatter.ofPattern("HHmmss")

    /** ADIF text for live QSO records (wire shape). */
    fun write(records: List<Record>, programVersion: String = ""): String {
        val out = StringBuilder("OpenHamShack logbook export\n")
        out.append(field("ADIF_VER", "3.1.4")).append(field("PROGRAMID", "OpenHamShack"))
        if (programVersion.isNotEmpty()) out.append(field("PROGRAMVERSION", programVersion))
        out.append("<EOH>\n")
        for (rec in records) {
            val f = LinkedHashMap(Merge.fields(rec))
            val parts = StringBuilder()
            (f.remove("start") as? String)?.takeIf { it.isNotEmpty() }?.let {
                val dt = java.time.Instant.ofEpochMilli(Hlc.parseIsoMs(it)).atZone(ZoneOffset.UTC)
                parts.append(field("QSO_DATE", dt.format(D))).append(field("TIME_ON", dt.format(T)))
            }
            (f.remove("end") as? String)?.takeIf { it.isNotEmpty() }?.let {
                val dt = java.time.Instant.ofEpochMilli(Hlc.parseIsoMs(it)).atZone(ZoneOffset.UTC)
                parts.append(field("QSO_DATE_OFF", dt.format(D))).append(field("TIME_OFF", dt.format(T)))
            }
            for ((src, dst) in listOf("freq_hz" to "FREQ", "freq_rx_hz" to "FREQ_RX")) {
                if (src in f) {
                    val hz = f.remove(src).asLong()
                    parts.append(field(dst, java.math.BigDecimal.valueOf(hz).movePointLeft(6).setScale(6).toPlainString()))
                }
            }
            for (k in f.keys.sorted()) {
                val v = f[k]
                if (v == null || v == "") continue
                parts.append(field(k.uppercase(java.util.Locale.ROOT), v))
            }
            parts.append(field("APP_SHACK_UUID", rec["id"]))
            val app = rec["origin"].asMap()["app"].asStr()
            if (app.isNotEmpty()) parts.append(field("APP_SHACK_ORIGIN", app))
            out.append(parts).append("<EOR>\n")
        }
        return out.toString()
    }
}
