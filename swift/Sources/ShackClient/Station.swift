import Foundation
#if canImport(Security)
import Security
#endif

public struct QSO: Equatable {
    public let uuid: String
    public let fields: JSONObject
    public let createdAt: String
    public let origin: JSONObject
    public let deleted: Bool
    public let hlc: String
    /// Contacts this one probably duplicates (same call, band, mode within
    /// 300 s): advice, not a rule (SERVICES.md §3.1). Filled on add.
    public var duplicateOf: [String] = []

    static func of(_ rec: Record) -> QSO {
        QSO(uuid: rec["id"]?.str ?? "", fields: Merge.fields(rec), createdAt: rec["createdAt"]?.str ?? "",
            origin: rec["origin"]?.obj ?? [:], deleted: !Merge.isLive(rec), hlc: Merge.stamps(rec).max() ?? "")
    }
}

/// A finished pairing.
public struct PairingInfo: Equatable {
    public let controllerId: String
    public let name: String
    public let baseURL: String
    public let deviceId: String
    public let user: String
    public let maxLevel: String
}

/// What `Station.pair` returns.
public enum PairResult: Equatable {
    case paired(PairingInfo)
    /// §7: this device and the controller both have a log and different
    /// callsigns. The app asks: merge, or use the controller's log.
    case needsDecision(PairingInfo, localCallsign: String, controllerCallsign: String, localCount: Int, controllerHasLog: Bool)

    public var pairing: PairingInfo {
        switch self {
        case .paired(let p): return p
        case .needsDecision(let p, _, _, _, _): return p
        }
    }
}

/// Where the pairing (base URL, device token, controller) is kept: the
/// Keychain on Apple platforms (SYNC.md §2.1), or pairing.json beside the
/// store (mode 0600) where there is no Keychain to use, as in tests.
public protocol PairingVault: AnyObject {
    func read() -> JSONObject?
    func write(_ info: JSONObject) throws
    func delete()
}

public final class FileVault: PairingVault {
    let url: URL
    public init(_ url: URL) { self.url = url }

    public func read() -> JSONObject? {
        guard let d = try? Data(contentsOf: url), let v = try? JSON.parse(data: d) else { return nil }
        return v.objectValue
    }

    public func write(_ info: JSONObject) throws {
        let tmp = url.appendingPathExtension("tmp")
        try? FileManager.default.removeItem(at: tmp)
        // Owner-only before the token is written into it.
        guard FileManager.default.createFile(atPath: tmp.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw ShackError(code: "vault_write_failed", message: "Could not save the pairing.")
        }
        try Data(JSON.dump(.object(info)).utf8).write(to: tmp)
        _ = try? FileManager.default.removeItem(at: url)
        try FileManager.default.moveItem(at: tmp, to: url)
    }

    public func delete() { try? FileManager.default.removeItem(at: url) }
}

#if canImport(Security)
/// The pairing as one generic-password Keychain item, readable only while
/// the device is unlocked and never synced to iCloud or restored to another
/// device (a device token belongs to this device).
public final class KeychainVault: PairingVault {
    let service: String
    let account: String
    public init(service: String = "com.openhamshack.pairing", account: String = "station") {
        self.service = service
        self.account = account
    }

    private var base: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }

    public func read() -> JSONObject? {
        var q = base
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data,
              let v = try? JSON.parse(data: d) else { return nil }
        return v.objectValue
    }

    public func write(_ info: JSONObject) throws {
        delete()
        var q = base
        q[kSecValueData as String] = Data(JSON.dump(.object(info)).utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let rc = SecItemAdd(q as CFDictionary, nil)
        guard rc == errSecSuccess else { throw ShackError(code: "vault_write_failed", message: "Could not save the pairing (\(rc)).") }
    }

    public func delete() { SecItemDelete(base as CFDictionary) }
}
#endif

public final class Logbook {
    unowned let st: Station
    init(_ st: Station) { self.st = st }

    public static let duplicateWindowS: Int64 = 300   // SERVICES.md §3.1

    private func clean(_ fields: JSONObject, require: Bool) throws -> JSONObject {
        var out: JSONObject = [:]
        for (k, v) in fields {
            let ok = !k.isEmpty && k.utf8.first.map { $0 >= 0x61 && $0 <= 0x7A } == true &&
                k.utf8.allSatisfy { ($0 >= 0x61 && $0 <= 0x7A) || ($0 >= 0x30 && $0 <= 0x39) || $0 == 0x5F }
            guard ok else { throw ShackError(code: "bad_field_name", message: "'\(k)' is not a logbook field name: fields are lower-case ADIF names") }
            out[k] = v
        }
        if let call = out["call"], !call.isNull {
            // String.uppercased() is locale-independent: the Turkish dotted
            // I never reaches a callsign (I18N.md §4.2).
            let s = call.stringValue ?? JSON.dump(call)
            out["call"] = .string(s.trimmingCharacters(in: .whitespacesAndNewlines).uppercased())
        }
        if require {
            if (out["call"]?.str ?? "").isEmpty { throw ShackError(code: "call_required", message: "A contact needs a callsign") }
            if (out["start"]?.str ?? "").isEmpty { throw ShackError(code: "start_required", message: "A contact needs a start time") }
        }
        if let start = out["start"], !start.isNull, start != .string("") {
            guard let s = start.stringValue, (try? Hlc.parseIsoMs(s)) != nil else {
                throw ShackError(code: "bad_start", message: "The start time '\(start.str)' is not an ISO date and time")
            }
        }
        return out
    }

    /// Adds a contact; idempotent by UUID (SERVICES.md §3.2 POST): the same
    /// contact again is the stored contact, not a second one.
    @discardableResult
    public func add(_ fields: JSONObject, uuid: String? = nil) throws -> QSO {
        let f = try clean(fields, require: true)
        let rid = try (uuid ?? Store.uuid7(st.store.now())).lowercased()
        if try st.store.get("logbook", rid) != nil { return try get(rid)! }
        var q = QSO.of(try st.store.edit("logbook", rid, kind: "create", fields: f, origin: st.origin()))
        q.duplicateOf = try duplicates(q)
        st.localChange("logbook", [rid])
        return q
    }

    /// Several contacts in one transaction; a `uuid` key in an item is its id.
    @discardableResult
    public func addMany(_ items: [JSONObject]) throws -> [QSO] {
        let edits = try items.map { fields -> Store.Edit in
            var f = fields
            let uid = f.removeValue(forKey: "uuid")?.str.lowercased() ?? ""
            return Store.Edit("logbook", uid.isEmpty ? Store.uuid7(try st.store.now()) : uid, "create",
                              try clean(f, require: true), st.origin())
        }
        let recs = try st.store.editMany(edits)
        st.localChange("logbook", recs.map { $0["id"]?.str ?? "" })
        return recs.map(QSO.of)
    }

    @discardableResult
    public func update(_ uuid: String, _ fields: JSONObject) throws -> QSO {
        guard try st.store.get("logbook", uuid) != nil else {
            throw ShackError(code: "no_such_contact", message: "There is no contact with that id in this logbook")
        }
        let q = QSO.of(try st.store.edit("logbook", uuid, kind: "update", fields: try clean(fields, require: false), origin: st.origin()))
        st.localChange("logbook", [uuid])
        return q
    }

    public func delete(_ uuid: String) throws {
        guard try st.store.get("logbook", uuid) != nil else {
            throw ShackError(code: "no_such_contact", message: "There is no contact with that id in this logbook")
        }
        try st.store.edit("logbook", uuid, kind: "delete", fields: [:], origin: st.origin())
        st.localChange("logbook", [uuid])
    }

    public func get(_ uuid: String) throws -> QSO? { try st.store.get("logbook", uuid).map(QSO.of) }

    /// Newest first. Deleted contacts are skipped unless asked for (lists and
    /// lookups skip them; history and sync do not: §5).
    public func query(call: String? = nil, band: String? = nil, mode: String? = nil, since: String? = nil, until: String? = nil,
                      limit: Int = 100, includeDeleted: Bool = false) throws -> [QSO] {
        var sql = "SELECT id FROM qso_index WHERE 1=1"
        var args: [Any?] = []
        for (col, v) in [("call", call?.uppercased()), ("band", band), ("mode", mode?.uppercased())] {
            if let v = v, !v.isEmpty { sql += " AND \(col)=?"; args.append(v) }
        }
        if let s = since, !s.isEmpty { sql += " AND start>=?"; args.append(s) }
        if let u = until, !u.isEmpty { sql += " AND start<=?"; args.append(u) }
        if !includeDeleted { sql += " AND deleted=0" }
        sql += " ORDER BY start DESC, id LIMIT ?"
        args.append(Int64(limit))
        let ids = try st.store.queryList(sql, args).map { $0[0] as! String }
        return try ids.compactMap { try get($0) }
    }

    public func count() throws -> Int { Int(try st.store.query("SELECT COUNT(*) FROM qso_index WHERE deleted=0")[0][0] as? Int64 ?? 0) }

    public func workedBefore(_ call: String, band: String? = nil, mode: String? = nil) throws -> [QSO] {
        try query(call: call, band: band, mode: mode, limit: 1000)
    }

    private func startMs(_ f: JSONObject) -> Int64? { f["start"]?.stringValue.flatMap { try? Hlc.parseIsoMs($0) } }

    private func bandOf(_ f: JSONObject) -> String {
        let b = f["band"]?.str ?? ""
        return b.isEmpty ? Adif.bandFor(f["freq_hz"]) : b
    }

    private func duplicates(_ q: QSO) throws -> [String] {
        guard let t = startMs(q.fields) else { return [] }
        let band = bandOf(q.fields)
        return try query(call: q.fields["call"]?.str, band: band.isEmpty ? nil : band, mode: q.fields["mode"]?.stringValue, limit: 50)
            .filter { $0.uuid != q.uuid }
            .filter { o in startMs(o.fields).map { abs($0 / 1000 - t / 1000) <= Logbook.duplicateWindowS } ?? false }
            .map(\.uuid)
    }

    /// Every edit made to this contact on this device, oldest first. The
    /// controller's history (SERVICES.md §3.2) adds every other device's.
    public func history(_ uuid: String) throws -> [JSONObject] {
        try st.store.query("SELECT op_id, hlc, kind, fields, origin, created_at FROM ops WHERE collection='logbook' AND id=? ORDER BY hlc", uuid)
            .map { r in ["opId": .string(r[0] as! String), "hlc": .string(r[1] as! String), "kind": .string(r[2] as! String),
                         "fields": (try? JSON.parse(r[3] as! String)) ?? .object([:]),
                         "origin": (try? JSON.parse(r[4] as! String)) ?? .object([:]), "createdAt": .string(r[5] as? String ?? "")] }
    }

    public func exportADIF(programVersion: String = "") throws -> String {
        let recs = try st.store.all("logbook").filter(Merge.isLive)
            .sorted { (Merge.fields($0)["start"]?.str ?? "", $0["id"]?.str ?? "") < (Merge.fields($1)["start"]?.str ?? "", $1["id"]?.str ?? "") }
        return Adif.write(recs, programVersion: programVersion.isEmpty ? st.appVersion : programVersion)
    }

    public struct ImportReport {
        public let added: Int
        public let duplicates: [JSONObject]
        public let errors: [JSONObject]
    }

    /// SERVICES.md §3.2 import: every record keeps its APP_SHACK_UUID or gets
    /// a fresh v7 UUID; duplicates are reported, never skipped silently.
    public func importADIF(_ text: String) throws -> ImportReport {
        var dups: [JSONObject] = [], errors: [JSONObject] = [], edits: [Store.Edit] = [], pending: [QSO] = []
        for (line, rec) in Adif.parse(text) {
            let fields: JSONObject, uid: String
            do {
                let t = try Adif.toFields(rec)
                fields = try clean(t.0, require: true)
                uid = t.1
            } catch let e as ShackError {
                errors.append(["line": .int(Int64(line)), "error": .string(e.message), "code": .string(e.code)])
                continue
            }
            if !uid.isEmpty, try st.store.get("logbook", uid) != nil {
                dups.append(["line": .int(Int64(line)), "of": .string(uid)])
                continue
            }
            let rid = uid.isEmpty ? Store.uuid7(try st.store.now()) : uid
            let probe = QSO(uuid: rid, fields: fields, createdAt: "", origin: [:], deleted: false, hlc: "")
            var same = try duplicates(probe)
            if same.isEmpty { same = pending.filter { near($0, probe) }.map(\.uuid) }
            if let first = same.first {
                dups.append(["line": .int(Int64(line)), "of": .string(first)])
                continue
            }
            pending.append(probe)
            edits.append(Store.Edit("logbook", rid, "create", fields, st.origin()))
        }
        if !edits.isEmpty {
            _ = try st.store.editMany(edits)
            st.localChange("logbook", edits.map(\.id))
        }
        return ImportReport(added: edits.count, duplicates: dups, errors: errors)
    }

    private func near(_ a: QSO, _ b: QSO) -> Bool {
        let fa = a.fields, fb = b.fields
        guard fa["call"] == fb["call"], (fa["mode"]?.str ?? "").uppercased() == (fb["mode"]?.str ?? "").uppercased(),
              bandOf(fa) == bandOf(fb), let ta = startMs(fa), let tb = startMs(fb) else { return false }
        return abs(ta - tb) <= Logbook.duplicateWindowS * 1000
    }
}

/// The station profile: one record (collection "profile", id "station"),
/// SERVICES.md §2.1, merged per field with the two maps split into dotted
/// paths (SYNC.md §4.2).
public final class Profile {
    public static let id = "station"
    unowned let st: Station
    init(_ st: Station) { self.st = st }

    public var current: JSONObject { (try? get()) ?? ["schema": 1] }

    public func get() throws -> JSONObject {
        var out: JSONObject = ["schema": 1]
        if let rec = try st.store.get("profile", Profile.id) {
            for (k, v) in Merge.joinProfile(Merge.fields(rec)) { out[k] = v }
        }
        return out
    }

    private static func isLocator(_ s: String) -> Bool {
        let c = Array(s.uppercased().utf8)
        guard [4, 6, 8].contains(c.count) else { return false }
        func inRange(_ i: Int, _ lo: Character, _ hi: Character) -> Bool { c[i] >= lo.asciiValue! && c[i] <= hi.asciiValue! }
        guard inRange(0, "A", "R"), inRange(1, "A", "R"), inRange(2, "0", "9"), inRange(3, "0", "9") else { return false }
        if c.count >= 6 { guard inRange(4, "A", "X"), inRange(5, "A", "X") else { return false } }
        if c.count == 8 { guard inRange(6, "0", "9"), inRange(7, "0", "9") else { return false } }
        return true
    }

    @discardableResult
    public func update(_ partial: JSONObject) throws -> JSONObject {
        var p = partial
        if let c = p["callsign"], !c.isNull { p["callsign"] = .string(c.str.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()) }
        if let l = p["locator"]?.stringValue {
            let loc = l.trimmingCharacters(in: .whitespacesAndNewlines)
            if !loc.isEmpty {
                guard Profile.isLocator(loc) else {
                    throw ShackError(code: "bad_locator", message: "\(loc) is not a Maidenhead locator (4, 6 or 8 characters)")
                }
                p["locator"] = .string(String(loc.prefix(6)).uppercased() + String(loc.dropFirst(6)))
            }
        }
        p.removeValue(forKey: "schema")
        p.removeValue(forKey: "updatedAt")
        try st.store.edit("profile", Profile.id, kind: "update", fields: Merge.splitProfile(p), origin: st.origin())
        st.localChange("profile", [Profile.id])
        return try get()
    }
}

/// The API apps use (SYNC.md §9.3), a port of python/shack_client
/// station.py: one Station per store, with `logbook`, `profile` and `sync`.
///
///     let station = try Station.open(app: "remote")
///     let qso = try station.logbook.add(["call": "DL1ABC", "start": "2026-10-09T10:12:30Z"])
///     try station.profile.update(["locator": "IO75RL"])
///     let result = try station.pair(baseURL, username: "brian", password: pw, deviceName: "iPad")
///
/// Every call here reads or writes the local store and returns at once; the
/// three that talk to a controller (`pair`, `pairTV`, `resolve`) block on
/// the network and an app runs them off the main actor (`Task.detached`).
public final class Station: @unchecked Sendable {
    public let store: Store
    public let app: String
    public let appVersion: String
    public let vault: PairingVault
    private let transports: TransportFactory
    private let background: Bool
    public private(set) lazy var logbook = Logbook(self)
    public private(set) lazy var profile = Profile(self)
    public private(set) lazy var sync = SyncClient(self)
    private let listenerLock = NSLock()
    private var listeners: [UUID: (String?, [String]) -> Void] = [:]
    private var watcher: Thread?
    private var closed = false

    public init(store: Store, app: String, appVersion: String = "", transports: @escaping TransportFactory = HTTPTransport.factory,
                background: Bool = true, vault: PairingVault? = nil) {
        self.store = store
        self.app = app
        self.appVersion = appVersion
        self.transports = transports
        self.background = background
        self.vault = vault ?? FileVault(store.path.deletingLastPathComponent().appendingPathComponent("pairing.json"))
        if background && pairingInfo() != nil { sync.start() }
    }

    /// The store in the app's private Application Support directory and the
    /// pairing in the Keychain (§2.1). `path` and `vault` are for tests and
    /// desktop tools.
    public static func open(app: String, appVersion: String = "", path: URL? = nil, clock: @escaping () -> Int64 = Hlc.systemMs,
                            transports: @escaping TransportFactory = HTTPTransport.factory, background: Bool = true,
                            vault: PairingVault? = nil) throws -> Station {
        let url = try path ?? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("OpenHamShack", isDirectory: true).appendingPathComponent("station.db")
        var v = vault
        #if canImport(Security)
        if v == nil && path == nil { v = KeychainVault() }
        #endif
        return Station(store: try Store(path: url, clock: clock), app: app, appVersion: appVersion, transports: transports,
                       background: background, vault: v)
    }

    // MARK: identity

    public func pairingInfo() -> JSONObject? { vault.read() }

    public func origin() -> JSONObject {
        let p = pairingInfo() ?? [:]
        let dev = p["deviceId"]?.str ?? ""
        return ["app": .string(app), "device": .string(dev.isEmpty ? store.storeId : dev), "user": .string(p["user"]?.str ?? "")]
    }

    /// A UUID per installation (REMOTE-API §7 `remote`), kept in the store.
    public func clientId() throws -> String {
        if let c = try store.meta("client_id") { return c }
        return try store.tx {
            if let c = try store.meta("client_id") { return c }
            let c = UUID().uuidString.lowercased()
            try store.setMeta("client_id", c)
            return c
        }
    }

    public func transport() throws -> Transport {
        guard let p = pairingInfo() else { throw Offline("not paired") }
        return transports(p["baseUrl"]?.str ?? "", p["token"]?.str ?? "")
    }

    // MARK: change notification

    /// `listener(collection, ids)` after local or synced changes in this
    /// process; `(nil, [])` when another process changed the store (noticed
    /// by PRAGMA data_version, once a second).
    @discardableResult
    public func onChange(_ listener: @escaping (String?, [String]) -> Void) -> UUID {
        let id = UUID()
        listenerLock.lock()
        listeners[id] = listener
        let start = watcher == nil
        if start {
            let t = Thread { [weak self] in self?.watchLoop() }
            t.name = "shack-store-watch"
            watcher = t
            t.start()
        }
        listenerLock.unlock()
        return id
    }

    public func removeChangeListener(_ id: UUID) {
        listenerLock.lock(); listeners.removeValue(forKey: id); listenerLock.unlock()
    }

    /// Changes as an AsyncStream for SwiftUI: (collection, ids).
    public var changes: AsyncStream<(String?, [String])> {
        AsyncStream { cont in
            let id = onChange { cont.yield(($0, $1)) }
            cont.onTermination = { [weak self] _ in self?.removeChangeListener(id) }
        }
    }

    private var isClosed: Bool { listenerLock.lock(); defer { listenerLock.unlock() }; return closed }

    private func watchLoop() {
        guard var seen = try? store.dataVersion() else { return }
        while !isClosed {
            Thread.sleep(forTimeInterval: 1)
            if isClosed { return }
            guard let now = try? store.dataVersion() else { return }
            if now != seen {
                seen = now
                each { $0(nil, []) }
            }
        }
    }

    private func each(_ f: ((String?, [String]) -> Void) -> Void) {
        listenerLock.lock()
        let ls = Array(listeners.values)
        listenerLock.unlock()
        ls.forEach(f)
    }

    func notify(_ keys: [RecordKey]) {
        var by: [String: [String]] = [:]
        var order: [String] = []
        for k in keys {
            if by[k.collection] == nil { order.append(k.collection) }
            by[k.collection, default: []].append(k.id)
        }
        for c in order { each { $0(c, by[c]!) } }
    }

    func localChange(_ collection: String, _ ids: [String]) {
        each { $0(collection, ids) }
        sync.localWrite()
    }

    // MARK: pairing

    /// Pair with a controller by password (CONTROLLER-CONTRACT.md §4.2) and,
    /// when nothing needs deciding, do the first full exchange (§7). Throws
    /// PairingError or Offline. Blocks on the network.
    @discardableResult
    public func pair(_ baseURL: String, username: String, password: String, totp: String? = nil, deviceName: String = "",
                     deviceKind: String = "phone") throws -> PairResult {
        let t = transports(baseURL, nil)
        let tok = try Pairing.exchangePassword(t, username: username, password: password, totp: totp,
                                               deviceName: deviceName.isEmpty ? app : deviceName, deviceKind: deviceKind,
                                               clientId: try clientId(), app: app)
        return try finishPairing(baseURL, t, tok)
    }

    /// Pair a TV by code (§4.3): `onCode` gets the six digits to show; this
    /// then polls until a person approves, denies, or the code expires.
    /// Returns the outcome and, when approved, the pairing result. Blocks;
    /// `sleep` is for tests.
    public func pairTV(_ baseURL: String, deviceName: String, onCode: (TVPairState) -> Void,
                       sleep: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) }) throws -> (TVPairState, PairResult?) {
        let t = transports(baseURL, nil)
        let p = try Pairing.tvStart(t, deviceName: deviceName, clientId: try clientId(), app: app)
        onCode(.code(p.code, expiresIn: p.expiresIn))
        let deadline = Date().addingTimeInterval(TimeInterval(p.expiresIn))
        while Date() < deadline {
            let (state, tok) = try Pairing.tvPoll(t, p)
            switch state {
            case "approved":
                var token = tok!
                if token.controllerId.isEmpty { token.controllerId = try Discovery.wellKnown(baseURL, transport: t)["controllerId"]?.str ?? "" }
                return (.approved(token), try finishPairing(baseURL, t, token))
            case "denied": return (.denied, nil)
            case "expired": return (.expired, nil)
            default: sleep(p.pollEvery)
            }
        }
        return (.expired, nil)
    }

    /// The same as pairTV, as an AsyncStream for a SwiftUI TV screen:
    /// `.code` first, then exactly one outcome. Runs on a background thread.
    public func pairTVUpdates(_ baseURL: String, deviceName: String) -> AsyncThrowingStream<TVPairState, Error> {
        AsyncThrowingStream { cont in
            Thread { [self] in
                do {
                    let (outcome, _) = try pairTV(baseURL, deviceName: deviceName, onCode: { cont.yield($0) })
                    cont.yield(outcome)
                    cont.finish()
                } catch {
                    cont.finish(throwing: error)
                }
            }.start()
        }
    }

    private func finishPairing(_ baseURL: String, _ t: Transport, _ tok: Token) throws -> PairResult {
        var name = "", stationCall = ""
        if let r = try? t.get(".well-known/openhamshack"), r.status == 200 {
            name = r.body["name"]?.str ?? ""
            stationCall = r.body["station"]?["callsign"].str ?? ""
        }
        try vault.write(["controllerId": .string(tok.controllerId), "baseUrl": .string(baseURL), "token": .string(tok.token),
                         "deviceId": .string(tok.deviceId), "user": .string(tok.user), "maxLevel": .string(tok.maxLevel),
                         "name": .string(name), "pairedAt": .string(try store.nowIso())])
        let pairing = PairingInfo(controllerId: tok.controllerId, name: name, baseURL: baseURL, deviceId: tok.deviceId,
                                  user: tok.user, maxLevel: tok.maxLevel)
        // A first exchange pushes everything (§7): records confirmed by
        // another controller are not dirty, but this one lacks them.
        if try store.cursor(tok.controllerId) == nil { try store.markAllDirty() }
        if let decision = try needsDecision(pairing, stationCall) {
            try store.tx { try store.setMeta("needs_decision", tok.controllerId) }
            return decision
        }
        try sync.runOnce()
        if background { sync.start() }
        return .paired(pairing)
    }

    /// §7: a full exchange when the controller's log is empty or its
    /// callsign matches; also when this device has no log at all.
    private func needsDecision(_ pairing: PairingInfo, _ stationCall: String) throws -> PairResult? {
        // A controller this store has synced with before already holds the
        // union: re-pairing with it is as a first pairing, minus the question.
        let known = (try? JSON.parse(try store.meta("synced_with") ?? "[]"))?.arr ?? []
        if try store.cursor(pairing.controllerId) != nil || known.contains(.string(pairing.controllerId)) { return nil }
        let localN = try logbook.count()
        if localN == 0 { return nil }
        let t = try transport()
        let logs = try t.get("api/shack/v1/sync/pull", query: [("since", "0"), ("limit", "1"), ("collections", "logbook")])
        try check(logs)
        if (logs.body["records"]?.arr ?? []).isEmpty { return nil }
        // The controller's station callsign: its public advert
        // (/.well-known station.callsign), else its synced profile.
        var theirs = stationCall
        if theirs.isEmpty {
            let prof = try t.get("api/shack/v1/sync/pull", query: [("since", "0"), ("limit", "5"), ("collections", "profile")])
            try check(prof)
            theirs = (prof.body["records"]?.arr ?? []).map(\.obj).first { $0["id"]?.str == Profile.id }
                .map { Merge.fields($0)["callsign"]?.str ?? "" } ?? ""
        }
        let mine = try profile.get()["callsign"]?.str ?? ""
        if !theirs.isEmpty && !mine.isEmpty && theirs.uppercased() == mine.uppercased() { return nil }
        return .needsDecision(pairing, localCallsign: mine, controllerCallsign: theirs, localCount: localN, controllerHasLog: true)
    }

    public enum Choice { case merge, useController }

    /// Answer a needsDecision: merge this device's log with the controller's,
    /// or archive this one (station-<date>.db and an .adi beside it) and take
    /// the controller's. There is no "keep two logs": one store is one
    /// station. Blocks on the network.
    @discardableResult
    public func resolve(_ result: PairResult, choice: Choice) throws -> PairingInfo {
        if choice == .useController {
            let day = String(try store.nowIso().prefix(10))
            let dir = store.path.deletingLastPathComponent()
            var base = "station-\(day)"
            var n = 1
            while FileManager.default.fileExists(atPath: dir.appendingPathComponent(base + ".db").path) {
                n += 1
                base = "station-\(day)-\(n)"
            }
            try Data(try logbook.exportADIF().utf8).write(to: dir.appendingPathComponent(base + ".adi"))
            try store.archive(to: dir.appendingPathComponent(base + ".db"))
            try store.wipeReplica()
        }
        try store.deleteMeta("needs_decision")
        try sync.runOnce()
        if background { sync.start() }
        return result.pairing
    }

    /// §7: forget the pairing and that controller's cursor. Records, ops and
    /// history stay exactly as they are; no network request.
    public func unpair() throws {
        let p = pairingInfo()
        sync.stop()
        if let p = p { try store.dropCursor(p["controllerId"]?.str ?? "") }
        vault.delete()
        try store.deleteMeta("needs_decision")
        sync.state = "unpaired"
        sync.profileRejected = false
    }

    public func close() {
        listenerLock.lock(); closed = true; listenerLock.unlock()
        sync.stop()
        store.close()
    }
}
