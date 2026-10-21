import Foundation

/// §7: the same status in every app. `state` is `unpaired`, `idle`,
/// `syncing`, `offline`, `error` (with `error` the controller's sentence) or
/// `needsDecision`.
public struct SyncStatus: Equatable {
    public var state: String
    public var controller: String = ""
    public var lastSyncAt: String = ""
    public var pendingRecords: Int = 0
    public var error: String = ""
    public var profileRejected: Bool = false
}

struct Refused: Error {
    let status: Int
    let message: String
    let code: String
}

func check(_ r: Response) throws {
    if r.status >= 400 {
        let msg = r.body["error"]?.str ?? ""
        throw Refused(status: r.status, message: msg.isEmpty ? "The controller answered \(r.status)." : msg, code: r.body["code"]?.str ?? "")
    }
}

/// The sync round and its triggers (SYNC.md §6.3, §7), a port of
/// python/shack_client sync.py.
///
/// A round: take the store's sync lease; measure the controller's clock
/// (once per pairing, then hourly); push dirty records and unacknowledged
/// ops; pull until `more` is false, merging each record; store the cursor;
/// release the lease. Nothing in a UI path waits for any of it: `now()`
/// asks for a round and returns at once, and a background thread does the
/// work. Failures back off 1 s, 2 s, 4 s ... to 5 min.
public final class SyncClient: @unchecked Sendable {
    public static let debounce: TimeInterval = 2
    public static let period: TimeInterval = 60
    public static let backoffMax: TimeInterval = 300
    public static let offsetEvery: TimeInterval = 3600
    public static let maxOffsetErrorMs: Int64 = 2000

    unowned let st: Station
    /// Unique per client, not per process: two Stations on one file must not
    /// share a lease.
    public let holder = UUID().uuidString

    private let stateLock = NSLock()
    private var _state = "unpaired", _error = "", _lastSyncAt = "", _profileRejected = false
    public internal(set) var state: String { get { locked { _state } } set { locked { _state = newValue } } }
    public internal(set) var error: String { get { locked { _error } } set { locked { _error = newValue } } }
    public private(set) var lastSyncAt: String { get { locked { _lastSyncAt } } set { locked { _lastSyncAt = newValue } } }
    public internal(set) var profileRejected: Bool { get { locked { _profileRejected } } set { locked { _profileRejected = newValue } } }

    /// Records per pull page (tests set it lower to exercise paging).
    public var pageLimit = 500
    /// Test hook: called with each pull page's answer after it is merged.
    public var afterPullPage: ((JSONObject) -> Void)?
    public var debounce = SyncClient.debounce
    public var period = SyncClient.period

    private var offsetAt: Date?
    private var backoff: TimeInterval = 0
    private let roundLock = NSLock()
    private let cond = NSCondition()
    private var woken = false
    private var stopping = false
    private var thread: Thread?
    private var running = false
    private var listeners: [UUID: (SyncStatus) -> Void] = [:]

    init(_ st: Station) { self.st = st }

    private func locked<T>(_ f: () -> T) -> T { stateLock.lock(); defer { stateLock.unlock() }; return f() }

    // MARK: what apps see

    public func status() -> SyncStatus {
        let p = st.pairingInfo()
        var s = state
        if p == nil { s = "unpaired" } else if (try? st.store.meta("needs_decision")) != nil { s = "needsDecision" }
        return SyncStatus(state: s, controller: p?["name"]?.str ?? "", lastSyncAt: lastSyncAt,
                          pendingRecords: (try? st.store.pendingCount()) ?? 0, error: s == "error" ? error : "",
                          profileRejected: profileRejected)
    }

    /// The status after every round, as an AsyncStream for SwiftUI.
    public var statusUpdates: AsyncStream<SyncStatus> {
        AsyncStream { cont in
            let id = addListener { cont.yield($0) }
            cont.onTermination = { [weak self] _ in self?.removeListener(id) }
        }
    }

    @discardableResult
    public func addListener(_ f: @escaping (SyncStatus) -> Void) -> UUID {
        let id = UUID()
        locked { listeners[id] = f }
        return id
    }

    public func removeListener(_ id: UUID) { _ = locked { listeners.removeValue(forKey: id) } }

    private func publish() {
        let ls = locked { Array(listeners.values) }
        if ls.isEmpty { return }
        let s = status()
        ls.forEach { $0(s) }
    }

    private var isRunning: Bool { cond.lock(); defer { cond.unlock() }; return running }

    /// Ask for a round; returns at once.
    public func now() {
        if isRunning { wake(); return }
        Thread { [weak self] in self?.safeRound() }.start()
    }

    /// A local edit happened: sync within the debounce (§6.3).
    public func localWrite() { if isRunning { wake() } }

    private func wake() {
        cond.lock(); woken = true; cond.broadcast(); cond.unlock()
    }

    /// Waits up to `seconds`; true when woken early.
    private func waitWake(_ seconds: TimeInterval) -> Bool {
        cond.lock()
        defer { cond.unlock() }
        let end = Date().addingTimeInterval(seconds)
        while !woken && !stopping {
            if !cond.wait(until: end) { break }
        }
        let w = woken
        woken = false
        return w
    }

    // MARK: the thread

    public func start() {
        cond.lock()
        defer { cond.unlock() }
        if running { return }
        stopping = false
        running = true
        let t = Thread { [weak self] in self?.loop() }
        t.name = "shack-sync"
        thread = t
        t.start()
    }

    public func stop() {
        cond.lock()
        stopping = true
        woken = true
        cond.broadcast()
        let t = thread
        cond.unlock()
        if let t = t, t != Thread.current {
            // Wait for a round in flight to finish (bounded by the 10 s
            // request timeout).
            let end = Date().addingTimeInterval(15)
            while isRunning && Date() < end { Thread.sleep(forTimeInterval: 0.01) }
        }
        cond.lock(); thread = nil; cond.unlock()
    }

    private var isStopping: Bool { cond.lock(); defer { cond.unlock() }; return stopping }

    private func loop() {
        defer { cond.lock(); running = false; cond.unlock() }
        while !isStopping {
            safeRound()
            let woke = waitWake(backoff > 0 ? backoff : period)
            if woke && !isStopping && backoff == 0 {
                // Debounce: a burst of edits becomes one round.
                cond.lock()
                if !stopping { _ = cond.wait(until: Date().addingTimeInterval(debounce)) }
                if !stopping { woken = false }
                cond.unlock()
            }
        }
    }

    private func safeRound() {
        do {
            _ = try runOnce()
        } catch {
            // A bug must not kill the thread: say so in the status.
            state = "error"
            self.error = "Sync stopped on an unexpected problem: \(error)"
            publish()
        }
    }

    // MARK: one round

    /// One sync round, synchronously (off the main actor). Returns the status.
    @discardableResult
    public func runOnce() throws -> SyncStatus {
        roundLock.lock()
        defer { roundLock.unlock() }
        guard let p = st.pairingInfo() else {
            state = "unpaired"
            return status()
        }
        let store = st.store
        if try store.meta("needs_decision") != nil { return status() }
        // Another process is syncing this store.
        guard try store.takeLease(holder) else { return status() }
        state = "syncing"
        let cid = p["controllerId"]?.str ?? ""
        do {
            defer { try? store.releaseLease(holder) }
            do {
                let t = try st.transport()
                try measure(t)
                try push(t, cid)
                try pull(t, cid)
                state = "idle"
                error = ""
                lastSyncAt = try store.nowIso()
                var known = (try? JSON.parse(try store.meta("synced_with") ?? "[]"))?.arr ?? []
                if !known.contains(.string(cid)) {
                    known.append(.string(cid))
                    try store.tx { try store.setMeta("synced_with", JSON.dump(.array(known))) }
                }
                backoff = 0
            } catch is Offline {
                state = "offline"
                backoff = min(SyncClient.backoffMax, max(1, backoff * 2))
            } catch let r as Refused {
                state = "error"
                error = r.message
                backoff = min(SyncClient.backoffMax, max(1, backoff * 2))
            }
        }
        let s = status()
        publish()
        return s
    }

    /// §3.2 offset: the controller's `time`, half the round trip as the
    /// error, kept when the error is under 2 s.
    private func measure(_ t: Transport, force: Bool = false) throws {
        if !force, let at = offsetAt, Date().timeIntervalSince(at) < SyncClient.offsetEvery { return }
        let t0 = st.store.clock()
        let r = try t.get("api/shack/v1/")
        let t1 = st.store.clock()
        guard r.status == 200, let time = r.body["time"]?.stringValue else {
            try check(r)
            return
        }
        if (t1 - t0) / 2 < SyncClient.maxOffsetErrorMs {
            let offset = try Hlc.parseIsoMs(time) - (t0 + t1) / 2
            try st.store.tx { try st.store.setMeta("clock_offset_ms", offset) }
        }
        offsetAt = Date()
    }

    private func key(_ o: JSONObject) -> RecordKey { RecordKey(o["collection"]?.str ?? "", o["id"]?.str ?? "") }

    private func push(_ t: Transport, _ cid: String) throws {
        let store = st.store
        var rollback = Set<RecordKey>()
        // Bounded so a controller that keeps answering without confirming
        // anything cannot spin this loop; each pass moves up to 500 records.
        for _ in 0..<1000 {
            let batch = try store.dirty(limit: 500)
            let keys = Set(batch.map { key($0.0) })
            let dirty = try store.dirtyKeys()
            // Ops travel with their record's state, or alone once that state
            // is confirmed; an op whose state is still waiting for a later
            // batch would only be ignored.
            let ops = try store.unackedOps(cid, limit: 2000).filter { keys.contains(key($0)) || !dirty.contains(key($0)) }
            if batch.isEmpty && ops.isEmpty { break }
            let body: JSONObject = ["storeId": .string(store.storeId), "records": .array(batch.map { .object($0.0) }),
                                    "ops": .array(ops.map { .object($0) })]
            let r = try t.post("api/shack/v1/sync/push", body)
            try check(r)
            var revs: [RecordKey: Int64] = [:]
            for (rec, rev) in batch { revs[key(rec)] = rev }
            var acked: [(RecordKey, Int64)] = []
            var ahead = false
            var rejectedOps: [String] = []
            for res in (r.body["records"]?.arr ?? []).map(\.obj) {
                let k = key(res)
                let s = res["status"]?.str ?? "", code = res["code"]?.str ?? ""
                if (s == "merged" || s == "unchanged"), let rev = revs[k] {
                    acked.append((k, rev))
                } else if s == "rejected" && code == "clock_ahead" {
                    ahead = true
                } else if s == "rejected" && code == "level_too_low" {
                    // §4.2: roll the local copy back to the controller's on
                    // the next pull; its ops are never pushed again.
                    rollback.insert(k)
                    acked.append((k, revs[k] ?? -1))
                    rejectedOps += ops.filter { key($0) == k }.map { $0["opId"]?.str ?? "" }
                    profileRejected = true
                }
            }
            let done = r.body["ops"]?.obj ?? [:]
            let accepted = (done["accepted"]?.arr ?? []).map(\.str)
            let duplicate = (done["duplicate"]?.arr ?? []).map(\.str)
            try store.ack(cid, records: acked, opIds: accepted + duplicate, rejectedOps: rejectedOps)
            if ahead {
                // §3.3: re-measure, reset the HLC to the corrected clock,
                // re-stamp what is unacknowledged, push again.
                try measure(t, force: true)
                try store.restampUnacked(cid)
                continue
            }
            if acked.isEmpty && accepted.isEmpty && duplicate.isEmpty { break }   // nothing moved: do not spin
        }
        if !rollback.isEmpty { try rollBack(t, rollback) }
    }

    private func rollBack(_ t: Transport, _ keys: Set<RecordKey>) throws {
        let r = try t.get("api/shack/v1/sync/pull", query: [("since", "0"), ("limit", "500"), ("collections", "profile")])
        try check(r)
        var theirs: [RecordKey: Record] = [:]
        for rec in (r.body["records"]?.arr ?? []).map(\.obj) { theirs[key(rec)] = rec }
        let recs: [Record] = keys.map { k in
            // The controller has no profile yet: the station's profile is the
            // controller's when paired, so the local edit goes (it stays in
            // the local ops as history).
            theirs[k] ?? ["collection": .string(k.collection), "id": .string(k.id), "fields": .object([:]),
                          "fieldHlc": .object([:]), "deletedHlc": .null, "createdAt": "", "origin": .object([:])]
        }
        st.notify(try st.store.mergeRemote(recs, replace: keys))
    }

    private func pull(_ t: Transport, _ cid: String) throws {
        let store = st.store
        for _ in 0..<2 {
            let cur = try store.cursor(cid)
            var epoch = cur?.epoch
            var since = cur?.seq ?? 0
            while true {
                let r = try t.get("api/shack/v1/sync/pull", query: [("since", "\(since)"), ("epoch", epoch),
                                                                     ("limit", "\(pageLimit)"), ("collections", "logbook,profile")])
                if r.status == 409 && r.body["code"]?.str == "epoch_changed" {
                    // §6.2: restored from a backup. Drop the cursor, push
                    // everything, pull from the start.
                    try store.dropCursor(cid)
                    try store.markAllDirty()
                    try push(t, cid)
                    break
                }
                try check(r)
                st.notify(try store.mergeRemote((r.body["records"]?.arr ?? []).map(\.obj)))
                if let n = r.body["next"]?.int64Value { since = n }
                if let e = r.body["epoch"]?.stringValue { epoch = e }
                try store.setCursor(cid, epoch: epoch ?? "", seq: since)
                afterPullPage?(r.body)
                if r.body["more"]?.boolValue != true {
                    // Anything merged that the controller lacks goes back.
                    if try store.pendingCount() > 0 { try push(t, cid) }
                    return
                }
            }
        }
    }
}
