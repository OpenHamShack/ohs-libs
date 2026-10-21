import Foundation
@testable import ShackClient

/// A reference controller for the tests: a port of python/shack_client
/// controller.py (SyncService: push, pull, epochs, sequence numbers,
/// history) and testing.py (the routes a client uses, fault knobs), so the
/// Swift client runs the shared vectors against the same server rules the
/// Python and Kotlin clients are tested against. Test code: the real
/// controller is the Dock360 edition, which imports the Python one.
final class SyncService {
    let path: String
    let controllerId: String
    let nowMs: () -> Int64
    let node: String
    private let db: SQLiteConnection
    private let lock = NSRecursiveLock()
    static let maxBatch = 500
    private static let cols = "collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra, seq"

    init(path: URL, controllerId: String, nowMs: @escaping () -> Int64) throws {
        self.path = path.path
        self.controllerId = controllerId
        self.nowMs = nowMs
        node = "ctl_" + String(controllerId.replacingOccurrences(of: "-", with: "").prefix(12))
        db = try SQLiteConnection(path: path.path)
        for s in SyncService.schema.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespacesAndNewlines) }) where !s.isEmpty {
            try db.exec(s)
        }
        if try meta("epoch") == nil {
            try setMeta("epoch", SyncService.newEpoch())
            try setMeta("latest_seq", "0")
            try setMeta("hlc_wall", "0")
            try setMeta("hlc_count", "0")
        }
    }

    private func locked<T>(_ f: () throws -> T) rethrows -> T { lock.lock(); defer { lock.unlock() }; return try f() }

    private func meta(_ k: String) throws -> String? { try db.query("SELECT value FROM meta WHERE key=?", k).first?[0] as? String }
    private func setMeta(_ k: String, _ v: Any) throws {
        try db.exec("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, "\(v)")
    }

    static func newEpoch() -> String { "e_" + String(UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "").prefix(8)) }

    var epoch: String { (try? meta("epoch")) ?? nil ?? "" }
    var latestSeq: Int64 { Int64((try? meta("latest_seq")) ?? nil ?? "0") ?? 0 }

    func info() -> JSONObject {
        ["services": ["profile": 1, "logbook": 1, "sync": 1], "controllerId": .string(controllerId), "time": .string(Hlc.isoMs(nowMs()))]
    }

    private func row(_ r: [Any?]) -> Record {
        var rec: Record = ["collection": .string(r[0] as! String), "id": .string(r[1] as! String),
                           "fields": (try? JSON.parse(r[2] as! String)) ?? [:], "fieldHlc": (try? JSON.parse(r[3] as! String)) ?? [:],
                           "deletedHlc": (r[4] as? String).map { .string($0) } ?? .null, "createdAt": .string(r[5] as? String ?? ""),
                           "origin": (try? JSON.parse(r[7] as! String)) ?? [:]]
        for (k, v) in JSON.parseObject(r[8] as? String ?? "{}") { rec[k] = v }
        rec["seq"] = .int(r[9] as? Int64 ?? 0)
        return rec
    }

    func get(_ collection: String, _ id: String) throws -> Record? {
        try db.query("SELECT \(SyncService.cols) FROM records WHERE collection=? AND id=?", collection, id).first.map(row)
    }

    func all(_ collection: String) -> [Record] {
        locked { ((try? db.query("SELECT \(SyncService.cols) FROM records WHERE collection=? ORDER BY seq", collection)) ?? []).map(row) }
    }

    private func store(_ rec: Record, _ seq: Int64) throws {
        try db.exec("INSERT INTO records(collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra, seq) " +
                    "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collection, id) DO UPDATE SET fields=excluded.fields, " +
                    "field_hlc=excluded.field_hlc, deleted_hlc=excluded.deleted_hlc, created_at=excluded.created_at, " +
                    "updated_at=excluded.updated_at, origin=excluded.origin, extra=excluded.extra, seq=excluded.seq",
                    rec["collection"]!.str, rec["id"]!.str, JSON.dump(Merge.fields(rec)),
                    JSON.dump(.object(Merge.fieldHlc(rec).mapValues { .string($0) })), Merge.deletedHlc(rec),
                    rec["createdAt"]?.str ?? "", Hlc.isoMs(nowMs()), JSON.dump(rec["origin"]?.obj ?? [:]), JSON.dump(Merge.extraOf(rec)), seq)
    }

    private func noSeq(_ r: Record?) -> Record? { r.map { var x = $0; x.removeValue(forKey: "seq"); return x } }

    func push(_ body: JSONObject, canEditProfile: Bool = true) throws -> (Int, JSONObject) {
        try locked {
            let recs = (body["records"]?.arr ?? []).map(\.obj)
            let ops = (body["ops"]?.arr ?? []).map(\.obj)
            if recs.count > SyncService.maxBatch || ops.count > 4 * SyncService.maxBatch {
                return (400, ["error": "A sync push carries at most 500 records.", "code": "bad_request"])
            }
            let now = nowMs(), limit = now + Hlc.maxAheadMs
            var results: [JSONValue] = []
            var rejected = Set<RecordKey>()
            try db.immediate {
                for rec in recs {
                    let k = RecordKey(rec["collection"]?.str ?? "", rec["id"]?.str ?? "")
                    func reject(_ code: String, _ extra: JSONObject) {
                        var r: JSONObject = ["collection": .string(k.collection), "id": .string(k.id), "status": "rejected", "code": .string(code)]
                        for (a, b) in extra { r[a] = b }
                        results.append(.object(r))
                        rejected.insert(k)
                    }
                    let bad = invalid(rec)
                    if !bad.isEmpty { reject("bad_request", ["error": .string(bad)]); continue }
                    let stamps = Merge.stamps(rec)
                    if stamps.contains(where: { Hlc.wallOf($0) > limit }) { reject("clock_ahead", ["serverTime": .string(Hlc.isoMs(now))]); continue }
                    if k.collection == "profile" && !canEditProfile {
                        reject("level_too_low", ["error": "Changing the station profile needs the Transmit level."]); continue
                    }
                    try db.exec("SAVEPOINT rec")
                    do {
                        let stored = try get(k.collection, k.id)
                        let incoming = noSeq(rec)!
                        let merged = Merge.merge(noSeq(stored), incoming)
                        if let s = stored, Merge.same(merged, s) {
                            results.append(["collection": .string(k.collection), "id": .string(k.id), "status": "unchanged",
                                            "seq": s["seq"]!, "lost": []])
                        } else {
                            let seq = latestSeq + 1
                            try setMeta("latest_seq", seq)
                            try store(merged, seq)
                            results.append(["collection": .string(k.collection), "id": .string(k.id), "status": "merged", "seq": .int(seq),
                                            "lost": .array(lostAgainst(incoming, stored).map { .string($0) })])
                        }
                        try observe(stamps)
                        try db.exec("RELEASE rec")
                    } catch {
                        try db.exec("ROLLBACK TO rec")
                        try db.exec("RELEASE rec")
                        throw error
                    }
                }
            }
            var accepted: [JSONValue] = [], duplicate: [JSONValue] = [], ignored: [JSONValue] = []
            try db.immediate {
                for op in ops {
                    let k = RecordKey(op["collection"]?.str ?? "", op["id"]?.str ?? "")
                    let oid = op["opId"]?.str ?? ""
                    if oid.isEmpty || rejected.contains(k) || Hlc.wallOf(op["hlc"]?.stringValue) > limit { continue }
                    if try !db.query("SELECT 1 FROM ops WHERE op_id=?", oid).isEmpty { duplicate.append(.string(oid)); continue }
                    guard let state = try get(k.collection, k.id) else { ignored.append(.string(oid)); continue }
                    try db.exec("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at, received_at, received_seq, lost) " +
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", oid, k.collection, k.id, (op["kind"]?.str).flatMap { $0.isEmpty ? nil : $0 } ?? "update",
                                JSON.dump(op["fields"]?.obj ?? [:]), op["hlc"]?.str ?? "", JSON.dump(op["origin"]?.obj ?? [:]),
                                op["createdAt"]?.str ?? "", Hlc.isoMs(now), state["seq"]?.int64Value ?? 0,
                                JSON.dump(.array(Merge.lostFields(op: op, state: state).map { .string($0) })))
                    accepted.append(.string(oid))
                }
            }
            return (200, ["controllerId": .string(controllerId), "epoch": .string(epoch), "records": .array(results),
                          "ops": ["accepted": .array(accepted), "duplicate": .array(duplicate), "ignored": .array(ignored)]])
        }
    }

    private func invalid(_ rec: JSONObject) -> String {
        guard ["logbook", "profile"].contains(rec["collection"]?.str ?? ""), !(rec["id"]?.str ?? "").isEmpty else {
            return "A sync record needs a collection (logbook or profile) and an id."
        }
        guard rec["fields"]?.objectValue != nil, rec["fieldHlc"]?.objectValue != nil else { return "A sync record needs fields and fieldHlc objects." }
        for s in Merge.stamps(rec) where (try? Hlc.parse(s)) == nil { return "A sync record carries a stamp that is not a hybrid logical clock." }
        return ""
    }

    private func lostAgainst(_ incoming: Record, _ stored: Record?) -> [String] {
        guard let stored = stored else { return [] }
        let sh = Merge.fieldHlc(stored)
        return Merge.fieldHlc(incoming).filter { f, h in
            guard let s = sh[f] else { return false }
            return s > h && Hlc.nodeOf(s) != Hlc.nodeOf(h)
        }.keys.sorted()
    }

    private func observe(_ stamps: [String]) throws {
        var st = Hlc.State(wall: Int64(try meta("hlc_wall")!)!, count: Int(try meta("hlc_count")!)!)
        let now = Hlc.corrected(nowMs(), 0)
        for s in stamps { st = try Hlc.receive(wall: st.wall, count: st.count, remote: s, now: now) }
        try setMeta("hlc_wall", st.wall)
        try setMeta("hlc_count", st.count)
    }

    func pull(since: Int64, epoch e: String?, limit: Int, collections: [String]?) throws -> (Int, JSONObject) {
        try locked {
            let latest = latestSeq
            if (e != nil && e != epoch) || since > latest {
                return (409, ["error": "This controller's sync history was restored from a backup; syncing again from the start.",
                              "code": "epoch_changed", "epoch": .string(epoch)])
            }
            let lim = max(1, min(limit, SyncService.maxBatch))
            var q = "SELECT \(SyncService.cols) FROM records WHERE seq > ?"
            var args: [Any?] = [since]
            if let cs = collections, !cs.isEmpty {
                q += " AND collection IN (" + cs.map { _ in "?" }.joined(separator: ",") + ")"
                args += cs.map { $0 as Any? }
            }
            args.append(Int64(lim + 1))
            var rows = try db.run(q + " ORDER BY seq LIMIT ?", args)
            let more = rows.count > lim
            rows = Array(rows.prefix(lim))
            let recs = rows.map(row)
            let next: Int64 = recs.last?["seq"]?.int64Value ?? (more ? since : latest)
            return (200, ["controllerId": .string(controllerId), "epoch": .string(epoch), "latestSeq": .int(latest),
                          "records": .array(recs.map { .object($0) }), "next": .int(next), "more": .bool(more)])
        }
    }

    func history(_ collection: String, _ id: String) -> [JSONObject] {
        locked {
            ((try? db.query("SELECT op_id, hlc, kind, fields, origin, received_at, lost FROM ops WHERE collection=? AND id=? ORDER BY hlc",
                            collection, id)) ?? []).map { r in
                ["opId": .string(r[0] as! String), "hlc": .string(r[1] as! String), "kind": .string(r[2] as! String),
                 "fields": (try? JSON.parse(r[3] as! String)) ?? [:], "origin": (try? JSON.parse(r[4] as! String)) ?? [:],
                 "receivedAt": .string(r[5] as? String ?? ""), "lost": (try? JSON.parse(r[6] as! String)) ?? []]
            }
        }
    }

    /// The controller's own write, stamped with its own HLC (§10).
    @discardableResult
    func edit(_ collection: String, _ id: String, kind: String, fields: JSONObject, origin: JSONObject) throws -> Record {
        try locked {
            try db.immediate {
                let (st, stamp) = Hlc.local(wall: Int64(try meta("hlc_wall")!)!, count: Int(try meta("hlc_count")!)!, node: node,
                                            now: Hlc.corrected(nowMs(), 0))
                try setMeta("hlc_wall", st.wall)
                try setMeta("hlc_count", st.count)
                let rec = Merge.applyEdit(noSeq(try get(collection, id)), collection: collection, id: id, kind: kind, fields: fields,
                                          hlc: stamp, origin: origin, nowIso: Hlc.isoMs(nowMs()))
                let seq = latestSeq + 1
                try setMeta("latest_seq", seq)
                try store(rec, seq)
                try db.exec("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at, received_at, received_seq, lost) " +
                            "VALUES (?,?,?,?,?,?,?,?,?,?,'[]')", UUID().uuidString, collection, id, kind, JSON.dump(fields), stamp,
                            JSON.dump(origin), Hlc.isoMs(nowMs()), Hlc.isoMs(nowMs()), seq)
                return rec
            }
        }
    }

    func snapshot() throws -> Data {
        try locked {
            let tmp = path + ".snap-\(UUID().uuidString)"
            defer { try? FileManager.default.removeItem(atPath: tmp) }
            try db.backup(to: tmp)
            return try Data(contentsOf: URL(fileURLWithPath: tmp))
        }
    }

    /// Restore a snapshot and mint a new epoch, as the platform's restore
    /// path must (§6.2).
    func restore(_ snapshot: Data) throws {
        try locked {
            let tmp = path + ".restore-\(UUID().uuidString)"
            defer { try? FileManager.default.removeItem(atPath: tmp) }
            try snapshot.write(to: URL(fileURLWithPath: tmp))
            try db.restore(from: tmp)
            try setMeta("epoch", SyncService.newEpoch())
        }
    }

    func close() { db.close() }

    private static let schema = """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
            collection TEXT NOT NULL, id TEXT NOT NULL, fields TEXT NOT NULL, field_hlc TEXT NOT NULL,
            deleted_hlc TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, origin TEXT NOT NULL,
            extra TEXT NOT NULL DEFAULT '{}', seq INTEGER NOT NULL,
            PRIMARY KEY (collection, id));
        CREATE INDEX IF NOT EXISTS records_by_seq ON records(seq);
        CREATE TABLE IF NOT EXISTS ops (
            op_id TEXT PRIMARY KEY, collection TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
            fields TEXT NOT NULL, hlc TEXT NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL, received_seq INTEGER NOT NULL, lost TEXT NOT NULL DEFAULT '[]');
        CREATE INDEX IF NOT EXISTS ops_by_record ON ops(collection, id, hlc)
        """
}

/// The test transport's "the request went out, the answer never came".
struct Dropped: Error {}

final class ReferenceController {
    let name: String
    let controllerId: String
    let nowMs: () -> Int64
    let sync: SyncService
    let users: [String: JSONObject]
    private let lock = NSRecursiveLock()
    var devices: [String: JSONObject] = [:]
    private var tvPairs: [String: JSONObject] = [:]
    var dropNextPushAnswer = false
    var offline = false
    private(set) var requests: [(String, String)] = []
    var server: LoopbackHTTPServer?

    init(path: URL, name: String = "GM4BRI", controllerId: String = UUID().uuidString.lowercased(), nowMs: @escaping () -> Int64 = Hlc.systemMs,
         users: [String: JSONObject]? = nil) throws {
        self.name = name
        self.controllerId = controllerId
        self.nowMs = nowMs
        sync = try SyncService(path: path, controllerId: controllerId, nowMs: nowMs)
        self.users = users ?? ["brian": ["password": "pw", "transmit": true]]
    }

    private func locked<T>(_ f: () throws -> T) rethrows -> T { lock.lock(); defer { lock.unlock() }; return try f() }

    func requestCount() -> Int { locked { requests.count } }
    func requestPaths() -> [String] { locked { requests.map(\.1) } }

    func handle(_ method: String, _ pathAndQuery: String, headers: [String: String], body: JSONObject?) throws -> (Int, JSONObject) {
        locked { requests.append((method, pathAndQuery)) }
        let parts = pathAndQuery.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
        var route = parts[0]
        while route.hasPrefix("/") { route.removeFirst() }
        var q: [String: String] = [:]
        if parts.count > 1 {
            for kv in parts[1].split(separator: "&") {
                let p = kv.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
                q[p[0].removingPercentEncoding ?? p[0]] = (p.count > 1 ? p[1] : "").removingPercentEncoding ?? ""
            }
        }
        let b = body ?? [:]
        switch (method, route) {
        case ("GET", ".well-known/openhamshack"): return (200, wellKnown())
        case ("POST", "api/shack/token"): return token(b)
        case ("POST", "api/shack/pair/start"): return tvStart(b)
        case ("POST", "api/shack/pair/poll"): return tvPoll(b)
        default: break
        }
        let auth = headers.first { $0.key.lowercased() == "authorization" }?.value ?? ""
        guard auth.hasPrefix("Bearer "), let dev = locked({ devices[String(auth.dropFirst(7))] }) else {
            return (401, ["error": "Sign in to this controller first.", "code": "credentials_required"])
        }
        if dev["revoked"]?.boolValue == true { return (401, ["error": .string("This device has been removed from \(name)."), "code": "token_revoked"]) }
        switch (method, route) {
        case ("GET", "api/shack/v1/"): return (200, sync.info())
        case ("POST", "api/shack/v1/sync/push"): return try sync.push(b, canEditProfile: dev["transmit"]?.boolValue == true)
        case ("GET", "api/shack/v1/sync/pull"):
            let cols = (q["collections"] ?? "").split(separator: ",").map(String.init).filter { !$0.isEmpty }
            return try sync.pull(since: Int64(q["since"] ?? "0") ?? 0, epoch: (q["epoch"] ?? "").isEmpty ? nil : q["epoch"],
                                 limit: Int(q["limit"] ?? "500") ?? 500, collections: cols.isEmpty ? nil : cols)
        case ("POST", "api/shack/v1/pair/approve"): return tvApprove(b, dev)
        default: return (404, ["error": "There is nothing at that address on this controller.", "code": "not_found"])
        }
    }

    func wellKnown() -> JSONObject {
        ["openhamshack": 1, "contract": "1.0", "controllerId": .string(controllerId), "name": .string(name), "version": "2.0.0-test",
         "time": .string(Hlc.isoMs(nowMs())), "station": ["callsign": .string(name), "locator": "", "country": ""],
         "auth": ["token": "api/shack/token", "pair": "api/shack/pair/start", "totp": "per-user", "anonymousView": []],
         "services": ["base": "api/shack/v1/", "profile": 1, "logbook": 1, "sync": 1],
         "bandplan": ["dataVersion": "", "schema": 1], "apps": []]
    }

    private static func random(_ n: Int) -> String {
        var g = SystemRandomNumberGenerator()
        let a = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        return String((0..<n).map { _ in a[Int.random(in: 0..<a.count, using: &g)] })
    }

    private func mint(_ user: String, _ name: String, _ kind: String, _ clientId: String, _ maxLevel: String) -> (String, JSONObject) {
        locked {
            for (t, d) in devices where d["clientId"]?.str == clientId && d["user"]?.str == user && d["revoked"]?.boolValue != true {
                devices[t]!["revoked"] = true
            }
            let token = "shackdev_" + ReferenceController.random(43)
            let dev: JSONObject = ["id": .string("dev_" + ReferenceController.random(12).uppercased()), "user": .string(user), "name": .string(name),
                                   "kind": .string(kind), "clientId": .string(clientId), "maxLevel": .string(maxLevel),
                                   "transmit": .bool(maxLevel == "transmit" && users[user]?["transmit"]?.boolValue == true)]
            devices[token] = dev
            return (token, dev)
        }
    }

    private func token(_ b: JSONObject) -> (Int, JSONObject) {
        let user = users[b["username"]?.str ?? ""]
        if b["deviceKind"]?.str == "tv" { return (400, ["error": "A TV pairs with a code, not a password.", "code": "use_tv_pairing"]) }
        guard let u = user, u["password"] == b["password"] else {
            return (401, ["error": "The username or password is wrong.", "code": "bad_credentials"])
        }
        if let totp = u["totp"], b["totp"] != totp { return (401, ["error": "Enter the code from your authenticator app.", "code": "totp_required"]) }
        let kind = b["deviceKind"]?.str ?? ""
        let (token, dev) = mint(b["username"]!.str, b["deviceName"]?.str ?? "", kind.isEmpty ? "desktop" : kind, b["clientId"]?.str ?? "", "transmit")
        return (201, ["token": .string(token), "deviceId": dev["id"]!, "user": b["username"]!, "admin": false, "maxLevel": "transmit",
                      "controllerId": .string(controllerId)])
    }

    private func tvStart(_ b: JSONObject) -> (Int, JSONObject) {
        locked {
            let pid = "pr_" + ReferenceController.random(6).uppercased()
            let code = String(format: "%06d", Int.random(in: 0..<1_000_000))
            let p: JSONObject = ["pairId": .string(pid), "pairSecret": .string(ReferenceController.random(43)), "code": .string(code),
                                 "state": "pending", "body": .object(b)]
            tvPairs[pid] = p
            return (200, ["pairId": .string(pid), "pairSecret": p["pairSecret"]!, "code": .string(code), "expiresIn": 300, "pollEvery": 2])
        }
    }

    private func tvApprove(_ b: JSONObject, _ dev: JSONObject) -> (Int, JSONObject) {
        locked {
            guard let entry = tvPairs.first(where: { $0.value["code"] == b["code"] && $0.value["state"]?.str == "pending" }) else {
                return (404, ["error": "No TV is showing that code.", "code": "not_found"])
            }
            let pid = entry.key, p = entry.value
            if b["deny"]?.boolValue == true { tvPairs[pid]!["state"] = "denied"; return (200, ["approved": false]) }
            let level = b["maxLevel"]?.str == "control" ? "control" : "view"
            let body = p["body"]?.obj ?? [:]
            let (token, tv) = mint(dev["user"]?.str ?? "", body["deviceName"]?.str ?? "", "tv", body["clientId"]?.str ?? "", level)
            tvPairs[pid]!["state"] = "approved"
            tvPairs[pid]!["token"] = .string(token)
            tvPairs[pid]!["device"] = .object(tv)
            return (200, ["approved": true])
        }
    }

    private func tvPoll(_ b: JSONObject) -> (Int, JSONObject) {
        locked {
            guard let p = tvPairs[b["pairId"]?.str ?? ""], p["pairSecret"] == b["pairSecret"] else {
                return (410, ["error": "That pairing has expired.", "code": "pair_expired"])
            }
            if p["state"]?.str == "approved" {
                tvPairs.removeValue(forKey: p["pairId"]!.str)
                let d = p["device"]?.obj ?? [:]
                return (200, ["state": "approved", "token": p["token"]!, "deviceId": d["id"]!, "user": d["user"]!, "maxLevel": d["maxLevel"]!])
            }
            return (200, ["state": p["state"]!])
        }
    }

    func records(_ collection: String = "logbook") -> [String: Record] {
        Dictionary(uniqueKeysWithValues: sync.all(collection).map { ($0["id"]!.str, $0) })
    }

    /// Start on a loopback port; the base URL.
    func serve() throws -> String {
        let s = try LoopbackHTTPServer { [unowned self] method, path, headers, body in
            if self.offline { return nil }
            let parsed = body.isEmpty ? nil : JSON.parseObject(String(decoding: body, as: UTF8.self))
            let (status, ans) = (try? self.handle(method, path, headers: headers, body: parsed)) ?? (500, ["error": "fault"])
            if method == "POST" && path.hasPrefix("/api/shack/v1/sync/push") && self.dropNextPushAnswer {
                self.dropNextPushAnswer = false
                return nil   // applied, and the answer is lost
            }
            return (status, Data(JSON.dump(.object(ans)).utf8))
        }
        server = s
        return "http://127.0.0.1:\(s.port)/"
    }

    func close() {
        server?.stop()
        sync.close()
    }
}

/// In-process controllers by base URL, and the transport factory a Station
/// takes to reach them without sockets (testing.py Network).
final class Network {
    var controllers: [String: ReferenceController] = [:]

    func factory(counting: (() -> Void)? = nil) -> TransportFactory {
        { [unowned self] base, token in DirectTransport(net: self, baseURL: base, token: token, counting: counting) }
    }
}

struct DirectTransport: Transport {
    let net: Network
    let baseURL: String
    let token: String?
    let counting: (() -> Void)?

    func request(_ method: String, _ path: String, body: JSONObject?, query: [(String, String?)]?) throws -> Response {
        counting?()
        guard let ctl = net.controllers[baseURL], !ctl.offline else { throw Offline("\(baseURL) does not answer") }
        var p = path
        while p.hasPrefix("/") { p.removeFirst() }
        let full = URLs.join("http://x/", p, query: query)
        let rel = String(full.dropFirst("http://x/".count))
        // A JSON round trip, so nothing shares state with the controller
        // that a real wire would not.
        let (status, ans) = try ctl.handle(method, rel, headers: token.map { ["Authorization": "Bearer \($0)"] } ?? [:],
                                           body: body.map { JSON.parseObject(JSON.dump(.object($0))) })
        if method == "POST" && rel.hasPrefix("api/shack/v1/sync/push") && ctl.dropNextPushAnswer {
            ctl.dropNextPushAnswer = false
            throw Offline("the answer to the push was lost")
        }
        return Response(status: status, body: JSON.parseObject(JSON.dump(.object(ans))))
    }
}
