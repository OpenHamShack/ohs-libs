import Foundation
import SQLite3

/// SQLite's destructor flag for "copy the bytes now": the C macro
/// SQLITE_TRANSIENT is not visible to Swift.
private let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

public struct SQLiteError: Error, CustomStringConvertible {
    public let code: Int32
    public let description: String
}

/// One connection to an SQLite file through the system library (the
/// SQLite3 module on every Apple platform: no bundled copy). Values bound
/// and returned are String, Int64, Double, Data and nil.
///
/// Not thread-safe on its own: the store serialises every use behind its
/// lock, as the Kotlin store does around its connection.
public final class SQLiteConnection {
    private var db: OpaquePointer?
    private var depth = 0

    public init(path: String) throws {
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        let rc = sqlite3_open_v2(path, &db, flags, nil)
        guard rc == SQLITE_OK else {
            let msg = db.map { String(cString: sqlite3_errmsg($0)) } ?? "cannot open \(path)"
            sqlite3_close(db)
            throw SQLiteError(code: rc, description: msg)
        }
        // busy_timeout before WAL: switching the journal mode needs the lock
        // another connection may be holding.
        sqlite3_busy_timeout(db, 5000)
        _ = try query("PRAGMA journal_mode=WAL")
    }

    deinit { close() }

    public func close() {
        if db != nil {
            sqlite3_close_v2(db)
            db = nil
        }
    }

    private func error(_ rc: Int32) -> SQLiteError {
        SQLiteError(code: rc, description: db.map { String(cString: sqlite3_errmsg($0)) } ?? "closed")
    }

    private func prepare(_ sql: String, _ args: [Any?]) throws -> OpaquePointer {
        var st: OpaquePointer?
        let rc = sqlite3_prepare_v2(db, sql, -1, &st, nil)
        guard rc == SQLITE_OK, let stmt = st else { throw error(rc) }
        for (n, v) in args.enumerated() {
            let i = Int32(n + 1)
            switch v {
            case nil: sqlite3_bind_null(stmt, i)
            case let s as String: sqlite3_bind_text(stmt, i, s, -1, SQLITE_TRANSIENT)
            case let x as Int64: sqlite3_bind_int64(stmt, i, x)
            case let x as Int: sqlite3_bind_int64(stmt, i, Int64(x))
            case let x as Int32: sqlite3_bind_int64(stmt, i, Int64(x))
            case let x as Double: sqlite3_bind_double(stmt, i, x)
            case let x as Bool: sqlite3_bind_int64(stmt, i, x ? 1 : 0)
            case let d as Data:
                _ = d.withUnsafeBytes { sqlite3_bind_blob(stmt, i, $0.baseAddress, Int32(d.count), SQLITE_TRANSIENT) }
            default: sqlite3_bind_text(stmt, i, "\(v!)", -1, SQLITE_TRANSIENT)
            }
        }
        return stmt
    }

    /// One statement that returns no rows.
    public func exec(_ sql: String, _ args: Any?...) throws {
        _ = try run(sql, args)
    }

    /// A query; every row as an array of column values.
    public func query(_ sql: String, _ args: Any?...) throws -> [[Any?]] {
        try run(sql, args)
    }

    public func run(_ sql: String, _ args: [Any?]) throws -> [[Any?]] {
        let st = try prepare(sql, args)
        defer { sqlite3_finalize(st) }
        var rows: [[Any?]] = []
        while true {
            let rc = sqlite3_step(st)
            if rc == SQLITE_DONE { break }
            guard rc == SQLITE_ROW else { throw error(rc) }
            let n = sqlite3_column_count(st)
            var row: [Any?] = []
            row.reserveCapacity(Int(n))
            for c in 0..<n {
                switch sqlite3_column_type(st, c) {
                case SQLITE_NULL: row.append(nil)
                case SQLITE_INTEGER: row.append(sqlite3_column_int64(st, c))
                case SQLITE_FLOAT: row.append(sqlite3_column_double(st, c))
                case SQLITE_BLOB:
                    let len = Int(sqlite3_column_bytes(st, c))
                    row.append(sqlite3_column_blob(st, c).map { Data(bytes: $0, count: len) } ?? Data())
                default: row.append(String(cString: sqlite3_column_text(st, c)))
                }
            }
            rows.append(row)
        }
        return rows
    }

    /// Runs `block` inside BEGIN IMMEDIATE ... COMMIT, rolling back if it
    /// throws. IMMEDIATE because every write transaction here also advances
    /// the HLC kept in `meta`: two connections taking the write lock at the
    /// start never mint the same stamp (SYNC.md §2.2, T18). Reentrant: a
    /// store method run inside another's transaction joins it.
    public func immediate<T>(_ block: () throws -> T) throws -> T {
        if depth > 0 {
            depth += 1
            defer { depth -= 1 }
            return try block()
        }
        try exec("BEGIN IMMEDIATE")
        depth = 1
        defer { depth = 0 }
        do {
            let v = try block()
            try exec("COMMIT")
            return v
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }

    /// A consistent copy of the whole database at `path`, through SQLite's
    /// online backup API (the archive of "use the controller's log": §7).
    public func backup(to path: String) throws {
        try copy(path: path, into: false)
    }

    /// Replace this database with the copy at `path` (the inverse of
    /// backup(to:); the tests' controller restores a backup with it).
    public func restore(from path: String) throws {
        try copy(path: path, into: true)
    }

    private func copy(path: String, into: Bool) throws {
        var other: OpaquePointer?
        guard sqlite3_open(path, &other) == SQLITE_OK else {
            defer { sqlite3_close(other) }
            throw SQLiteError(code: SQLITE_CANTOPEN, description: "cannot open \(path)")
        }
        defer { sqlite3_close(other) }
        let (dst, src) = into ? (db, other) : (other, db)
        guard let b = sqlite3_backup_init(dst, "main", src, "main") else { throw error(sqlite3_errcode(dst)) }
        let rc = sqlite3_backup_step(b, -1)
        sqlite3_backup_finish(b)
        guard rc == SQLITE_DONE else { throw SQLiteError(code: rc, description: "backup failed (\(rc))") }
    }
}
