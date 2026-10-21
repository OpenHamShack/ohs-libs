package com.openhamshack.client

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.net.URLEncoder

/** No answer from the controller: refused, unreachable, timed out, or the
 *  connection dropped mid-answer. The one case the sync loop treats as "the
 *  controller is away" rather than "the controller said no". */
open class Offline(message: String) : IOException(message)

/** One request to a controller, answering (status, JSON object) for any
 *  HTTP status and throwing [Offline] only when no answer came at all. */
fun interface Transport {
    fun request(method: String, path: String, body: Map<String, Any?>?, query: Map<String, Any?>?): Response

    data class Response(val status: Int, val body: Map<String, Any?>)
}

fun Transport.get(path: String, query: Map<String, Any?>? = null) = request("GET", path, null, query)
fun Transport.post(path: String, body: Map<String, Any?>) = request("POST", path, body, null)

/** Makes the transport for a base URL and token (null before pairing).
 *  Tests pass one that reaches an in-process controller. */
fun interface TransportFactory {
    fun create(baseUrl: String, token: String?): Transport
}

object Urls {
    /** [path] resolved against [baseUrl] as a relative reference, never
     *  built absolute (CONTROLLER-CONTRACT.md §3): the same controller is
     *  reached under a path prefix on one address and at the root of
     *  another. */
    fun join(baseUrl: String, path: String, query: Map<String, Any?>? = null): String {
        val base = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        var url = URI(base).resolve(path.trimStart('/')).toString()
        val q = query?.filterValues { it != null }
        if (!q.isNullOrEmpty()) {
            url += "?" + q.entries.joinToString("&") { (k, v) ->
                URLEncoder.encode(k, "UTF-8") + "=" + URLEncoder.encode(v.toString(), "UTF-8")
            }
        }
        return url
    }
}

/**
 * HTTP over java.net.HttpURLConnection, which Android and the JVM both
 * have, so the library needs no HTTP dependency. Timeouts are §6.3's 10 s.
 * The token goes in an Authorization header, never a URL (§4.1).
 */
class HttpTransport(private val baseUrl: String, private val token: String? = null,
                    private val timeoutMs: Int = TIMEOUT_MS) : Transport {
    override fun request(method: String, path: String, body: Map<String, Any?>?, query: Map<String, Any?>?): Transport.Response {
        val conn = try {
            URL(Urls.join(baseUrl, path, query)).openConnection() as HttpURLConnection
        } catch (e: IOException) {
            throw Offline(e.message ?: e.toString())
        }
        try {
            conn.requestMethod = method
            conn.connectTimeout = timeoutMs
            conn.readTimeout = timeoutMs
            conn.instanceFollowRedirects = false
            conn.setRequestProperty("Accept", "application/json")
            if (token != null) conn.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                val raw = Json.dump(body).toByteArray(Charsets.UTF_8)
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                // Buffered, not setFixedLengthStreamingMode: in streaming
                // mode the JDK's connection drops the body of a 401, and
                // that body is the `totp_required` the client must read.
                conn.outputStream.use { it.write(raw) }
            }
            val status = conn.responseCode
            val stream = if (status >= 400) conn.errorStream else conn.inputStream
            val raw = stream?.use { it.readBytes() } ?: ByteArray(0)
            return Transport.Response(status, decode(raw))
        } catch (e: IOException) {
            throw Offline(e.message ?: e.toString())
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        const val TIMEOUT_MS = 10_000

        val FACTORY = TransportFactory { base, token -> HttpTransport(base, token) }

        internal fun decode(raw: ByteArray): Map<String, Any?> {
            if (raw.isEmpty()) return emptyMap()
            val v = try {
                Json.parse(String(raw, Charsets.UTF_8))
            } catch (e: Json.ParseError) {
                return mapOf("error" to "The controller answered with something that is not JSON.", "code" to "bad_response")
            }
            @Suppress("UNCHECKED_CAST")
            return v as? Map<String, Any?> ?: mapOf("value" to v)
        }
    }
}
