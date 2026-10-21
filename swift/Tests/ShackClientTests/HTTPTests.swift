import Foundation
import XCTest
@testable import ShackClient

/// The client against the reference controller over real HTTP (URLSession
/// to a loopback socket): pairing by password and TOTP, by TV code, a
/// dropped answer, well-known resolution and the background thread. A port
/// of tests/test_http.py.
final class HTTPTests: XCTestCase {
    var dir: URL!
    var ctl: ReferenceController!
    var url: String!
    let T: JSONValue = "2026-10-09T10:12:30Z"

    override func setUpWithError() throws {
        dir = scratch("http")
        ctl = try ReferenceController(path: dir.appendingPathComponent("services.db"),
                                      users: ["brian": ["password": "pw", "transmit": true, "totp": "123456"]])
        url = try ctl.serve()
    }

    override func tearDown() {
        ctl.close()
        try? FileManager.default.removeItem(at: dir)
    }

    func station(_ name: String = "a", background: Bool = false) throws -> Station {
        try Station.open(app: "remote", appVersion: "1.0.0", path: dir.appendingPathComponent("\(name)/station.db"), background: background)
    }

    func pairingCode(_ f: () throws -> Any) -> String {
        do { _ = try f(); return "no error" } catch let e as PairingError { return e.code } catch { return "\(error)" }
    }

    func testPasswordPairingAsksForTotpThenSyncs() throws {
        let st = try station()
        defer { st.close() }
        try st.logbook.add(["call": "DL1ABC", "start": T])
        XCTAssertEqual(pairingCode { try st.pair(url, username: "brian", password: "pw", deviceName: "iPad") }, "totp_required")
        XCTAssertEqual(pairingCode { try st.pair(url, username: "brian", password: "wrong", totp: "123456") }, "bad_credentials")
        let p = try st.pair(url, username: "brian", password: "pw", totp: "123456", deviceName: "iPad")
        guard case .paired(let info) = p else { return XCTFail("\(p)") }
        XCTAssertEqual(info.controllerId, ctl.controllerId)
        XCTAssertEqual(info.name, "GM4BRI")
        XCTAssertEqual(ctl.records().count, 1)
        XCTAssertEqual(st.sync.status().state, "idle")
        XCTAssertTrue(st.pairingInfo()!["token"]!.str.hasPrefix("shackdev_"))
        XCTAssertEqual(ctl.devices.values.first?["name"], "iPad")
    }

    func testATvIsRefusedAPassword() throws {
        let st = try station()
        defer { st.close() }
        XCTAssertEqual(pairingCode { try st.pair(url, username: "brian", password: "pw", totp: "123456", deviceKind: "tv") }, "use_tv_pairing")
    }

    func testPairingAgainRevokesThePreviousToken() throws {
        let st = try station()
        defer { st.close() }
        try st.pair(url, username: "brian", password: "pw", totp: "123456")
        let first = st.pairingInfo()!["token"]!.str
        try st.pair(url, username: "brian", password: "pw", totp: "123456")
        let r = try HTTPTransport(url, token: first).get("api/shack/v1/")
        XCTAssertEqual(r.status, 401)
        XCTAssertEqual(r.body["code"], "token_revoked")
        XCTAssertEqual(try st.sync.runOnce().state, "idle")
    }

    func testADroppedAnswerOverHttpIsRetriedHarmlessly() throws {
        let st = try station()
        defer { st.close() }
        try st.pair(url, username: "brian", password: "pw", totp: "123456")
        let q = try st.logbook.add(["call": "DL1ABC", "start": T])
        ctl.dropNextPushAnswer = true
        // URLSession may resend a request whose connection closed before any
        // answer (as the JDK's connection does), so the first round can
        // already end idle. Either way the push reached the controller
        // twice and the merge made the second one harmless.
        let first = try st.sync.runOnce().state
        XCTAssertTrue(first == "offline" || first == "idle", first)
        XCTAssertEqual(try st.sync.runOnce().state, "idle")
        XCTAssertGreaterThanOrEqual(ctl.requestPaths().filter { $0.hasPrefix("/api/shack/v1/sync/push") }.count, 2)
        XCTAssertEqual(ctl.records().count, 1)
        XCTAssertEqual(ctl.sync.history("logbook", q.uuid).count, 1)
    }

    func testTvPairingByCodeApprovedFromAPhone() throws {
        let phone = try station("phone"), tv = try station("tv")
        defer { phone.close(); tv.close() }
        try phone.pair(url, username: "brian", password: "pw", totp: "123456", deviceKind: "phone")
        var shown = ""
        let (state, result) = try tv.pairTV(url, deviceName: "Living room TV", onCode: { s in
            guard case .code(let c, _) = s else { return }
            shown = c
            // The person reads the code off the TV and approves it on the phone.
            let r = try! phone.transport().post("api/shack/v1/pair/approve", ["code": .string(c), "maxLevel": "control"])
            XCTAssertEqual(r.status, 200)
        }, sleep: { _ in })
        XCTAssertEqual(shown.count, 6)
        guard case .approved(let tok) = state else { return XCTFail("\(state)") }
        XCTAssertEqual(tok.maxLevel, "control")
        XCTAssertEqual(tok.controllerId, ctl.controllerId)
        if case .paired = result {} else { XCTFail("\(String(describing: result))") }
        XCTAssertEqual(tv.pairingInfo()?["maxLevel"], "control")
        XCTAssertEqual(tv.sync.status().state, "idle")
        XCTAssertFalse(tok.description.contains("shackdev_"))
    }

    func testTvPairingDeniedAndExpired() throws {
        let phone = try station("phone"), tv = try station("tv")
        defer { phone.close(); tv.close() }
        try phone.pair(url, username: "brian", password: "pw", totp: "123456")
        let (denied, _) = try tv.pairTV(url, deviceName: "TV", onCode: { s in
            guard case .code(let c, _) = s else { return }
            _ = try! phone.transport().post("api/shack/v1/pair/approve", ["code": .string(c), "deny": true])
        }, sleep: { _ in })
        XCTAssertEqual(denied, .denied)
        let t = HTTPTransport(url)
        let p = try Pairing.tvStart(t, deviceName: "TV", clientId: "tv-1", app: "remote")
        XCTAssertEqual(try Pairing.tvPoll(t, p).0, "pending")
        let wrong = TVPairing(pairId: p.pairId, pairSecret: "wrong", code: p.code, expiresIn: 300, pollEvery: 2)
        XCTAssertEqual(try Pairing.tvPoll(t, wrong).0, "expired")
    }

    func testWellKnownResolvesAgainstTheBase() throws {
        let wk = try Discovery.wellKnown(url)
        XCTAssertEqual(wk["controllerId"]?.str, ctl.controllerId)
        XCTAssertEqual(wk["auth"]?["token"].str, url + "api/shack/token")
        XCTAssertEqual(wk["services"]?["base"].str, url + "api/shack/v1/")
    }

    func testWellKnownOfNothingIsOffline() {
        XCTAssertThrowsError(try Discovery.wellKnown("http://127.0.0.1:9/")) { XCTAssertTrue($0 is Offline, "\($0)") }
    }

    func testBackgroundThreadSyncsAfterALocalWriteAndUnpairStopsIt() throws {
        let st = try station(background: true)
        defer { st.close() }
        st.sync.debounce = 0.05
        var seen: [SyncStatus] = []
        let lock = NSLock()
        st.sync.addListener { s in lock.lock(); seen.append(s); lock.unlock() }
        try st.pair(url, username: "brian", password: "pw", totp: "123456")
        try st.logbook.add(["call": "DL1ABC", "start": T])
        let deadline = Date().addingTimeInterval(5)
        while ctl.records().isEmpty && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        XCTAssertEqual(ctl.records().count, 1)
        lock.lock(); XCTAssertTrue(seen.contains { $0.state == "idle" }); lock.unlock()
        // Unpair stops the thread (waiting for a round in flight to finish),
        // and from then on nothing reaches the controller.
        try st.unpair()
        let before = ctl.requestCount()
        try st.logbook.add(["call": "F4XYZ", "start": T])
        Thread.sleep(forTimeInterval: 0.3)
        XCTAssertEqual(ctl.requestCount(), before)
        XCTAssertEqual(try st.logbook.count(), 2)
        XCTAssertEqual(st.sync.status().state, "unpaired")
    }

    func testAControllerSideChangeReachesTheApp() throws {
        let st = try station()
        defer { st.close() }
        var seen: [(String?, [String])] = []
        let lock = NSLock()
        st.onChange { c, ids in lock.lock(); seen.append((c, ids)); lock.unlock() }
        try st.pair(url, username: "brian", password: "pw", totp: "123456")
        let q = try st.logbook.add(["call": "DL1ABC", "start": T])
        try st.sync.runOnce()
        try ctl.sync.edit("logbook", q.uuid, kind: "update", fields: ["qsl_rcvd": "Y"], origin: ["app": "controller-ui"])
        try st.sync.runOnce()
        XCTAssertEqual(try st.logbook.get(q.uuid)!.fields["qsl_rcvd"], "Y")
        lock.lock(); XCTAssertTrue(seen.contains { $0.0 == "logbook" && $0.1 == [q.uuid] }); lock.unlock()
        XCTAssertTrue(ctl.sync.history("logbook", q.uuid).last!["hlc"]!.str.hasSuffix(ctl.sync.node))
    }
}
