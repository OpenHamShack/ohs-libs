import Foundation

/// A JSON value. Records are open bags of fields (SYNC.md §4: a field this
/// library does not know is kept and sent back, T19), so they are held as
/// JSON rather than as typed structs, the way the Python reference holds
/// dicts.
///
/// Parsed and written here rather than through JSONSerialization or
/// Codable, for two reasons. Integers must stay integers (`freq_hz` is
/// integer Hz; NSNumber blurs 57 and 57.0 and true and 1). And `dump` must
/// produce exactly Python's `json.dumps(value, sort_keys=True)`: the merge
/// breaks a createdAt tie on the origin's sorted JSON, and the controller a
/// Swift client converges with is Python, so the two encodings must agree
/// byte for byte or the same two copies would merge to different origins.
public enum JSONValue: Equatable, Hashable, CustomStringConvertible {
    case null
    case bool(Bool)
    case int(Int64)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public var description: String { JSON.dump(self) }

    public var stringValue: String? { if case .string(let s) = self { return s }; return nil }
    public var str: String { stringValue ?? "" }
    public var objectValue: [String: JSONValue]? { if case .object(let o) = self { return o }; return nil }
    public var obj: [String: JSONValue] { objectValue ?? [:] }
    public var arr: [JSONValue] { if case .array(let a) = self { return a }; return [] }
    public var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    public var isNull: Bool { if case .null = self { return true }; return false }
    public var int64Value: Int64? {
        switch self {
        case .int(let i): return i
        case .double(let d): return Int64(exactly: d)
        case .string(let s): return Int64(s)
        default: return nil
        }
    }
    public var doubleValue: Double? {
        switch self {
        case .int(let i): return Double(i)
        case .double(let d): return d
        default: return nil
        }
    }

    public subscript(key: String) -> JSONValue {
        if case .object(let o) = self { return o[key] ?? .null }
        return .null
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByBooleanLiteral,
    ExpressibleByFloatLiteral, ExpressibleByNilLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral {
    public init(stringLiteral value: String) { self = .string(value) }
    public init(integerLiteral value: Int64) { self = .int(value) }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(floatLiteral value: Double) { self = .double(value) }
    public init(nilLiteral: ()) { self = .null }
    public init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
    public init(dictionaryLiteral elements: (String, JSONValue)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { _, b in b }))
    }
}

public typealias JSONObject = [String: JSONValue]

public enum JSON {
    public struct ParseError: Error, CustomStringConvertible {
        public let description: String
    }

    public static func parse(_ text: String) throws -> JSONValue {
        var p = Parser(Array(text.utf8))
        p.ws()
        let v = try p.value()
        p.ws()
        if p.i != p.b.count { throw ParseError(description: "unexpected text at offset \(p.i)") }
        return v
    }

    public static func parse(data: Data) throws -> JSONValue {
        try parse(String(decoding: data, as: UTF8.self))
    }

    /// An object, or empty for anything else.
    public static func parseObject(_ text: String) -> JSONObject {
        ((try? parse(text))?.objectValue) ?? [:]
    }

    /// Python's `json.dumps(v, sort_keys=True)`: ", " and ": " separators,
    /// keys sorted by code point, non-ASCII escaped as \uXXXX (UTF-16).
    public static func dump(_ v: JSONValue) -> String {
        var out = ""
        write(&out, v)
        return out
    }

    public static func dump(_ o: JSONObject) -> String { dump(.object(o)) }

    private static func write(_ out: inout String, _ v: JSONValue) {
        switch v {
        case .null: out += "null"
        case .bool(let b): out += b ? "true" : "false"
        case .int(let i): out += String(i)
        case .double(let d): out += pyFloat(d)
        case .string(let s): str(&out, s)
        case .array(let a):
            out += "["
            for (n, x) in a.enumerated() {
                if n > 0 { out += ", " }
                write(&out, x)
            }
            out += "]"
        case .object(let o):
            out += "{"
            // Python sorts str keys by code point: compare unicode scalars.
            let keys = o.keys.sorted { $0.unicodeScalars.lexicographicallyPrecedes($1.unicodeScalars) }
            for (n, k) in keys.enumerated() {
                if n > 0 { out += ", " }
                str(&out, k)
                out += ": "
                write(&out, o[k]!)
            }
            out += "}"
        }
    }

    /// Python's float repr: shortest round-trip digits (Swift's description
    /// gives the same digits), `.0` on integral values, exponent form below
    /// 1e-4 and from 1e16 written as Python writes it (`1e+16`, `1e-05`).
    static func pyFloat(_ d: Double) -> String {
        precondition(d.isFinite, "\(d) is not a JSON number")
        let a = abs(d)
        if a != 0 && (a < 1e-4 || a >= 1e16) {
            // Digits and exponent from Swift's shortest representation.
            let s = "\(a)"   // e.g. "1e-05" or "1.5e+16" or "1e+16"
            var mant = s
            var exp = 0
            if let e = s.firstIndex(where: { $0 == "e" || $0 == "E" }) {
                mant = String(s[s.startIndex..<e])
                exp = Int(s[s.index(after: e)...])!
            }
            // Normalise to one digit before the point.
            var digits = mant.replacingOccurrences(of: ".", with: "")
            let point = mant.firstIndex(of: ".").map { mant.distance(from: mant.startIndex, to: $0) } ?? mant.count
            while digits.hasPrefix("0") && digits.count > 1 { digits.removeFirst(); exp -= 1 }
            exp += point - 1
            while digits.hasSuffix("0") && digits.count > 1 { digits.removeLast() }
            let m = digits.count == 1 ? digits : String(digits.first!) + "." + digits.dropFirst()
            let e = String(format: "%02d", abs(exp))
            return (d < 0 ? "-" : "") + m + "e" + (exp < 0 ? "-" : "+") + e
        }
        var s = "\(d)"
        if s.contains("e") {
            // Swift writes 1e+15 style for some values Python writes plainly.
            s = Decimal(string: s).map { "\($0)" } ?? s
        }
        return s.contains(".") ? s : s + ".0"
    }

    private static func str(_ out: inout String, _ s: String) {
        out += "\""
        for u in s.utf16 {
            switch u {
            case 0x22: out += "\\\""
            case 0x5C: out += "\\\\"
            case 0x0A: out += "\\n"
            case 0x0D: out += "\\r"
            case 0x09: out += "\\t"
            case 0x08: out += "\\b"
            case 0x0C: out += "\\f"
            case 0x20...0x7F: out.unicodeScalars.append(Unicode.Scalar(u)!)
            default:
                let h = String(u, radix: 16)
                out += "\\u" + String(repeating: "0", count: 4 - h.count) + h
            }
        }
        out += "\""
    }

    private struct Parser {
        let b: [UInt8]
        var i = 0
        init(_ b: [UInt8]) { self.b = b }

        mutating func ws() {
            while i < b.count, b[i] == 0x20 || b[i] == 0x0A || b[i] == 0x0D || b[i] == 0x09 { i += 1 }
        }

        mutating func value() throws -> JSONValue {
            guard i < b.count else { throw ParseError(description: "unexpected end of JSON") }
            switch b[i] {
            case UInt8(ascii: "{"): return try object()
            case UInt8(ascii: "["): return try array()
            case UInt8(ascii: "\""): return .string(try string())
            case UInt8(ascii: "t"): return try lit("true", .bool(true))
            case UInt8(ascii: "f"): return try lit("false", .bool(false))
            case UInt8(ascii: "n"): return try lit("null", .null)
            case UInt8(ascii: "-"), UInt8(ascii: "0")...UInt8(ascii: "9"): return try number()
            default: throw ParseError(description: "unexpected byte at offset \(i)")
            }
        }

        mutating func lit(_ word: String, _ v: JSONValue) throws -> JSONValue {
            let w = Array(word.utf8)
            guard i + w.count <= b.count, Array(b[i..<i + w.count]) == w else {
                throw ParseError(description: "unexpected text at offset \(i)")
            }
            i += w.count
            return v
        }

        mutating func object() throws -> JSONValue {
            var out: JSONObject = [:]
            i += 1
            ws()
            if i < b.count, b[i] == UInt8(ascii: "}") { i += 1; return .object(out) }
            while true {
                ws()
                guard i < b.count, b[i] == UInt8(ascii: "\"") else { throw ParseError(description: "expected a key at offset \(i)") }
                let k = try string()
                ws()
                guard i < b.count, b[i] == UInt8(ascii: ":") else { throw ParseError(description: "expected ':' at offset \(i)") }
                i += 1
                ws()
                out[k] = try value()
                ws()
                if i < b.count, b[i] == UInt8(ascii: ",") { i += 1; continue }
                if i < b.count, b[i] == UInt8(ascii: "}") { i += 1; return .object(out) }
                throw ParseError(description: "expected ',' or '}' at offset \(i)")
            }
        }

        mutating func array() throws -> JSONValue {
            var out: [JSONValue] = []
            i += 1
            ws()
            if i < b.count, b[i] == UInt8(ascii: "]") { i += 1; return .array(out) }
            while true {
                ws()
                out.append(try value())
                ws()
                if i < b.count, b[i] == UInt8(ascii: ",") { i += 1; continue }
                if i < b.count, b[i] == UInt8(ascii: "]") { i += 1; return .array(out) }
                throw ParseError(description: "expected ',' or ']' at offset \(i)")
            }
        }

        mutating func hex4() throws -> UInt16 {
            guard i + 4 <= b.count, let v = UInt16(String(decoding: b[i..<i + 4], as: UTF8.self), radix: 16) else {
                throw ParseError(description: "bad \\u escape at offset \(i)")
            }
            i += 4
            return v
        }

        mutating func string() throws -> String {
            i += 1
            var units: [UInt16] = []
            var run: [UInt8] = []
            func flush() { units.append(contentsOf: String(decoding: run, as: UTF8.self).utf16); run.removeAll() }
            while true {
                guard i < b.count else { throw ParseError(description: "unterminated string") }
                let c = b[i]
                i += 1
                if c == UInt8(ascii: "\"") { flush(); return String(decoding: units, as: UTF16.self) }
                if c != UInt8(ascii: "\\") { run.append(c); continue }
                flush()
                guard i < b.count else { throw ParseError(description: "unterminated string") }
                let e = b[i]
                i += 1
                switch e {
                case UInt8(ascii: "\""): units.append(0x22)
                case UInt8(ascii: "\\"): units.append(0x5C)
                case UInt8(ascii: "/"): units.append(0x2F)
                case UInt8(ascii: "b"): units.append(0x08)
                case UInt8(ascii: "f"): units.append(0x0C)
                case UInt8(ascii: "n"): units.append(0x0A)
                case UInt8(ascii: "r"): units.append(0x0D)
                case UInt8(ascii: "t"): units.append(0x09)
                case UInt8(ascii: "u"): units.append(try hex4())
                default: throw ParseError(description: "bad escape at offset \(i)")
                }
            }
        }

        mutating func number() throws -> JSONValue {
            let start = i
            if b[i] == UInt8(ascii: "-") { i += 1 }
            func digits() { while i < b.count, b[i] >= 0x30, b[i] <= 0x39 { i += 1 } }
            digits()
            var real = false
            if i < b.count, b[i] == UInt8(ascii: ".") { real = true; i += 1; digits() }
            if i < b.count, b[i] == UInt8(ascii: "e") || b[i] == UInt8(ascii: "E") {
                real = true
                i += 1
                if i < b.count, b[i] == UInt8(ascii: "+") || b[i] == UInt8(ascii: "-") { i += 1 }
                digits()
            }
            let lit = String(decoding: b[start..<i], as: UTF8.self)
            if !real, let n = Int64(lit) { return .int(n) }
            guard let d = Double(lit) else { throw ParseError(description: "bad number at offset \(start)") }
            return .double(d)
        }
    }
}
