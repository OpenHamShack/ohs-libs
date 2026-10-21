import Foundation

/// ADIF 3.1.4 import and export for the logbook (SERVICES.md §3.3), a port
/// of python/shack_client adif.py.
///
/// Stored QSO fields are ADIF names in lower case, so most of the mapping is
/// "upper-case the name". The exceptions are the timestamps (`start` and
/// `end` replace QSO_DATE+TIME_ON and QSO_DATE_OFF+TIME_OFF), the two
/// frequencies (integer Hz on the wire, MHz with six decimals in ADIF) and
/// the record's identity (APP_SHACK_UUID, APP_SHACK_ORIGIN), written on
/// every record so a file exported here and imported elsewhere is the same
/// contacts, not copies.
public enum Adif {
    /// ADIF 3.1.4's Band enumeration in Hz: a QSO's `band` when it has a
    /// frequency and no band. The same table as data/bandplan/vocab.json
    /// "bands" and adif.py; carried here so the client needs no band-plan
    /// dependency.
    public static let bands: [(String, Int64, Int64)] = [
        ("2190m", 135_700, 137_800), ("630m", 472_000, 479_000), ("560m", 501_000, 504_000),
        ("160m", 1_800_000, 2_000_000), ("80m", 3_500_000, 4_000_000), ("60m", 5_060_000, 5_450_000),
        ("40m", 7_000_000, 7_300_000), ("30m", 10_100_000, 10_150_000), ("20m", 14_000_000, 14_350_000),
        ("17m", 18_068_000, 18_168_000), ("15m", 21_000_000, 21_450_000), ("12m", 24_890_000, 24_990_000),
        ("10m", 28_000_000, 29_700_000), ("8m", 40_000_000, 45_000_000), ("6m", 50_000_000, 54_000_000),
        ("5m", 54_000_001, 69_900_000), ("4m", 70_000_000, 71_000_000), ("2m", 144_000_000, 148_000_000),
        ("1.25m", 222_000_000, 225_000_000), ("70cm", 420_000_000, 450_000_000), ("33cm", 902_000_000, 928_000_000),
        ("23cm", 1_240_000_000, 1_300_000_000), ("13cm", 2_300_000_000, 2_450_000_000),
        ("9cm", 3_300_000_000, 3_500_000_000), ("6cm", 5_650_000_000, 5_925_000_000),
        ("3cm", 10_000_000_000, 10_500_000_000), ("1.25cm", 24_000_000_000, 24_250_000_000),
        ("6mm", 47_000_000_000, 47_200_000_000), ("4mm", 75_500_000_000, 81_000_000_000),
        ("2.5mm", 119_980_000_000, 123_000_000_000), ("2mm", 134_000_000_000, 149_000_000_000),
        ("1mm", 241_000_000_000, 250_000_000_000),
    ]

    public static func bandFor(_ hz: JSONValue?) -> String {
        guard let hz = hz else { return "" }
        let f: Int64?
        switch hz {
        case .int(let i): f = i
        case .double(let d): f = Int64(exactly: d.rounded(.towardZero))
        case .string(let s): f = Int64(s.trimmingCharacters(in: .whitespaces))
        default: f = nil
        }
        guard let v = f else { return "" }
        return bands.first { v >= $0.1 && v <= $0.2 }?.0 ?? ""
    }

    /// (line number, [FIELD: value]) for every record of an .adi file.
    /// Field lengths are honoured, so a value may contain '<' or a newline.
    /// Lengths count characters, as the Python library counts them.
    public static func parse(_ text: String) -> [(Int, [String: String])] {
        let chars = Array(text)
        var i = 0
        // Skip the header, if there is one.
        let lower = text.lowercased()
        if let r = lower.range(of: "<eoh>") {
            i = lower.distance(from: lower.startIndex, to: r.upperBound)
        } else if !text.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("<") {
            return []
        }
        var out: [(Int, [String: String])] = []
        var cur: [String: String] = [:]
        var curLine: Int? = nil
        while i < chars.count {
            guard let lt = chars[i...].firstIndex(of: "<") else { break }
            guard let gt = chars[lt...].firstIndex(of: ">") else { break }
            let spec = String(chars[(lt + 1)..<gt])
            let parts = spec.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
            let nameOK = !parts[0].isEmpty && parts[0].unicodeScalars.allSatisfy { ($0.isASCII && CharacterSet.alphanumerics.contains($0)) || $0 == "_" }
            guard nameOK, parts.count <= 3, parts.count < 2 || Int(parts[1]) != nil else { i = lt + 1; continue }
            let name = parts[0].uppercased()
            if name == "EOR" {
                if !cur.isEmpty { out.append((curLine ?? 1, cur)) }
                cur = [:]
                curLine = nil
                i = gt + 1
                continue
            }
            guard parts.count >= 2, let n = Int(parts[1]) else { i = gt + 1; continue }
            let from = gt + 1
            let to = min(chars.count, from + n)
            if curLine == nil { curLine = chars[0..<lt].filter { $0 == "\n" }.count + 1 }
            cur[name] = String(chars[from..<to])
            i = to
        }
        return out
    }

    private static func iso(_ date: String, _ time: String) throws -> String {
        var t = time.isEmpty ? "0000" : time
        while t.count < 6 { t += "0" }
        t = String(t.prefix(6))
        let s = date + t
        guard s.count == 14, s.allSatisfy(\.isASCII), s.allSatisfy(\.isNumber) else {
            throw ShackError(code: "bad_adif_date", message: "'\(date) \(time)' is not an ADIF date and time")
        }
        let c = Array(s)
        let iso = "\(String(c[0..<4]))-\(String(c[4..<6]))-\(String(c[6..<8]))T\(String(c[8..<10])):\(String(c[10..<12])):\(String(c[12..<14]))Z"
        _ = try Hlc.parseIsoMs(iso)
        return iso
    }

    /// An ADIF record as stored fields, plus its APP_SHACK_UUID and
    /// APP_SHACK_ORIGIN (empty when absent).
    public static func toFields(_ adif: [String: String]) throws -> (JSONObject, String, String) {
        var a = adif
        var out: JSONObject = [:]
        let uid = (a.removeValue(forKey: "APP_SHACK_UUID") ?? "").lowercased()
        let origin = a.removeValue(forKey: "APP_SHACK_ORIGIN") ?? ""
        let date = a.removeValue(forKey: "QSO_DATE") ?? ""
        let on = a.removeValue(forKey: "TIME_ON") ?? ""
        let dateOff = a.removeValue(forKey: "QSO_DATE_OFF") ?? ""
        let off = a.removeValue(forKey: "TIME_OFF") ?? ""
        if !date.isEmpty { out["start"] = .string(try iso(date, on)) }
        if !off.isEmpty {
            // TIME_OFF without QSO_DATE_OFF ends on the start date.
            let d = dateOff.isEmpty ? date : dateOff
            if !d.isEmpty { out["end"] = .string(try iso(d, off)) }
        }
        for (src, dst) in [("FREQ", "freq_hz"), ("FREQ_RX", "freq_rx_hz")] {
            guard let v = a.removeValue(forKey: src) else { continue }
            if let mhz = Decimal(string: v.trimmingCharacters(in: .whitespaces), locale: Locale(identifier: "en_US_POSIX")) {
                var hz = mhz * 1_000_000
                var r = Decimal()
                NSDecimalRound(&r, &hz, 0, .bankers)
                out[dst] = .int(NSDecimalNumber(decimal: r).int64Value)
            } else {
                out[String(dst.dropLast(3))] = .string(v)
            }
        }
        for (k, v) in a { out[k.lowercased()] = .string(v) }
        return (out, uid, origin)
    }

    private static func field(_ name: String, _ value: String) -> String { "<\(name):\(value.count)>\(value) " }

    private static func text(_ v: JSONValue) -> String {
        switch v {
        case .string(let s): return s
        case .null: return ""
        default: return JSON.dump(v)
        }
    }

    private static func dateTime(_ iso: String) throws -> (String, String) {
        let ms = try Hlc.parseIsoMs(iso)
        let s = Hlc.isoMs(ms)   // yyyy-MM-ddTHH:mm:ss.SSSZ in UTC
        let c = Array(s)
        return (String(c[0..<4]) + String(c[5..<7]) + String(c[8..<10]), String(c[11..<13]) + String(c[14..<16]) + String(c[17..<19]))
    }

    /// ADIF text for live QSO records (wire shape).
    public static func write(_ records: [Record], programVersion: String = "") -> String {
        var out = "OpenHamShack logbook export\n" + field("ADIF_VER", "3.1.4") + field("PROGRAMID", "OpenHamShack")
        if !programVersion.isEmpty { out += field("PROGRAMVERSION", programVersion) }
        out += "<EOH>\n"
        for rec in records {
            var f = Merge.fields(rec)
            var parts = ""
            if let start = f.removeValue(forKey: "start")?.stringValue, !start.isEmpty, let dt = try? dateTime(start) {
                parts += field("QSO_DATE", dt.0) + field("TIME_ON", dt.1)
            }
            if let end = f.removeValue(forKey: "end")?.stringValue, !end.isEmpty, let dt = try? dateTime(end) {
                parts += field("QSO_DATE_OFF", dt.0) + field("TIME_OFF", dt.1)
            }
            for (src, dst) in [("freq_hz", "FREQ"), ("freq_rx_hz", "FREQ_RX")] {
                if let v = f.removeValue(forKey: src), let hz = v.int64Value {
                    let whole = hz / 1_000_000, frac = abs(hz % 1_000_000)
                    let fs = String(frac)
                    parts += field(dst, "\(whole).\(String(repeating: "0", count: 6 - fs.count))\(fs)")
                }
            }
            for k in f.keys.sorted() {
                let v = f[k]!
                if v.isNull || v == .string("") { continue }
                parts += field(k.uppercased(), text(v))
            }
            parts += field("APP_SHACK_UUID", rec["id"]?.str ?? "")
            if let app = rec["origin"]?["app"].stringValue, !app.isEmpty { parts += field("APP_SHACK_ORIGIN", app) }
            out += parts + "<EOR>\n"
        }
        return out
    }
}
