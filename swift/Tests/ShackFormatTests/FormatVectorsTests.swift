import Foundation
import ShackClient
import XCTest
@testable import ShackFormat

/// spec/vectors/i18n/format.json (I18N.md §5 item 3), read from the checkout
/// and never copied: the Python, Kotlin, Swift and JS helpers must give
/// exactly these results, byte for byte. An expected `error` is a machine
/// code; the English sentence is not compared.
final class FormatVectorsTests: XCTestCase {
    static let spec = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("spec")

    lazy var vectors: JSONObject = {
        JSON.parseObject(try! String(contentsOf: Self.spec.appendingPathComponent("vectors/i18n/format.json"), encoding: .utf8))
    }()

    func cases(_ section: String) -> [JSONObject] { vectors[section]?.arr.map(\.obj) ?? [] }

    func value(_ v: JSONValue?) -> ShackFormat.Value {
        switch v {
        case .int(let i)?: return .int(i)
        case .double(let d)?: return .double(d)
        default: XCTFail("not a number: \(String(describing: v))"); return .int(0)
        }
    }

    /// Runs every case of a section; returns how many ran.
    @discardableResult
    func run(_ section: String, want: String, _ call: (JSONObject) throws -> JSONValue) -> Int {
        let cs = cases(section)
        for c in cs {
            let got: JSONValue
            do {
                got = try call(c)
            } catch let e as ShackFormat.FormatError {
                got = .string("error:" + e.code)
            } catch {
                got = .string("unexpected: \(error)")
            }
            let expected: JSONValue = c["error"].map { .string("error:" + $0.str) } ?? c[want]!
            XCTAssertEqual(got, expected, "\(section) \(JSON.dump(c))")
        }
        return cs.count
    }

    func testFrequency() {
        XCTAssertGreaterThan(run("frequency", want: "text") { c in
            .string(try ShackFormat.frequency(value(c["hz"]), locale: c["locale"]!.str, unit: c["unit"]!.str,
                                              decimals: c["decimals"]?.int64Value.map { Int($0) }))
        }, 0)
    }

    func testOffset() {
        XCTAssertGreaterThan(run("offset", want: "text") { c in
            .string(try ShackFormat.offset(value(c["hz"]), unit: c["unit"]!.str, locale: c["locale"]!.str,
                                           decimals: c["decimals"]?.int64Value.map { Int($0) }))
        }, 0)
    }

    func testParseFrequency() {
        XCTAssertGreaterThan(run("parse_frequency", want: "hz") { c in
            .int(try ShackFormat.parseFrequency(c["text"]!.str, locale: c["locale"]!.str, unit: c["unit"]?.stringValue))
        }, 0)
    }

    func testNumber() {
        XCTAssertGreaterThan(run("number", want: "text") { c in
            .string(try ShackFormat.number(value(c["x"]), locale: c["locale"]!.str, decimals: Int(c["decimals"]!.int64Value!)))
        }, 0)
    }

    func testSwr() {
        XCTAssertGreaterThan(run("swr", want: "text") { c in
            .string(try ShackFormat.swr(value(c["x"]), locale: c["locale"]!.str))
        }, 0)
    }

    func testTheVectorsAreTheVersionThisPortWasWrittenFor() {
        XCTAssertEqual(vectors["version"], .int(1))
        // Every section has cases: a renamed section must not pass silently.
        for s in ["frequency", "offset", "parse_frequency", "number", "swr"] { XCTAssertFalse(cases(s).isEmpty, s) }
    }

    func testAFoundationLocaleFindsItsSymbols() {
        XCTAssertEqual(Symbols.of(Locale(identifier: "de_DE").identifier).decimal, ",")
        XCTAssertEqual(Symbols.of("de_CH.UTF-8@euro").group, "\u{2019}")
    }
}
