package com.openhamshack.client

/**
 * Reading what DNS-SD found (CONTROLLER-CONTRACT.md §2), a port of
 * python/shack_client discovery.py's pure half.
 *
 * Two record types are browsed: `_openhamshack._tcp` (a controller, always
 * with TXT `platform=1`) and the legacy `_openhamshack._tcp`, which is a Classic
 * 0.9.x box when it has no `platform` key (shown as one qo100wb app, PIN
 * session on port 8080) and, with `platform=1`, PLATFORM-PLAN's draft name
 * for the same advert. When both resolve to one host, the `_openhamshack`
 * record wins.
 *
 * Browsing itself is the platform's (Android `NsdManager`, which Remote
 * already drives): it hands each resolved service to [Discovery.parseAdvert]
 * with its TXT attributes as `NsdServiceInfo.getAttributes()` gives them.
 */
data class Controller(
    val name: String,
    /** The advert's 8-hex `id`; the full controllerId comes from [Discovery.wellKnown]. */
    val id: String,
    val url: String,
    val kinds: List<String> = emptyList(),
    val version: String = "",
    val contract: String = "",
    /** A Classic 0.9.x box, not a controller. */
    val classic: Boolean = false,
    val service: String = Discovery.SERVICE,
    val address: String = "",
    val port: Int = 0,
)

/** Something that is not an OpenHamShack controller, or one newer than
 *  this library: [code] is `not_a_controller` or `controller_too_new`. */
class NotAController(val code: String, message: String) : Exception(message)

object Discovery {
    const val SERVICE = "_openhamshack._tcp"
    const val LEGACY = "_openhamshack._tcp"

    private fun txt(props: Map<String, Any?>): Map<String, String> = props.mapValues { (_, v) ->
        when (v) {
            null -> ""
            is ByteArray -> String(v, Charsets.US_ASCII)
            else -> v.toString()
        }
    }

    /** One resolved DNS-SD record as a Controller, or null if it is not
     *  ours. [serviceType] may carry `.local.` and a trailing dot (NsdManager
     *  reports `._openhamshack._tcp` with a leading dot on some versions);
     *  [instance] is the service instance name (the callsign). TXT values
     *  are `ByteArray` (NsdManager) or `String`. */
    fun parseAdvert(serviceType: String, instance: String, address: String, port: Int, props: Map<String, Any?>): Controller? {
        val t = txt(props)
        val st = serviceType.trimStart('.')
        val base = st.substringBefore("._tcp") + "._tcp"
        val name = t["name"].orEmpty().ifEmpty { instance.substringBefore("." + base.substringBefore('.')) }
        val isShack = base == SERVICE
        val isLegacy = base == LEGACY
        if (isShack || (isLegacy && t["platform"] == "1")) {
            if (t["platform"] != "1") return null
            val url = if (t["https"].orEmpty().isNotEmpty()) "https://${host(address)}:${t["https"]}/"
            else "http://${host(address)}:$port/"
            val kinds = t["kinds"].orEmpty().split(",").filter { it.isNotEmpty() }.sorted()
            return Controller(name, t["id"].orEmpty(), url, kinds, t["version"].orEmpty(), t["contract"].orEmpty(), false,
                if (isShack) SERVICE else LEGACY, address, port)
        }
        if (isLegacy) {
            // Classic box: REMOTE-API §1 TXT `name`, `version`, `port`.
            val p = t["port"]?.toIntOrNull() ?: if (port != 0) port else 8080
            return Controller(name, "", "http://${host(address)}:$p/", listOf("qo100wb"), t["version"].orEmpty(), "", true,
                LEGACY, address, p)
        }
        return null
    }

    // An IPv6 literal needs brackets in a URL; the contract is IPv4-only
    // (§2.1) but NsdManager can hand one over.
    private fun host(address: String) = if (':' in address && !address.startsWith("[")) "[$address]" else address

    /** One entry per host: the `_openhamshack` record beats a `_openhamshack`
     *  one with platform=1 for the same address and port; Classic boxes
     *  stay as they are. */
    fun mergeAdverts(found: List<Controller>): List<Controller> {
        val best = LinkedHashMap<Triple<String, Int, Boolean>, Controller>()
        for (c in found) {
            val key = Triple(c.address, c.port, c.classic)
            val cur = best[key]
            if (cur == null || (c.service == SERVICE && cur.service != SERVICE)) best[key] = c
        }
        return best.values.sortedWith(compareBy({ it.name }, { it.url }))
    }

    /**
     * GET <base>.well-known/openhamshack, with every relative reference in
     * it resolved against the base URL (not the document's own URL: by RFC
     * 3986 "api/shack/token" against ".../.well-known/openhamshack" lands
     * under .well-known, which is not what the contract means).
     * Throws [NotAController] or [Offline].
     */
    fun wellKnown(baseUrl: String, transport: Transport = HttpTransport(baseUrl, null, 5_000)): Map<String, Any?> {
        val (status, doc) = transport.get(".well-known/openhamshack")
        if (status != 200 || "openhamshack" !in doc) {
            throw NotAController("not_a_controller", "$baseUrl is not an OpenHamShack controller")
        }
        if ((doc["openhamshack"] as? Number)?.toLong() != 1L) {
            throw NotAController("controller_too_new", "This controller is newer than this app: update the app to use it")
        }
        val out = LinkedHashMap(doc)
        out["auth"] = doc["auth"].asMap().mapValues { (k, v) ->
            if ((k == "token" || k == "pair") && v is String) Urls.join(baseUrl, v) else v
        }
        val services = doc["services"].asMap()
        if ("base" in services) out["services"] = LinkedHashMap(services).also { it["base"] = Urls.join(baseUrl, services["base"].asStr()) }
        out["apps"] = doc["apps"].asList().map { a ->
            LinkedHashMap(a.asMap()).also { m ->
                m["base"] = Urls.join(baseUrl, m["base"].asStr())
                if (m["icon"].asStr().isNotEmpty()) m["icon"] = Urls.join(baseUrl, m["icon"].asStr())
            }
        }
        return out
    }
}
