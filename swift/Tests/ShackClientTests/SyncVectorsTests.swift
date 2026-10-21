import Foundation
import XCTest
@testable import ShackClient

/// The checkout's spec/ directory: the vectors are read from it, never
/// copied, so this suite and the Python and Kotlin ones run one contract.
let specDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("spec")

/// A scratch directory for one test, removed by the caller.
func scratch(_ name: String) -> URL {
    let d = FileManager.default.temporaryDirectory.appendingPathComponent("shack-\(name)-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
    return d
}

/// Runs spec/vectors/sync/T1-T21 (SYNC.md §8) against Station and the
/// reference controller: the Swift runner of the step language in
/// spec/schemas/sync-vector.schema.json, a port of python/shack_client
/// tests/test_vectors.py.
///
/// Time is simulated: one true time advanced by `tick`, and each store or
/// controller sees true time plus its own skew, so clock faults (a device in
/// 1970, a laptop two days fast) are exact and the suite is fast.
final class SyncVectorsTests: XCTestCase {
    static var vectorFiles: [URL] {
        let dir = specDir.appendingPathComponent("vectors/sync")
        return ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent.range(of: #"^T\d+\.json$"#, options: .regularExpression) != nil }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    static func load(_ u: URL) -> JSONObject { JSON.parseObject(try! String(contentsOf: u, encoding: .utf8)) }

    func testEveryCaseOfTheSpecHasAVector() {
        XCTAssertEqual(Self.vectorFiles.map { Self.load($0)["id"]?.str ?? "" }, (1...21).map { "T\($0)" })
    }

    /// One XCTest method per vector, so a failure names its case.
    func runVector(_ id: String) throws {
        let url = specDir.appendingPathComponent("vectors/sync/\(id).json")
        let vec = Self.load(url)
        XCTAssertEqual(vec["runner"]?.str, "script")
        let dir = scratch(id)
        defer { try? FileManager.default.removeItem(at: dir) }
        let w = try World(vec, dir)
        defer { w.close() }
        try w.run((vec["steps"]?.arr ?? []).map(\.obj))
        for e in vec["expect"]?.arr ?? [] { try w.check(e.obj) }
    }

    func testT01() throws { try runVector("T01") }
    func testT02() throws { try runVector("T02") }
    func testT03() throws { try runVector("T03") }
    func testT04() throws { try runVector("T04") }
    func testT05() throws { try runVector("T05") }
    func testT06() throws { try runVector("T06") }
    func testT07() throws { try runVector("T07") }
    func testT08() throws { try runVector("T08") }
    func testT09() throws { try runVector("T09") }
    func testT10() throws { try runVector("T10") }
    func testT11() throws { try runVector("T11") }
    func testT12() throws { try runVector("T12") }
    func testT13() throws { try runVector("T13") }
    func testT15() throws { try runVector("T15") }
    func testT16() throws { try runVector("T16") }
    func testT17() throws { try runVector("T17") }
    func testT19() throws { try runVector("T19") }
    func testT20() throws { try runVector("T20") }
    func testT21() throws { try runVector("T21") }

    /// Every script vector has a test method above: a vector added to spec/
    /// without one fails here rather than going unrun.
    func testEveryScriptVectorIsRun() {
        let scripts = Self.vectorFiles.filter { Self.load($0)["runner"]?.str == "script" }.map { $0.deletingPathExtension().lastPathComponent }
        let methods = Set((0..<200).map { String(format: "T%02d", $0) }.filter {
            // A throwing test method's Objective-C selector carries AndReturnError:.
            responds(to: Selector("test\($0)")) || responds(to: Selector("test\($0)AndReturnError:"))
        })
        XCTAssertFalse(scripts.isEmpty)
        XCTAssertEqual(Set(scripts).subtracting(methods), [])
    }

    /// T14: a controller that accepts the connection and never answers (a
    /// socket listening that nobody serves, so the request hangs as dropped
    /// packets do). Adding a QSO while a sync round is stuck on it returns
    /// within the vector's budget.
    func testT14UnreachableControllerNeverSlowsAWrite() throws {
        let vec = Self.load(specDir.appendingPathComponent("vectors/sync/T14.json"))
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        _ = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        listen(fd, 1)
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) } }
        let url = "http://127.0.0.1:\(UInt16(bigEndian: addr.sin_port))/"
        let dir = scratch("T14")
        defer { try? FileManager.default.removeItem(at: dir) }
        let st = try Station.open(app: "shack-vector", path: dir.appendingPathComponent("a/station.db"),
                                  transports: { HTTPTransport($0, token: $1, timeout: 1) }, background: false)
        defer { st.close() }
        try st.vault.write(["controllerId": "c", "baseUrl": .string(url), "token": "shackdev_x", "deviceId": "", "user": "",
                            "maxLevel": "transmit", "name": "hole"])
        st.sync.now()
        Thread.sleep(forTimeInterval: 0.05)
        var worst = 0.0
        for i in 0..<20 {
            let t0 = Date()
            try st.logbook.add(["call": .string("DL\(i)ABC"), "start": "2026-10-09T10:12:30Z"])
            worst = max(worst, Date().timeIntervalSince(t0))
        }
        XCTAssertLessThan(worst * 1000, Double(vec["budgetMs"]!.int64Value!), "slowest add took \(worst * 1000) ms")
        let deadline = Date().addingTimeInterval(5)
        while st.sync.status().state != "offline" && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        XCTAssertEqual(st.sync.status().state, "offline")
    }

    /// T18: separate connections writing one store file from separate
    /// threads (a test process cannot fork itself under XCTest; SQLite locks
    /// two connections in one process exactly as it locks two processes),
    /// then the lease.
    func testT18TwoWritersOneStore() throws {
        let vec = Self.load(specDir.appendingPathComponent("vectors/sync/T18.json"))
        let n = Int(vec["processes"]!.int64Value!), per = Int(vec["perProcess"]!.int64Value!)
        let dir = scratch("T18")
        defer { try? FileManager.default.removeItem(at: dir) }
        let path = dir.appendingPathComponent("shared/station.db")
        try Station.open(app: "shack-vector", path: path, background: false).close()
        let group = DispatchGroup()
        var failures: [String] = []
        let fl = NSLock()
        for k in 0..<n {
            group.enter()
            Thread {
                defer { group.leave() }
                do {
                    let s = try Station.open(app: "shack-vector", path: path, background: false)
                    defer { s.close() }
                    for i in 0..<per { try s.logbook.add(["call": .string("P\(k)A\(i)X"), "start": "2026-10-09T10:12:30Z"]) }
                } catch {
                    fl.lock(); failures.append("\(error)"); fl.unlock()
                }
            }.start()
        }
        group.wait()
        XCTAssertEqual(failures, [])
        let st = try Station.open(app: "shack-vector", path: path, background: false)
        defer { st.close() }
        XCTAssertEqual(try st.logbook.count(), n * per)
        let stamps = try st.store.query("SELECT hlc FROM ops").map { $0[0] as! String }
        XCTAssertEqual(stamps.count, Set(stamps).count, "two edits shared a stamp")
        let other = try Station.open(app: "shack-vector", path: path, background: false)
        defer { other.close() }
        XCTAssertTrue(try st.store.takeLease(st.sync.holder))
        XCTAssertFalse(try other.store.takeLease(other.sync.holder))
        try st.store.releaseLease(st.sync.holder)
        XCTAssertTrue(try other.store.takeLease(other.sync.holder))
    }
}

struct VectorFailure: Error, CustomStringConvertible {
    let description: String
}

func expect(_ ok: Bool, _ msg: @autoclosure () -> String) throws {
    if !ok { throw VectorFailure(description: msg()) }
}

final class World {
    private var t: Int64
    private var skew: [String: Int64] = [:]
    private let net = Network()
    var ctls: [String: ReferenceController] = [:]
    var stores: [String: Station] = [:]
    private var calls: [String: Int] = [:]
    private var marks: [String: Int] = [:]
    private var backups: [String: Data] = [:]
    private var lastPair: PairResult?
    private var page = 0

    init(_ vec: JSONObject, _ tmp: URL) throws {
        t = try Hlc.parseIsoMs(vec["start"]!.str)
        for (name, c) in vec["controllers"]?.obj ?? [:] {
            skew[name] = c["skewMs"].int64Value ?? 0
            let users = c["users"].objectValue?.mapValues(\.obj)
            let ctl = try ReferenceController(path: tmp.appendingPathComponent("\(name).db"), name: c["name"].stringValue ?? name,
                                              nowMs: clock(name), users: users)
            ctls[name] = ctl
            net.controllers[url(name)] = ctl
        }
        for (name, s) in vec["stores"]?.obj ?? [:] {
            skew[name] = s["skewMs"].int64Value ?? 0
            if let c = s["clock"].stringValue { skew[name] = try Hlc.parseIsoMs(c) - t }
            calls[name] = 0
            stores[name] = try Station.open(app: "shack-vector", appVersion: "0.0", path: tmp.appendingPathComponent("\(name)/station.db"),
                                            clock: clock(name), transports: net.factory(counting: { [unowned self] in self.calls[name]! += 1 }),
                                            background: false)
        }
    }

    private func url(_ name: String) -> String { "http://\(name.lowercased()).test/" }
    private func clock(_ name: String) -> () -> Int64 { { [unowned self] in self.t + (self.skew[name] ?? 0) } }

    func run(_ steps: [JSONObject]) throws { for s in steps { try step(s) } }

    private func store(_ s: JSONObject) -> Station { stores[s["store"]!.str]! }
    private func ctl(_ s: JSONObject) -> ReferenceController { ctls[s["controller"]!.str]! }

    private func step(_ s: JSONObject) throws {
        switch s["do"]!.str {
        case "tick": t += s["ms"]!.int64Value!
        case "skew":
            let who = s["who"]!.str
            skew[who] = s["ms"]!.int64Value!
            if s["resetOffset"]?.boolValue == true { let st = stores[who]!.store; try st.tx { try st.setMeta("clock_offset_ms", 0) } }
        case "add":
            var id = s["id"]?.stringValue
            if let prefix = s["idFromPage"]?.stringValue {
                page += 1
                id = prefix + String(format: "%012d", page)
            }
            try store(s).logbook.add(s["fields"]!.obj, uuid: id)
        case "addMany":
            let items: [JSONObject] = (0..<Int(s["count"]!.int64Value!)).map { i in
                ["uuid": .string(s["idPrefix"]!.str + String(format: "%012d", i)), "call": .string("T\(i)"), "start": "2026-10-09T09:00:00Z"]
            }
            try store(s).logbook.addMany(items)
        case "update":
            let fields = s["fields"]?.objectValue ?? [s["fieldsFromPage"]!.str: .string("page \(page)")]
            try store(s).logbook.update(s["id"]!.str, fields)
        case "delete": try store(s).logbook.delete(s["id"]!.str)
        case "profile": try store(s).profile.update(s["set"]!.obj)
        case "pair":
            let st = store(s)
            if let n = s["pageLimit"]?.int64Value { st.sync.pageLimit = Int(n) }
            if let steps = s["duringEachPage"]?.arr.map(\.obj) {
                st.sync.afterPullPage = { [unowned self] _ in try! self.run(steps) }
            }
            let res = try st.pair(url(s["controller"]!.str), username: s["user"]?.stringValue ?? "brian",
                                  password: s["password"]?.stringValue ?? "pw", deviceName: s["store"]!.str)
            st.sync.afterPullPage = nil
            let got: String
            if case .needsDecision = res { got = "needsDecision" } else { got = "paired" }
            try expect(got == (s["expect"]?.stringValue ?? "paired"), "pair \(s["store"]!.str) -> \(s["controller"]!.str): \(got)")
            lastPair = res
        case "resolve":
            try store(s).resolve(lastPair!, choice: s["choice"]?.str == "use-controller" ? .useController : .merge)
        case "unpair": try store(s).unpair()
        case "sync":
            let st = store(s)
            if s["fromSeq"]?.int64Value == 0 { try st.store.dropCursor(st.pairingInfo()!["controllerId"]!.str) }
            let status = try st.sync.runOnce()
            try expect(status.state == (s["expectState"]?.stringValue ?? "idle"), "sync \(s["store"]!.str): \(status)")
        case "dropNextPushAnswer": ctl(s).dropNextPushAnswer = true
        case "offline": ctl(s).offline = s["value"]?.boolValue == true
        case "backup": backups[s["name"]!.str] = try ctl(s).sync.snapshot()
        case "restore": try ctl(s).sync.restore(backups[s["name"]!.str]!)
        case "inject":
            let (status, ans) = try ctl(s).sync.push(["records": [s["record"]!]])
            try expect(status == 200 && ans["records"]?.arr.first?["status"].str == "merged", "\(ans)")
        case "markRequests": marks[s["store"]!.str] = calls[s["store"]!.str]!
        case let op: throw VectorFailure(description: "unknown step \(op)")
        }
    }

    // MARK: views

    private func records(_ who: String, _ collection: String) throws -> [String: Record] {
        if let c = ctls[who] {
            return Dictionary(uniqueKeysWithValues: c.sync.all(collection).map { r -> (String, Record) in
                var x = r; x.removeValue(forKey: "seq"); return (r["id"]!.str, x)
            })
        }
        return Dictionary(uniqueKeysWithValues: try stores[who]!.store.all(collection).map { ($0["id"]!.str, $0) })
    }

    private func stamps(_ who: String) throws -> [String] {
        try ["logbook", "profile"].flatMap { c in try records(who, c).values.flatMap(Merge.stamps) }
    }

    // MARK: expectations

    func check(_ e: JSONObject) throws {
        let (kind, a0) = e.first!
        let a = a0.obj
        let whereList = a["where"]?.arr.map(\.str) ?? []
        switch kind {
        case "record":
            for who in whereList {
                guard let r = try records(who, a["collection"]!.str)[a["id"]!.str] else { throw VectorFailure(description: "\(who) lacks \(a["id"]!.str)") }
                if let live = a["live"]?.boolValue { try expect(Merge.isLive(r) == live, "\(who): live is \(Merge.isLive(r))") }
                for (k, v) in a["fields"]?.obj ?? [:] {
                    try expect(Merge.fields(r)[k] == v, "\(who): \(k) = \(String(describing: Merge.fields(r)[k])), expected \(v)")
                }
                for (k, v) in a["extra"]?.obj ?? [:] { try expect(r[k] == v, "\(who): envelope \(k) = \(String(describing: r[k]))") }
            }
        case "count", "countAtLeast":
            for who in whereList {
                let n = try records(who, a["collection"]!.str).values.filter(Merge.isLive).count
                let want = Int(a["live"]!.int64Value!)
                try expect(kind == "count" ? n == want : n >= want, "\(who) has \(n) live records, expected \(kind == "count" ? "" : "at least ")\(want)")
            }
        case "converged":
            func view(_ who: String) throws -> [String: String] {
                try records(who, a["collection"]!.str).mapValues { r in
                    "\(Merge.isLive(r))|\(JSON.dump(Merge.fields(r)))|\(JSON.dump(.object(Merge.fieldHlc(r).mapValues { .string($0) })))|\(Merge.deletedHlc(r) ?? "")"
                }
            }
            let first = try view(whereList[0])
            for who in whereList.dropFirst() {
                let other = try view(who)
                try expect(Set(first.keys) == Set(other.keys), "\(whereList[0]) and \(who) hold different records: \(Set(first.keys).symmetricDifference(other.keys))")
                let diff = first.keys.filter { first[$0] != other[$0] }.sorted()
                try expect(diff.isEmpty, "\(whereList[0]) and \(who) differ on \(diff.prefix(3)): \(diff.first.map { "\(first[$0]!) / \(other[$0]!)" } ?? "")")
            }
        case "history":
            let h = ctls[a["controller"]!.str]!.sync.history("logbook", a["id"]!.str)
            if let n = a["edits"]?.int64Value { try expect(h.count == Int(n), "history \(h)") }
            if let lost = a["lostFields"]?.arr.map(\.str) {
                try expect(Set(h.flatMap { $0["lost"]?.arr.map(\.str) ?? [] }).sorted() == lost.sorted(), "lost \(h)")
            }
            if let kinds = a["kinds"]?.arr.map(\.str) { try expect(h.map { $0["kind"]!.str }.sorted() == kinds.sorted(), "kinds \(h)") }
        case "status":
            let st = stores[a["store"]!.str]!.sync.status()
            let m: JSONObject = ["state": .string(st.state), "controller": .string(st.controller), "lastSyncAt": .string(st.lastSyncAt),
                                 "pendingRecords": .int(Int64(st.pendingRecords)), "error": .string(st.error), "profileRejected": .bool(st.profileRejected)]
            for (k, v) in a where k != "store" { try expect(m[k] == v, "status \(k): \(st)") }
        case "noFutureHlc":
            let c = ctls[a["controller"]!.str]!
            let limit = c.nowMs() + Hlc.maxAheadMs
            let bad = try stamps(a["controller"]!.str).filter { Hlc.wallOf($0) > limit }
            try expect(bad.isEmpty, "future stamps \(bad)")
        case "hlcAtLeastFloor":
            let s = try stamps(a["store"]!.str)
            try expect(!s.isEmpty && s.allSatisfy { Hlc.wallOf($0) >= Hlc.clockFloorMs }, "\(s)")
        case "archive":
            let d = stores[a["store"]!.str]!.store.path.deletingLastPathComponent()
            let names = try FileManager.default.contentsOfDirectory(atPath: d.path)
            try expect(names.contains { $0.hasPrefix("station-") && $0.hasSuffix(".db") } &&
                       names.contains { $0.hasPrefix("station-") && $0.hasSuffix(".adi") }, "\(names)")
        case "requests":
            let who = a["store"]!.str
            try expect(calls[who]! - marks[who]! == Int(a["sinceMark"]!.int64Value!), "requests \(calls[who]! - marks[who]!)")
        case "profile":
            func contains(_ big: JSONObject, _ small: JSONObject) -> Bool {
                small.allSatisfy { k, v in
                    guard let b = big[k] else { return false }
                    if let sv = v.objectValue { return b.objectValue.map { contains($0, sv) } ?? false }
                    return b == v
                }
            }
            for who in whereList {
                let got = try stores[who]!.profile.get()
                try expect(contains(got, a["has"]!.obj), "\(who): \(got)")
            }
        default: throw VectorFailure(description: "unknown expectation \(kind)")
        }
    }

    func close() {
        stores.values.forEach { $0.close() }
        ctls.values.forEach { $0.close() }
    }
}
