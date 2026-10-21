import Foundation

/// Hybrid logical clocks (SYNC.md §3; Kulkarni et al., 2014), a port of
/// python/shack_client hlc.py.
///
/// A stamp orders by plain string comparison: wall time, then count, then
/// node. After a store has seen an edit, everything it does next sorts after
/// it, whatever its own clock says (the receive rule), and two edits never
/// share a stamp (the node differs, or the count rises).
///
///     000001a1202d7194-0003-st_K4M2Q7Z9XW3A
///     wall ms, 16 hex   count  store id (node)
///
/// State lives with the caller (the store keeps it in its meta table,
/// inside the transaction of the edit it stamps): these are pure functions.
public enum Hlc {
    /// No stamp is ever earlier than the library's build date, so a device
    /// with no RTC that boots believing it is 1970 (§3.3, T9) still stamps
    /// after every edit made before this library existed. The same date as
    /// the Python and Kotlin libraries'; raise all three with each release.
    public static let clockFloorMs: Int64 = 1_791_504_000_000   // 2026-10-09T00:00:00Z

    /// The controller refuses edits more than this far ahead of its clock
    /// (§3.3): a stamp days in the future would beat every real edit for days.
    public static let maxAheadMs: Int64 = 10 * 60 * 1000

    public struct HlcError: Error, CustomStringConvertible {
        public let description: String
    }

    public struct Parts: Equatable {
        public let wall: Int64
        public let count: Int
        public let node: String
    }

    public struct State: Equatable {
        public let wall: Int64
        public let count: Int
        public init(wall: Int64, count: Int) { self.wall = wall; self.count = count }
    }

    public static func fmt(_ wall: Int64, _ count: Int, _ node: String) -> String {
        let w = String(wall, radix: 16)
        let c = String(count, radix: 16)
        return String(repeating: "0", count: max(0, 16 - w.count)) + w + "-" +
            String(repeating: "0", count: max(0, 4 - c.count)) + c + "-" + node
    }

    private static func isHex(_ s: Substring) -> Bool { s.utf8.allSatisfy { ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x61 && $0 <= 0x66) } }

    public static func parse(_ s: String?) throws -> Parts {
        let s = s ?? ""
        let parts = s.split(separator: "-", maxSplits: 2, omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 16, parts[1].count == 4, isHex(parts[0]), isHex(parts[1]),
              !parts[2].isEmpty,
              parts[2].unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) && $0.isASCII || $0 == "_" }),
              let w = Int64(parts[0], radix: 16), let c = Int(parts[1], radix: 16)
        else { throw HlcError(description: "'\(s)' is not a hybrid logical clock") }
        return Parts(wall: w, count: c, node: String(parts[2]))
    }

    public static func wallOf(_ s: String?) -> Int64 {
        guard let s = s, !s.isEmpty else { return 0 }
        return (try? parse(s).wall) ?? 0
    }

    public static func nodeOf(_ s: String) -> String {
        s.range(of: "-", options: .backwards).map { String(s[$0.upperBound...]) } ?? s
    }

    public static func systemMs() -> Int64 { Int64((Date().timeIntervalSince1970 * 1000).rounded(.down)) }

    /// `now()` of §3.2: the system clock plus the measured offset, never
    /// below the clock floor.
    public static func corrected(_ nowMs: Int64, _ offsetMs: Int64) -> Int64 { max(nowMs + offsetMs, clockFloorMs) }

    /// A local edit: the new state and its stamp.
    public static func local(wall: Int64, count: Int, node: String, now: Int64) -> (State, String) {
        var w = max(wall, now)
        var c = w == wall ? count + 1 : 0
        if c > 0xFFFF {
            // 65536 edits within one millisecond of one store: move the wall
            // on rather than wrap the count, which would break the ordering.
            w += 1
            c = 0
        }
        return (State(wall: w, count: c), fmt(w, c, node))
    }

    /// Seeing a remote stamp: everything this store does next is later.
    public static func receive(wall: Int64, count: Int, remote: String, now: Int64) throws -> State {
        let r = try parse(remote)
        var w = max(wall, r.wall, now)
        var c: Int
        if w == wall && w == r.wall { c = max(count, r.count) + 1 }
        else if w == wall { c = count + 1 }
        else if w == r.wall { c = r.count + 1 }
        else { c = 0 }
        if c > 0xFFFF { w += 1; c = 0 }
        return State(wall: w, count: c)
    }

    /// ISO 8601 UTC with milliseconds and Z (SERVICES.md §1).
    public static func isoMs(_ ms: Int64) -> String {
        let secs = ms >= 0 ? ms / 1000 : (ms - 999) / 1000
        let milli = ms - secs * 1000
        var t = time_t(secs)
        var tm = tm()
        gmtime_r(&t, &tm)
        return String(format: "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", Int(tm.tm_year) + 1900, Int(tm.tm_mon) + 1, Int(tm.tm_mday),
                      Int(tm.tm_hour), Int(tm.tm_min), Int(tm.tm_sec), Int(milli))
    }

    /// Milliseconds from an ISO 8601 date and time. A time without an
    /// offset is UTC, as in the Python library's `parse_iso_ms`.
    public static func parseIsoMs(_ s: String) throws -> Int64 {
        let t = s.trimmingCharacters(in: .whitespaces)
        // YYYY-MM-DD[THH:MM[:SS[.fff]]][Z|±HH:MM]
        let b = Array(t.utf8)
        func num(_ from: Int, _ n: Int) -> Int? {
            guard from + n <= b.count else { return nil }
            var v = 0
            for k in from..<from + n {
                guard b[k] >= 0x30, b[k] <= 0x39 else { return nil }
                v = v * 10 + Int(b[k] - 0x30)
            }
            return v
        }
        let bad = HlcError(description: "'\(s)' is not an ISO date and time")
        guard let y = num(0, 4), b.count >= 10, b[4] == 0x2D, let mo = num(5, 2), b[7] == 0x2D, let d = num(8, 2),
              (1...12).contains(mo), (1...31).contains(d) else { throw bad }
        var i = 10
        var h = 0, mi = 0, sec = 0
        var frac = 0.0
        if i < b.count, b[i] == UInt8(ascii: "T") || b[i] == UInt8(ascii: " ") {
            guard let hh = num(i + 1, 2), i + 3 < b.count, b[i + 3] == 0x3A, let mm = num(i + 4, 2) else { throw bad }
            h = hh; mi = mm; i += 6
            if i < b.count, b[i] == 0x3A {
                guard let ss = num(i + 1, 2) else { throw bad }
                sec = ss; i += 3
                if i < b.count, b[i] == UInt8(ascii: ".") || b[i] == UInt8(ascii: ",") {
                    var j = i + 1
                    while j < b.count, b[j] >= 0x30, b[j] <= 0x39 { j += 1 }
                    guard j > i + 1, let f = Double("0." + String(decoding: b[(i + 1)..<j], as: UTF8.self)) else { throw bad }
                    frac = f; i = j
                }
            }
            guard h < 24, mi < 60, sec < 61 else { throw bad }
        }
        var offset = 0
        if i < b.count {
            if b[i] == UInt8(ascii: "Z") || b[i] == UInt8(ascii: "z") {
                i += 1
            } else if b[i] == UInt8(ascii: "+") || b[i] == UInt8(ascii: "-") {
                let sign = b[i] == UInt8(ascii: "-") ? -1 : 1
                guard let oh = num(i + 1, 2) else { throw bad }
                var j = i + 3
                if j < b.count, b[j] == 0x3A { j += 1 }
                guard let om = num(j, 2) else { throw bad }
                offset = sign * (oh * 3600 + om * 60)
                i = j + 2
            }
        }
        guard i == b.count else { throw bad }
        var comps = DateComponents()
        comps.year = y; comps.month = mo; comps.day = d; comps.hour = h; comps.minute = mi; comps.second = sec
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        guard let date = cal.date(from: comps), cal.component(.day, from: date) == d else { throw bad }
        let ms = (date.timeIntervalSince1970 - Double(offset)) * 1000 + (frac * 1000).rounded()
        return Int64(ms.rounded())
    }
}
