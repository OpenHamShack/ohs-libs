import Foundation
import XCTest
@testable import ShackClient

/// The merge's algebra, the HLC, JSON, the logbook, ADIF and discovery:
/// everything but the sync vectors and real HTTP. Ports of
/// python/shack_client tests/test_merge.py, test_logbook.py and
/// test_discovery.py, as the Kotlin suite's are.
final class ClientTests: XCTestCase {
    let T: JSONValue = "2026-10-09T10:12:30Z"
    var dir: URL!

    override func setUp() { dir = scratch("client") }
    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    func station(_ name: String = "a") throws -> Station {
        try Station.open(app: "shack-test", appVersion: "1.0.0", path: dir.appendingPathComponent("\(name)/station.db"), background: false)
    }

    // MARK: merge and HLC

    let nodes = ["st_AAAAAAAAAAAA", "st_BBBBBBBBBBBB", "st_CCCCCCCCCCCC"]
    func h(_ n: Int64, _ node: String = "st_AAAAAAAAAAAA") -> String { Hlc.fmt(Hlc.clockFloorMs + n, 0, node) }

    /// A small deterministic generator, so a failing seed can be replayed.
    struct LCG: RandomNumberGenerator {
        var s: UInt64
        mutating func next() -> UInt64 { s = s &* 6364136223846793005 &+ 1442695040888963407; return s }
    }

    func randomRecord(_ g: inout LCG) -> Record {
        var fields: JSONObject = [:], hlcs: JSONObject = [:]
        for f in ["call", "rst_rcvd", "rst_sent", "comment", "qsl_sent"].shuffled(using: &g).prefix(Int.random(in: 1...5, using: &g)) {
            let s = Hlc.fmt(Hlc.clockFloorMs + Int64.random(in: 0...20, using: &g), Int.random(in: 0...3, using: &g), nodes.randomElement(using: &g)!)
            hlcs[f] = .string(s)
            if Double.random(in: 0..<1, using: &g) > 0.2 { fields[f] = .string("\(f)-\(s.suffix(4))-\(Int.random(in: 0...9, using: &g))") }
        }
        let dh: JSONValue = Double.random(in: 0..<1, using: &g) < 0.4
            ? .string(Hlc.fmt(Hlc.clockFloorMs + Int64.random(in: 0...20, using: &g), 0, nodes.randomElement(using: &g)!)) : .null
        return ["collection": "logbook", "id": "q", "fields": .object(fields), "fieldHlc": .object(hlcs), "deletedHlc": dh,
                "createdAt": .string("2026-10-09T10:00:\(Int.random(in: 10...59, using: &g)).000Z"),
                "origin": ["app": .string(["x", "y", "z"].randomElement(using: &g)!)]]
    }

    /// The same stamp always carries the same value, as real edits do.
    func consistent(_ recs: [Record]) -> [Record] {
        var seen: [String: JSONValue?] = [:]
        return recs.map { r in
            var r = r
            var fields = Merge.fields(r)
            for (f, s) in Merge.fieldHlc(r) {
                let k = f + "|" + s
                if let v = seen[k] { fields[f] = v } else { seen[k] = .some(fields[f]) }
            }
            r["fields"] = .object(fields)
            return r
        }
    }

    func testMergeIsCommutativeAssociativeIdempotent() {
        for seed in 0..<300 {
            var g = LCG(s: UInt64(seed) &+ 1)
            let rs = consistent((0..<3).map { _ in randomRecord(&g) })
            let (a, b, c) = (rs[0], rs[1], rs[2])
            XCTAssertTrue(Merge.same(Merge.merge(a, b), Merge.merge(b, a)), "commutative, seed \(seed)")
            XCTAssertTrue(Merge.same(Merge.merge(Merge.merge(a, b), c), Merge.merge(a, Merge.merge(b, c))), "associative, seed \(seed)")
            XCTAssertTrue(Merge.same(Merge.merge(a, a), a), "idempotent, seed \(seed)")
            let want = Merge.merge(Merge.merge(a, b), c)
            for p in [[a, c, b], [b, a, c], [b, c, a], [c, a, b], [c, b, a]] {
                XCTAssertTrue(Merge.same(Merge.merge(Merge.merge(p[0], p[1]), p[2]), want), "order, seed \(seed)")
            }
        }
    }

    func testEditBeatsAnEarlierDeleteAndLosesToALaterOne() {
        let r = Merge.applyEdit(nil, collection: "logbook", id: "q", kind: "create", fields: ["call": "DL1ABC"], hlc: h(1), origin: [:], nowIso: "t")
        let deleted = Merge.applyEdit(r, collection: "logbook", id: "q", kind: "delete", fields: [:], hlc: h(2), origin: [:], nowIso: "t")
        XCTAssertFalse(Merge.isLive(deleted))
        let back = Merge.applyEdit(deleted, collection: "logbook", id: "q", kind: "update", fields: ["rst_rcvd": "57"], hlc: h(3), origin: [:], nowIso: "t")
        XCTAssertTrue(Merge.isLive(back))
        XCTAssertEqual(Merge.fields(back), ["call": "DL1ABC", "rst_rcvd": "57"])
    }

    func testANullValueClearsAFieldWithItsOwnStamp() {
        let r = Merge.applyEdit(nil, collection: "logbook", id: "q", kind: "create", fields: ["call": "DL1ABC", "comment": "x"], hlc: h(1), origin: [:], nowIso: "t")
        let cleared = Merge.applyEdit(r, collection: "logbook", id: "q", kind: "update", fields: ["comment": .null], hlc: h(2), origin: [:], nowIso: "t")
        XCTAssertNil(Merge.fields(cleared)["comment"])
        XCTAssertEqual(Merge.fieldHlc(cleared)["comment"], h(2))
        XCTAssertNil(Merge.fields(Merge.merge(r, cleared))["comment"])
    }

    func testUnknownEnvelopeKeysSurviveAndTheNewerSideWins() {
        var old = Merge.applyEdit(nil, collection: "logbook", id: "q", kind: "create", fields: ["call": "A"], hlc: h(1), origin: [:], nowIso: "t")
        old["x_future"] = 1
        var new = Merge.applyEdit(old, collection: "logbook", id: "q", kind: "update", fields: ["call": "B"], hlc: h(2), origin: [:], nowIso: "t")
        new["x_future"] = 2
        XCTAssertEqual(Merge.merge(old, new)["x_future"], 2)
        XCTAssertEqual(Merge.merge(new, old)["x_future"], 2)
    }

    func testOriginTieBreaksOnThePythonJsonOfTheOrigin() {
        let a: Record = ["collection": "logbook", "id": "q", "fields": [:], "fieldHlc": [:], "deletedHlc": nil, "createdAt": "t",
                         "origin": ["device": "b", "app": "z"]]
        var b = a
        b["origin"] = ["app": "z", "device": "a"]
        XCTAssertEqual(Merge.merge(a, b)["origin"], ["app": "z", "device": "a"])
        XCTAssertEqual(Merge.merge(b, a)["origin"], ["app": "z", "device": "a"])
    }

    func testProfileMapsSplitIntoDottedPathsAndJoinBack() {
        let patch: JSONObject = ["locator": "IO75RL", "conditions": ["nearLondon": true],
                                 "controllerMaps": ["qo100wb": ["ajazz-akp03e": ["pages": []]]]]
        let split = Merge.splitProfile(patch)
        XCTAssertEqual(Set(split.keys), ["locator", "conditions.nearLondon", "controllerMaps.qo100wb.ajazz-akp03e"])
        XCTAssertEqual(Merge.joinProfile(split), patch)
    }

    func testHlcLocalAndReceiveRules() throws {
        let (s1, a) = Hlc.local(wall: 0, count: 0, node: "st_A", now: Hlc.clockFloorMs)
        XCTAssertEqual(a, Hlc.fmt(Hlc.clockFloorMs, 0, "st_A"))
        let (_, b) = Hlc.local(wall: s1.wall, count: s1.count, node: "st_A", now: Hlc.clockFloorMs)
        XCTAssertGreaterThan(b, a)
        let remote = Hlc.fmt(Hlc.clockFloorMs + 7_200_000, 3, "st_B")
        let r = try Hlc.receive(wall: s1.wall, count: s1.count, remote: remote, now: Hlc.clockFloorMs)
        XCTAssertGreaterThan(Hlc.local(wall: r.wall, count: r.count, node: "st_A", now: Hlc.clockFloorMs).1, remote)
        XCTAssertEqual(Hlc.local(wall: 5, count: 0xFFFF, node: "st_A", now: 5).0, Hlc.State(wall: 6, count: 0))
        XCTAssertEqual(Hlc.corrected(0, 0), Hlc.clockFloorMs)
        for bad in ["", "000001a1202d7194-0003", "000001A1202D7194-0003-st_X", "x-y-z"] {
            XCTAssertThrowsError(try Hlc.parse(bad), bad)
        }
        XCTAssertEqual(try Hlc.parse("000001a1202d7194-0003-st_K4M2Q7Z9XW3A"), Hlc.Parts(wall: 0x1a1202d7194, count: 3, node: "st_K4M2Q7Z9XW3A"))
        XCTAssertEqual(Hlc.isoMs(Hlc.clockFloorMs + 1234), "2026-10-09T00:00:01.234Z")
        XCTAssertEqual(try Hlc.parseIsoMs("2026-10-09T01:00:01.234+01:00"), Hlc.clockFloorMs + 1234)
        XCTAssertEqual(try Hlc.parseIsoMs("2026-10-09T00:00:00"), Hlc.clockFloorMs)
        XCTAssertThrowsError(try Hlc.parseIsoMs("yesterday"))
    }

    // MARK: JSON

    func testJsonDumpIsPythonsSortedKeysForm() throws {
        // json.dumps({"b": 1, "a": [True, None, 1.5, "é\n"]}, sort_keys=True)
        XCTAssertEqual(JSON.dump(["b": 1, "a": [true, nil, 1.5, "é\n"]] as JSONObject), "{\"a\": [true, null, 1.5, \"\\u00e9\\n\"], \"b\": 1}")
        XCTAssertEqual(JSON.dump([1e16, 1e-5, 10.0, -0.5] as JSONValue), "[1e+16, 1e-05, 10.0, -0.5]")
        let v = JSON.parseObject("{\"freq_hz\": 10489750123, \"x\": [1, 2.5, \"a\\\"b\"], \"n\": null}")
        XCTAssertEqual(v["freq_hz"], .int(10489750123))
        XCTAssertEqual(v["x"], [1, 2.5, "a\"b"])
        XCTAssertEqual(JSON.parseObject(JSON.dump(v)), v)
        XCTAssertEqual(try JSON.parse("\"\\ud83d\\ude00\""), "😀")
    }

    // MARK: logbook

    func testAddIsIdempotentByUuidAndReportsLikelyDuplicates() throws {
        let st = try station()
        defer { st.close() }
        let a = try st.logbook.add(["call": " dl1abc ", "start": T, "freq_hz": 14_074_000, "mode": "FT8"])
        XCTAssertEqual(a.fields["call"], "DL1ABC")
        XCTAssertEqual(try st.logbook.add(["call": "DL1ABC", "start": T], uuid: a.uuid), a)
        let b = try st.logbook.add(["call": "DL1ABC", "start": "2026-10-09T10:14:00Z", "freq_hz": 14_075_000, "mode": "ft8"])
        XCTAssertEqual(b.duplicateOf, [a.uuid])
        XCTAssertEqual(try st.logbook.workedBefore("dl1abc", band: "20m", mode: "FT8").count, 2)
        XCTAssertEqual(try st.logbook.count(), 2)
    }

    func code(_ f: () throws -> Any) -> String {
        do { _ = try f(); return "no error" } catch let e as ShackError { return e.code } catch { return "\(error)" }
    }

    func testAContactNeedsACallAndAStartAndFieldNamesAreAdif() throws {
        let st = try station()
        defer { st.close() }
        XCTAssertEqual(code { try st.logbook.add(["start": T]) }, "call_required")
        XCTAssertEqual(code { try st.logbook.add(["call": "X"]) }, "start_required")
        XCTAssertEqual(code { try st.logbook.add(["call": "X", "start": "yesterday"]) }, "bad_start")
        XCTAssertEqual(code { try st.logbook.add(["call": "X", "start": T, "RST": "5"]) }, "bad_field_name")
    }

    func testDeleteHidesFromListsButKeepsHistory() throws {
        let st = try station()
        defer { st.close() }
        let q = try st.logbook.add(["call": "DL1ABC", "start": T])
        try st.logbook.update(q.uuid, ["rst_rcvd": "57"])
        try st.logbook.delete(q.uuid)
        XCTAssertTrue(try st.logbook.get(q.uuid)!.deleted)
        XCTAssertTrue(try st.logbook.query().isEmpty)
        XCTAssertEqual(try st.logbook.query(includeDeleted: true).count, 1)
        XCTAssertEqual(try st.logbook.history(q.uuid).map { $0["kind"]!.str }, ["create", "update", "delete"])
    }

    func testAdifRoundTripKeepsIdentityAndReportsDuplicates() throws {
        let a = try station("a"), b = try station("b")
        defer { a.close(); b.close() }
        try a.logbook.add(["call": "DL1ABC", "start": T, "freq_hz": 2_400_250_000, "mode": "SSB", "rst_rcvd": "57"])
        let text = try a.logbook.exportADIF()
        XCTAssertTrue(text.contains("<FREQ:11>2400.250000 ") && text.contains("<QSO_DATE:8>20261009 ") && text.contains("<TIME_ON:6>101230 "), text)
        let r = try b.logbook.importADIF(text)
        XCTAssertEqual(r.added, 1)
        let got = try b.logbook.query().first!
        XCTAssertEqual(got.uuid, try a.logbook.query().first!.uuid)
        XCTAssertEqual(got.fields["freq_hz"], .int(2_400_250_000))
        XCTAssertEqual(Adif.bandFor(got.fields["freq_hz"]), "13cm")
        XCTAssertEqual(try b.logbook.importADIF(text).duplicates.count, 1)
    }

    func testProfileUpdatesNormaliseAndMergePerPath() throws {
        let st = try station()
        defer { st.close() }
        try st.profile.update(["callsign": "gm4bri ", "locator": "io75rl"])
        try st.profile.update(["conditions": ["nearLondon": false]])
        let p = try st.profile.get()
        XCTAssertEqual(p["callsign"], "GM4BRI")
        XCTAssertEqual(p["locator"], "IO75RL")
        XCTAssertEqual(p["conditions"], ["nearLondon": false])
        XCTAssertEqual(code { try st.profile.update(["locator": "ZZ99"]) }, "bad_locator")
    }

    func testUuid7IsAVersion7Uuid() {
        let u = Store.uuid7(Hlc.clockFloorMs)
        let hex = u.replacingOccurrences(of: "-", with: "")
        XCTAssertEqual(Array(hex)[12], "7")
        XCTAssertTrue("89ab".contains(Array(hex)[16]))
        XCTAssertEqual(UInt64(hex.prefix(12), radix: 16), UInt64(Hlc.clockFloorMs))
        XCTAssertNotNil(UUID(uuidString: u))
    }

    func testAnUnpairedStationMakesNoRequestAndSaysSo() throws {
        let st = try station()
        defer { st.close() }
        XCTAssertEqual(try st.sync.runOnce().state, "unpaired")
        XCTAssertNil(st.pairingInfo())
    }

    // MARK: discovery

    let txt: [String: Any] = ["platform": Data("1".utf8), "contract": Data("1".utf8), "id": Data("7f3c2a9e".utf8), "name": Data("GM4BRI".utf8),
                              "version": Data("2.0.0-beta.1".utf8), "kinds": Data("qo100wb,icom".utf8), "path": Data("/.well-known/openhamshack".utf8)]

    func testAControllerAdvert() {
        let c = Discovery.parseAdvert(serviceType: "_openhamshack._tcp.local.", instance: "GM4BRI._openhamshack._tcp.local.",
                                      address: "198.51.100.208", port: 80, txt: txt)!
        XCTAssertEqual([c.name, c.id, c.url], ["GM4BRI", "7f3c2a9e", "http://198.51.100.208:80/"])
        XCTAssertEqual(c.kinds, ["icom", "qo100wb"])
        XCTAssertFalse(c.classic)
        var t = txt
        t["https"] = "443"
        XCTAssertEqual(Discovery.parseAdvert(serviceType: "._openhamshack._tcp", instance: "GM4BRI", address: "198.51.100.208", port: 80, txt: t)!.url,
                       "https://198.51.100.208:443/")
    }

    func testAnOpenhamshackRecordWithoutPlatformIsNotOurs() {
        XCTAssertNil(Discovery.parseAdvert(serviceType: Discovery.service, instance: "x", address: "10.0.0.1", port: 80, txt: ["name": "x"]))
        XCTAssertNil(Discovery.parseAdvert(serviceType: "_http._tcp", instance: "x", address: "10.0.0.1", port: 80, txt: txt))
    }

    func testAClassicBoxIsOneQo100wbApp() {
        let c = Discovery.parseAdvert(serviceType: Discovery.legacy + ".local.", instance: "OpenHamShack._openhamshack._tcp.local.", address: "198.51.100.50",
                                      port: 8080, txt: ["name": "OpenHamShack workshop", "version": "0.9.14", "port": "8080"])!
        XCTAssertTrue(c.classic)
        XCTAssertEqual(c.kinds, ["qo100wb"])
        XCTAssertEqual(c.url, "http://198.51.100.50:8080/")
        XCTAssertEqual(c.name, "OpenHamShack workshop")
    }

    func testTheOpenhamshackRecordWinsOverTheDraftLegacyName() {
        let draft = Discovery.parseAdvert(serviceType: Discovery.legacy, instance: "GM4BRI", address: "198.51.100.208", port: 80, txt: txt)!
        let real = Discovery.parseAdvert(serviceType: Discovery.service, instance: "GM4BRI", address: "198.51.100.208", port: 80, txt: txt)!
        XCTAssertTrue(draft.service == Discovery.legacy && !draft.classic)
        XCTAssertEqual(Discovery.mergeAdverts([draft, real]), [real])
        XCTAssertEqual(Discovery.mergeAdverts([real, draft]), [real])
        let classic = Discovery.parseAdvert(serviceType: Discovery.legacy, instance: "Box", address: "198.51.100.208", port: 80, txt: ["name": "Box"])!
        XCTAssertEqual(Discovery.mergeAdverts([draft, real, classic]).count, 2)
        XCTAssertFalse(Discovery.mergeAdverts([draft, real, classic]).contains(draft))
    }

    func testUrlsAreRelativeToTheBase() {
        XCTAssertEqual(URLs.join("http://h/shack", "api/shack/token"), "http://h/shack/api/shack/token")
        XCTAssertEqual(URLs.join("http://h/shack/", "/api/x", query: [("since", "0"), ("epoch", nil), ("collections", "logbook,profile")]),
                       "http://h/shack/api/x?since=0&collections=logbook%2Cprofile")
    }
}
