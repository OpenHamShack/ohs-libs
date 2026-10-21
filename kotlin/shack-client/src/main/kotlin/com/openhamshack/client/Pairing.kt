package com.openhamshack.client

/**
 * Pairing: exchanging a user's password for a device token, and the TV's
 * six-digit code (CONTROLLER-CONTRACT.md §4.2, §4.3). A port of
 * python/shack_client pairing.py.
 *
 * The token is the only credential a paired app holds. It appears in the
 * exchange's answer once; the library hands it to the [PairingVault] and
 * never logs it.
 */

/** The controller refused: [code] is its machine code (`totp_required`,
 *  `bad_credentials`, `account_locked`, `use_tv_pairing`, `pair_expired`
 *  ...), which an app translates through its own catalog; [message] is the
 *  controller's English sentence, the fallback. */
class PairingError(val code: String, message: String, val status: Int = 0,
                   val body: Map<String, Any?> = emptyMap()) : Exception(message)

data class Token(
    val token: String,
    val deviceId: String,
    val user: String,
    val maxLevel: String,
    val controllerId: String,
    val admin: Boolean = false,
) {
    // Never print the credential (a crash report or log line would leak it).
    override fun toString() = "Token(deviceId=$deviceId, user=$user, maxLevel=$maxLevel, controllerId=$controllerId)"
}

data class TvPairing(val pairId: String, val pairSecret: String, val code: String, val expiresIn: Int, val pollEvery: Double) {
    override fun toString() = "TvPairing(pairId=$pairId, code=$code, expiresIn=$expiresIn)"
}

/** What a TV sees while it waits: the code to show, then the outcome. */
sealed class TvPairState {
    data class Code(val code: String, val expiresIn: Int) : TvPairState()
    data class Approved(val token: Token) : TvPairState()
    object Denied : TvPairState()
    object Expired : TvPairState()
}

object Pairing {
    private fun refusal(status: Int, body: Map<String, Any?>) = PairingError(
        body["code"].asStr().ifEmpty { "http_$status" },
        body["error"].asStr().ifEmpty { "The controller refused to pair this device." }, status, body)

    /** POST api/shack/token. Throws [PairingError] (code `totp_required`
     *  means: ask for the code and call again) or [Offline]. */
    fun exchangePassword(t: Transport, username: String, password: String, totp: String? = null,
                         deviceName: String, deviceKind: String = "phone", clientId: String, app: String): Token {
        val body = linkedMapOf<String, Any?>("username" to username, "password" to password, "deviceName" to deviceName,
            "deviceKind" to deviceKind, "clientId" to clientId, "app" to app)
        if (!totp.isNullOrEmpty()) body["totp"] = totp
        val (status, ans) = t.post("api/shack/token", body)
        if (status != 201 || ans["token"].asStr().isEmpty()) throw refusal(status, ans)
        return Token(ans["token"] as String, ans["deviceId"].asStr(), ans["user"].asStr().ifEmpty { username },
            ans["maxLevel"].asStr(), ans["controllerId"].asStr(), ans["admin"] == true)
    }

    /** POST api/shack/pair/start: the code the TV shows. */
    fun tvStart(t: Transport, deviceName: String, clientId: String, app: String): TvPairing {
        val (status, ans) = t.post("api/shack/pair/start", mapOf("deviceName" to deviceName, "deviceKind" to "tv",
            "clientId" to clientId, "app" to app))
        if ((status != 200 && status != 201) || "pairId" !in ans) throw refusal(status, ans)
        return TvPairing(ans["pairId"] as String, ans["pairSecret"].asStr(), ans["code"].asStr(),
            ans["expiresIn"].asLong(300).toInt(), (ans["pollEvery"] as? Number)?.toDouble() ?: 2.0)
    }

    /** One poll: `pending`, `approved` (with the token), `denied` or
     *  `expired`. */
    fun tvPoll(t: Transport, p: TvPairing): Pair<String, Token?> {
        val (status, ans) = t.post("api/shack/pair/poll", mapOf("pairId" to p.pairId, "pairSecret" to p.pairSecret))
        if (status == 410) return "expired" to null
        if (status != 200) throw refusal(status, ans)
        val state = ans["state"].asStr().ifEmpty { "pending" }
        if (state == "approved") {
            return state to Token(ans["token"] as String, ans["deviceId"].asStr(), ans["user"].asStr(),
                ans["maxLevel"].asStr(), ans["controllerId"].asStr())
        }
        return state to null
    }
}
