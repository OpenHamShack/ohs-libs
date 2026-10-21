import Foundation

/// A pair that names a record: (collection, id).
public struct RecordKey: Hashable {
    public let collection: String
    public let id: String
    public init(_ collection: String, _ id: String) { self.collection = collection; self.id = id }
}

/// The local store (SYNC.md §2), a port of python/shack_client store.py,
/// with the same schema and the same three additive columns
/// (`records.extra`, `records.rev`, `ops.rejected_by`), so a store file means
/// the same thing whichever library wrote it.
///
/// Every read and write goes here and returns: nothing a person does waits
/// on the network (§1 rule 1). Writes run in BEGIN IMMEDIATE transactions
/// that also advance the HLC kept in `meta`, so two connections writing one
/// file never mint the same stamp and never lose a write (T18).
///
/// One connection per store behind a recursive lock: the UI and the sync
/// thread take turns, and no network I/O ever happens while it is held (the
/// sync client builds a push, lets go, sends it, and comes back with the
/// answer).
public final class Store: @unchecked Sendable {
    public let path: URL
    public let clock: () -> Int64
    public let storeId: String
    private let db: SQLiteConnection
    private let lock = NSRecursiveLock()

    public static let schemaVersion = 1

    public init(path: URL, clock: @escaping () -> Int64 = Hlc.systemMs) throws {
        self.path = path
        self.clock = clock
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        let conn = try SQLiteConnection(path: path.path)
        for stmt in Store.schema.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespacesAndNewlines) }) where !stmt.isEmpty {
            try conn.exec(stmt)
        }
        try conn.immediate {
            if try conn.query("SELECT value FROM meta WHERE key='store_id'").isEmpty {
                for (k, v) in [("store_id", Store.mintStoreId()), ("schema_version", "\(Store.schemaVersion)"),
                               ("hlc_wall", "0"), ("hlc_count", "0"), ("clock_offset_ms", "0")] {
                    try conn.exec("INSERT INTO meta(key, value) VALUES(?, ?)", k, v)
                }
            }
        }
        db = conn
        storeId = try conn.query("SELECT value FROM meta WHERE key='store_id'")[0][0] as! String
    }

    // MARK: plumbing

    public func withDb<T>(_ block: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try block()
    }

    public func tx<T>(_ block: () throws -> T) throws -> T {
        try withDb { try db.immediate(block) }
    }

    public func query(_ sql: String, _ args: Any?...) throws -> [[Any?]] {
        try withDb { try db.run(sql, args) }
    }

    /// A query whose argument list is built at run time.
    public func queryList(_ sql: String, _ args: [Any?]) throws -> [[Any?]] {
        try withDb { try db.run(sql, args) }
    }

    public func meta(_ key: String) throws -> String? {
        try query("SELECT value FROM meta WHERE key=?", key).first?[0] as? String
    }

    public func setMeta(_ key: String, _ value: Any) throws {
        try withDb { _ = try db.run("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    [key, "\(value)"]) }
    }

    public func deleteMeta(_ key: String) throws {
        try tx { _ = try db.run("DELETE FROM meta WHERE key=?", [key]) }
    }

    /// §3.2 now(): the corrected clock, never below the floor.
    public func now() throws -> Int64 { Hlc.corrected(clock(), Int64(try meta("clock_offset_ms") ?? "0") ?? 0) }

    public func nowIso() throws -> String { Hlc.isoMs(try now()) }

    /// A new HLC for a local edit; call inside a transaction.
    private func stamp() throws -> String {
        let (st, s) = Hlc.local(wall: Int64(try meta("hlc_wall")!)!, count: Int(try meta("hlc_count")!)!, node: storeId, now: try now())
        try setMeta("hlc_wall", st.wall)
        try setMeta("hlc_count", st.count)
        return s
    }

    private func receive(_ stamps: [String]) throws {
        var st = Hlc.State(wall: Int64(try meta("hlc_wall")!)!, count: Int(try meta("hlc_count")!)!)
        let now = try self.now()
        for s in stamps where !s.isEmpty {
            st = try Hlc.receive(wall: st.wall, count: st.count, remote: s, now: now)
        }
        try setMeta("hlc_wall", st.wall)
        try setMeta("hlc_count", st.count)
    }

    /// SQLite's change counter for other connections' commits: how a
    /// process notices another's writes (§2.2).
    public func dataVersion() throws -> Int64 { try query("PRAGMA data_version")[0][0] as? Int64 ?? 0 }

    // MARK: records

    private static let cols = "collection, id, fields, field_hlc, deleted_hlc, created_at, updated_at, origin, extra"

    private static func row(_ r: [Any?]) -> Record {
        var rec: Record = [
            "collection": .string(r[0] as! String), "id": .string(r[1] as! String),
            "fields": (try? JSON.parse(r[2] as! String)) ?? .object([:]),
            "fieldHlc": (try? JSON.parse(r[3] as! String)) ?? .object([:]),
            "deletedHlc": (r[4] as? String).map { .string($0) } ?? .null,
            "createdAt": .string(r[5] as? String ?? ""),
            "origin": (try? JSON.parse(r[7] as! String)) ?? .object([:]),
        ]
        for (k, v) in JSON.parseObject(r[8] as? String ?? "{}") { rec[k] = v }
        return rec
    }

    public func get(_ collection: String, _ id: String) throws -> Record? {
        try query("SELECT \(Store.cols) FROM records WHERE collection=? AND id=?", collection, id).first.map(Store.row)
    }

    public func all(_ collection: String) throws -> [Record] {
        try query("SELECT \(Store.cols) FROM records WHERE collection=? ORDER BY created_at, id", collection).map(Store.row)
    }

    private func put(_ rec: Record, dirty: Bool?, bump: Bool) throws {
        let coll = rec["collection"]!.str, id = rec["id"]!.str
        let cur = try db.query("SELECT dirty, rev FROM records WHERE collection=? AND id=?", coll, id).first
        let d: Int64 = dirty.map { $0 ? 1 : 0 } ?? (cur?[0] as? Int64 ?? 0)
        let rev = (cur?[1] as? Int64 ?? 0) + (bump ? 1 : 0)
        try db.exec("INSERT INTO records(\(Store.cols), dirty, rev) VALUES (?,?,?,?,?,?,?,?,?,?,?) " +
            "ON CONFLICT(collection, id) DO UPDATE SET fields=excluded.fields, field_hlc=excluded.field_hlc, " +
            "deleted_hlc=excluded.deleted_hlc, created_at=excluded.created_at, updated_at=excluded.updated_at, " +
            "origin=excluded.origin, extra=excluded.extra, dirty=excluded.dirty, rev=excluded.rev",
            coll, id, JSON.dump(Merge.fields(rec)), JSON.dump(.object(Merge.fieldHlc(rec).mapValues { .string($0) })),
            Merge.deletedHlc(rec), rec["createdAt"]?.str ?? "", try nowIso(), JSON.dump(rec["origin"]?.obj ?? [:]),
            JSON.dump(Merge.extraOf(rec)), d, rev)
        if coll == "logbook" {
            let f = Merge.fields(rec)
            var band = f["band"]?.str ?? ""
            if band.isEmpty { band = Adif.bandFor(f["freq_hz"]) }
            func text(_ v: JSONValue?) -> String {
                guard let v = v else { return "" }
                if case .string(let s) = v { return s }
                return v.isNull ? "" : JSON.dump(v)
            }
            try db.exec("INSERT OR REPLACE INTO qso_index(id, call, band, mode, start, deleted) VALUES (?,?,?,?,?,?)",
                        id, text(f["call"]).uppercased(), band, text(f["mode"]).uppercased(), f["start"]?.str ?? "",
                        Int64(Merge.isLive(rec) ? 0 : 1))
        }
    }

    private func logOp(_ collection: String, _ id: String, _ kind: String, _ fields: JSONObject, _ stamp: String, _ origin: JSONObject) throws {
        try db.exec("INSERT INTO ops(op_id, collection, id, kind, fields, hlc, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    UUID().uuidString.lowercased(), collection, id, kind, JSON.dump(fields), stamp, JSON.dump(origin), try nowIso())
    }

    /// One local edit: stamp it, merge it into the record, log it in the
    /// outbox, mark the record dirty. Returns the new state.
    @discardableResult
    public func edit(_ collection: String, _ id: String, kind: String, fields: JSONObject, origin: JSONObject) throws -> Record {
        try tx {
            let s = try stamp()
            let rec = Merge.applyEdit(try get(collection, id), collection: collection, id: id, kind: kind, fields: fields,
                                      hlc: s, origin: origin, nowIso: try nowIso())
            try put(rec, dirty: true, bump: true)
            try logOp(collection, id, kind, fields, s, origin)
            return rec
        }
    }

    public struct Edit {
        public let collection: String, id: String, kind: String, fields: JSONObject, origin: JSONObject
        public init(_ collection: String, _ id: String, _ kind: String, _ fields: JSONObject, _ origin: JSONObject) {
            self.collection = collection; self.id = id; self.kind = kind; self.fields = fields; self.origin = origin
        }
    }

    /// Several local edits in one transaction (ADIF import, bulk adds).
    public func editMany(_ edits: [Edit]) throws -> [Record] {
        try tx {
            try edits.map { e in
                let s = try stamp()
                let rec = Merge.applyEdit(try get(e.collection, e.id), collection: e.collection, id: e.id, kind: e.kind,
                                          fields: e.fields, hlc: s, origin: e.origin, nowIso: try nowIso())
                try put(rec, dirty: true, bump: true)
                try logOp(e.collection, e.id, e.kind, e.fields, s, e.origin)
                return rec
            }
        }
    }

    /// Records from a pull: the receive rule for every stamp, then the
    /// merge. `replace` names records to take as they are rather than merge
    /// (a profile edit the controller refused: §4.2). Returns what changed.
    public func mergeRemote(_ recs: [Record], replace: Set<RecordKey> = []) throws -> [RecordKey] {
        var changed: [RecordKey] = []
        try tx {
            for raw in recs {
                var rec = raw
                rec.removeValue(forKey: "seq")
                let key = RecordKey(rec["collection"]?.str ?? "", rec["id"]?.str ?? "")
                try receive(Merge.stamps(rec))
                let cur = try get(key.collection, key.id)
                let new: Record
                let dirty: Bool
                if cur == nil || replace.contains(key) {
                    new = rec
                    dirty = false
                } else {
                    new = Merge.merge(cur, rec)
                    // Dirty while the local copy holds something the
                    // controller lacks; clean once the merge is its state.
                    dirty = !Merge.same(new, rec)
                }
                try put(new, dirty: dirty, bump: dirty)
                if cur == nil || !Merge.same(new, cur) { changed.append(key) }
            }
        }
        return changed
    }

    // MARK: outbox

    public func dirty(limit: Int = 500) throws -> [(Record, Int64)] {
        try query("SELECT \(Store.cols), rev FROM records WHERE dirty=1 ORDER BY rowid LIMIT ?", Int64(limit)).map {
            (Store.row(Array($0.dropLast())), $0.last! as? Int64 ?? 0)
        }
    }

    public func dirtyKeys() throws -> Set<RecordKey> {
        Set(try query("SELECT collection, id FROM records WHERE dirty=1").map { RecordKey($0[0] as! String, $0[1] as! String) })
    }

    public func pendingCount() throws -> Int { Int(try query("SELECT COUNT(*) FROM records WHERE dirty=1")[0][0] as? Int64 ?? 0) }

    public func unackedOps(_ controllerId: String, limit: Int = 2000) throws -> [JSONObject] {
        // The JSON lists hold quoted ids, so instr() on the quoted id is an
        // exact membership test that keeps a 10,000-op outbox in SQLite.
        let q = JSON.dump(.string(controllerId))
        let rows = try query("SELECT op_id, collection, id, kind, fields, hlc, origin, created_at, acked_by, rejected_by " +
                             "FROM ops WHERE instr(acked_by, ?) = 0 AND instr(rejected_by, ?) = 0 ORDER BY hlc", q, q)
        var out: [JSONObject] = []
        for r in rows {
            let acked = (try? JSON.parse(r[8] as! String))?.arr ?? []
            let rejected = (try? JSON.parse(r[9] as! String))?.arr ?? []
            if acked.contains(.string(controllerId)) || rejected.contains(.string(controllerId)) { continue }
            out.append(["opId": .string(r[0] as! String), "collection": .string(r[1] as! String), "id": .string(r[2] as! String),
                        "kind": .string(r[3] as! String), "fields": (try? JSON.parse(r[4] as! String)) ?? .object([:]),
                        "hlc": .string(r[5] as! String), "origin": (try? JSON.parse(r[6] as! String)) ?? .object([:]),
                        "createdAt": .string(r[7] as? String ?? "")])
            if out.count >= limit { break }
        }
        return out
    }

    /// Clear dirty for what the answer confirmed (§6.1), only where the
    /// record has not changed since the push was built.
    public func ack(_ controllerId: String, records: [(RecordKey, Int64)], opIds: [String], rejectedOps: [String] = []) throws {
        try tx {
            for (key, rev) in records {
                try db.exec("UPDATE records SET dirty=0 WHERE collection=? AND id=? AND rev=?", key.collection, key.id, rev)
            }
            for (oid, column) in opIds.map({ ($0, "acked_by") }) + rejectedOps.map({ ($0, "rejected_by") }) {
                guard let row = try db.query("SELECT \(column) FROM ops WHERE op_id=?", oid).first else { continue }
                var ids = (try? JSON.parse(row[0] as! String))?.arr ?? []
                if !ids.contains(.string(controllerId)) {
                    ids.append(.string(controllerId))
                    try db.exec("UPDATE ops SET \(column)=? WHERE op_id=?", JSON.dump(.array(ids)), oid)
                }
            }
        }
    }

    public func markAllDirty() throws { try tx { try db.exec("UPDATE records SET dirty=1") } }

    /// After `clock_ahead` (§3.3): reset the HLC to the corrected clock and
    /// re-stamp the unacknowledged ops in their original order, moving each
    /// record's field stamps with them, so no future stamp ever reaches the
    /// controller. Returns how many stamps moved.
    @discardableResult
    public func restampUnacked(_ controllerId: String) throws -> Int {
        try tx {
            let now = try self.now()
            try setMeta("hlc_wall", now)
            try setMeta("hlc_count", 0)
            let ops = try unackedOps(controllerId, limit: Int.max)
            let future = now + Hlc.maxAheadMs
            // Field stamps from edits whose ops are already acknowledged can
            // still be in the future (made offline under the wrong clock and
            // pushed nowhere yet): every local stamp past the limit moves.
            var remap: [String: String] = [:]
            for op in ops {
                let new = try stamp()
                remap[op["hlc"]!.str] = new
                try db.exec("UPDATE ops SET hlc=? WHERE op_id=?", new, op["opId"]!.str)
            }
            func mine(_ s: String) -> Bool { s.hasSuffix(storeId) && Hlc.wallOf(s) > future }
            func moved(_ s: String) throws -> String {
                if let m = remap[s] { return m }
                let n = try stamp()
                remap[s] = n
                return n
            }
            for var rec in try all("logbook") + all("profile") {
                var changed = false
                var fh = Merge.fieldHlc(rec)
                for (f, s) in fh where remap[s] != nil || mine(s) {
                    fh[f] = try moved(s)
                    changed = true
                }
                var d = Merge.deletedHlc(rec)
                if let x = d, remap[x] != nil || mine(x) {
                    d = try moved(x)
                    changed = true
                }
                if changed {
                    rec["fieldHlc"] = .object(fh.mapValues { .string($0) })
                    rec["deletedHlc"] = d.map { .string($0) } ?? .null
                    try put(rec, dirty: true, bump: true)
                }
            }
            return remap.count
        }
    }

    // MARK: cursors and lease

    public func cursor(_ controllerId: String) throws -> (epoch: String, seq: Int64)? {
        try query("SELECT epoch, seq FROM cursors WHERE controller_id=?", controllerId).first.map { ($0[0] as! String, $0[1] as? Int64 ?? 0) }
    }

    public func setCursor(_ controllerId: String, epoch: String, seq: Int64) throws {
        try tx {
            try db.exec("INSERT INTO cursors(controller_id, epoch, seq, last_sync_at) VALUES (?,?,?,?) " +
                        "ON CONFLICT(controller_id) DO UPDATE SET epoch=excluded.epoch, seq=excluded.seq, " +
                        "last_sync_at=excluded.last_sync_at", controllerId, epoch, seq, try nowIso())
        }
    }

    public func dropCursor(_ controllerId: String) throws {
        try tx { try db.exec("DELETE FROM cursors WHERE controller_id=?", controllerId) }
    }

    /// Only one process syncs a shared store at a time (§2.2). Wall-clock
    /// seconds like the Python store's, so they respect each other's lease.
    public func takeLease(_ holder: String, seconds: Double = 30) throws -> Bool {
        try tx {
            let now = Date().timeIntervalSince1970
            if let row = try db.query("SELECT holder, expires_at FROM sync_lease WHERE one=1").first,
               row[0] as? String != holder, (row[1] as? Double ?? Double(row[1] as? Int64 ?? 0)) > now {
                return false
            }
            try db.exec("INSERT INTO sync_lease(one, holder, expires_at) VALUES (1,?,?) ON CONFLICT(one) " +
                        "DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at", holder, now + seconds)
            return true
        }
    }

    public func releaseLease(_ holder: String) throws {
        try tx { try db.exec("DELETE FROM sync_lease WHERE one=1 AND holder=?", holder) }
    }

    // MARK: whole-store operations

    /// Forget every record and op, keep the store's identity and clock:
    /// "use the controller's log" after archiving (§7).
    public func wipeReplica() throws {
        try tx { for t in ["records", "ops", "cursors", "qso_index"] { try db.exec("DELETE FROM \(t)") } }
    }

    public func archive(to dest: URL) throws { try withDb { try db.backup(to: dest.path) } }

    public func close() { withDb { db.close() } }

    // MARK: ids

    private static let b32 = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")

    public static func mintStoreId() -> String {
        var g = SystemRandomNumberGenerator()
        return "st_" + String((0..<12).map { _ in b32[Int.random(in: 0..<32, using: &g)] })
    }

    /// An RFC 9562 version 7 UUID (time-ordered, so the logbook's index
    /// stays in insertion order).
    public static func uuid7(_ nowMs: Int64 = Hlc.systemMs()) -> String {
        var b = [UInt8](repeating: 0, count: 16)
        var g = SystemRandomNumberGenerator()
        for i in 6..<16 { b[i] = UInt8.random(in: 0...255, using: &g) }
        let ms = UInt64(bitPattern: nowMs) & 0xFFFF_FFFF_FFFF
        for i in 0..<6 { b[i] = UInt8((ms >> UInt64(8 * (5 - i))) & 0xFF) }
        b[6] = 0x70 | (b[6] & 0x0F)
        b[8] = 0x80 | (b[8] & 0x3F)
        let hex = b.map { String(format: "%02x", $0) }.joined()
        let c = Array(hex)
        return "\(String(c[0..<8]))-\(String(c[8..<12]))-\(String(c[12..<16]))-\(String(c[16..<20]))-\(String(c[20..<32]))"
    }

    private static let schema = """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
            collection  TEXT NOT NULL,
            id          TEXT NOT NULL,
            fields      TEXT NOT NULL,
            field_hlc   TEXT NOT NULL,
            deleted_hlc TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            origin      TEXT NOT NULL,
            dirty       INTEGER NOT NULL DEFAULT 0,
            extra       TEXT NOT NULL DEFAULT '{}',
            rev         INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (collection, id));
        CREATE TABLE IF NOT EXISTS ops (
            op_id       TEXT PRIMARY KEY,
            collection  TEXT NOT NULL,
            id          TEXT NOT NULL,
            kind        TEXT NOT NULL,
            fields      TEXT NOT NULL,
            hlc         TEXT NOT NULL,
            origin      TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            acked_by    TEXT NOT NULL DEFAULT '[]',
            rejected_by TEXT NOT NULL DEFAULT '[]');
        CREATE INDEX IF NOT EXISTS ops_by_record ON ops(collection, id, hlc);
        CREATE TABLE IF NOT EXISTS cursors (
            controller_id TEXT PRIMARY KEY,
            epoch         TEXT NOT NULL,
            seq           INTEGER NOT NULL,
            last_sync_at  TEXT);
        CREATE TABLE IF NOT EXISTS qso_index (
            id TEXT PRIMARY KEY, call TEXT, band TEXT, mode TEXT, start TEXT, deleted INTEGER);
        CREATE INDEX IF NOT EXISTS qso_by_call  ON qso_index(call, band, mode);
        CREATE INDEX IF NOT EXISTS qso_by_start ON qso_index(start);
        CREATE TABLE IF NOT EXISTS sync_lease (one INTEGER PRIMARY KEY CHECK (one = 1), holder TEXT, expires_at REAL)
        """
}
