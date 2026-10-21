import Foundation

/// The controller refused to pair: `code` is its machine code
/// (`totp_required`, `bad_credentials`, `account_locked`, `use_tv_pairing`,
/// `pair_expired` ...), which an app translates through its own catalog;
/// `message` is the controller's English sentence, the fallback.
public struct PairingError: Error, CustomStringConvertible {
    public let code: String
    public let message: String
    public let status: Int
    public let body: JSONObject
    public var description: String { message }
}

public struct Token: Equatable, CustomStringConvertible {
    public let token: String
    public let deviceId: String
    public let user: String
    public let maxLevel: String
    public var controllerId: String
    public var admin: Bool = false

    // Never print the credential (a crash report or log line would leak it).
    public var description: String { "Token(deviceId: \(deviceId), user: \(user), maxLevel: \(maxLevel), controllerId: \(controllerId))" }
}

public struct TVPairing: CustomStringConvertible {
    public let pairId: String
    public let pairSecret: String
    public let code: String
    public let expiresIn: Int
    public let pollEvery: Double
    public var description: String { "TVPairing(pairId: \(pairId), code: \(code), expiresIn: \(expiresIn))" }
}

/// What a TV sees while it waits: the code to show, then the outcome.
public enum TVPairState: Equatable {
    case code(String, expiresIn: Int)
    case approved(Token)
    case denied
    case expired
}

/// Pairing: exchanging a user's password for a device token, and the TV's
/// six-digit code (CONTROLLER-CONTRACT.md §4.2, §4.3). A port of
/// python/shack_client pairing.py. The token is the only credential a paired
/// app holds; the library hands it to the PairingVault (the Keychain on
/// Apple platforms) and never logs it.
public enum Pairing {
    static func refusal(_ status: Int, _ body: JSONObject) -> PairingError {
        let code = body["code"]?.str ?? ""
        let msg = body["error"]?.str ?? ""
        return PairingError(code: code.isEmpty ? "http_\(status)" : code,
                            message: msg.isEmpty ? "The controller refused to pair this device." : msg, status: status, body: body)
    }

    /// POST api/shack/token. Throws PairingError (code `totp_required`
    /// means: ask for the code and call again) or Offline.
    public static func exchangePassword(_ t: Transport, username: String, password: String, totp: String? = nil,
                                        deviceName: String, deviceKind: String = "phone", clientId: String, app: String) throws -> Token {
        var body: JSONObject = ["username": .string(username), "password": .string(password), "deviceName": .string(deviceName),
                                "deviceKind": .string(deviceKind), "clientId": .string(clientId), "app": .string(app)]
        if let code = totp, !code.isEmpty { body["totp"] = .string(code) }
        let r = try t.post("api/shack/token", body)
        guard r.status == 201, let tok = r.body["token"]?.stringValue, !tok.isEmpty else { throw refusal(r.status, r.body) }
        let user = r.body["user"]?.str ?? ""
        return Token(token: tok, deviceId: r.body["deviceId"]?.str ?? "", user: user.isEmpty ? username : user,
                     maxLevel: r.body["maxLevel"]?.str ?? "", controllerId: r.body["controllerId"]?.str ?? "",
                     admin: r.body["admin"]?.boolValue ?? false)
    }

    /// POST api/shack/pair/start: the code the TV shows.
    public static func tvStart(_ t: Transport, deviceName: String, clientId: String, app: String) throws -> TVPairing {
        let r = try t.post("api/shack/pair/start", ["deviceName": .string(deviceName), "deviceKind": "tv",
                                                    "clientId": .string(clientId), "app": .string(app)])
        guard r.status == 200 || r.status == 201, let pid = r.body["pairId"]?.stringValue else { throw refusal(r.status, r.body) }
        return TVPairing(pairId: pid, pairSecret: r.body["pairSecret"]?.str ?? "", code: r.body["code"]?.str ?? "",
                         expiresIn: Int(r.body["expiresIn"]?.int64Value ?? 300), pollEvery: r.body["pollEvery"]?.doubleValue ?? 2)
    }

    /// One poll: "pending", "approved" (with the token), "denied" or "expired".
    public static func tvPoll(_ t: Transport, _ p: TVPairing) throws -> (String, Token?) {
        let r = try t.post("api/shack/pair/poll", ["pairId": .string(p.pairId), "pairSecret": .string(p.pairSecret)])
        if r.status == 410 { return ("expired", nil) }
        guard r.status == 200 else { throw refusal(r.status, r.body) }
        let state = r.body["state"]?.str ?? ""
        if state == "approved" {
            return (state, Token(token: r.body["token"]?.str ?? "", deviceId: r.body["deviceId"]?.str ?? "",
                                 user: r.body["user"]?.str ?? "", maxLevel: r.body["maxLevel"]?.str ?? "",
                                 controllerId: r.body["controllerId"]?.str ?? ""))
        }
        return (state.isEmpty ? "pending" : state, nil)
    }
}
