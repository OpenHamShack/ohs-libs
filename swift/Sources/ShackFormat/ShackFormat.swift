import Foundation

/// Locale formatting for frequencies and numbers (I18N.md §4), the Swift
/// port of python/shack_i18n fmt.py. spec/vectors/i18n/format.json is the
/// contract between the ports; this is one implementation of it.
///
/// Decisions the vectors pin, so a port cannot quietly differ:
///
/// - Arithmetic is decimal (Foundation's Decimal), never binary floating
///   point, and ties round half away from zero (`.plain` rounding). A
///   Double argument is read through its shortest decimal form first
///   (`description`), so `number(2.675, decimals: 2)` is `2.68` as a person
///   expects.
/// - Frequencies are never grouped: "10,489.750" and "10.489,750" each read
///   as the other in half the world (§4.1).
/// - The unit follows a no-break space so a line break can never separate a
///   number from its unit.
///
/// The decimal, group and minus symbols come from the same CLDR-derived
/// table as the Python and Kotlin libraries (`Symbols`), not from
/// `Locale.decimalSeparator`: the platform's CLDR changes with the OS
/// version, so two devices in one shack could format one number two ways
/// and the shared vectors could not pin it. A table that moves with the
/// library is one answer everywhere.
public enum ShackFormat {
    /// A value the helpers refuse. `code` is the machine code the vectors
    /// compare and a UI translates through its catalog; `message` is an
    /// English fallback.
    public struct FormatError: Error, CustomStringConvertible {
        public let code: String
        public let message: String
        public var description: String { message }
    }

    /// A number in any of the forms an app holds one.
    public enum Value {
        case int(Int64)
        case double(Double)
        case decimal(Decimal)
    }

    public static let units: [String: Int64] = ["Hz": 1, "kHz": 1_000, "MHz": 1_000_000, "GHz": 1_000_000_000]
    static let unitOrder = ["Hz", "kHz", "MHz", "GHz"]
    // The most decimals a unit can show is its Hz resolution.
    static let maxDecimals: [String: Int] = ["Hz": 0, "kHz": 3, "MHz": 6, "GHz": 9]
    // §4.1: MHz 3 by default (kHz resolution), kHz 1; GHz keeps kHz resolution.
    static let defaultDecimals: [String: Int] = ["Hz": 0, "kHz": 1, "MHz": 3, "GHz": 6]
    static let autoMHzFrom: Int64 = 30_000_000
    /// §4.1 fixes U+2212 for signed shifts in every locale, unlike number(),
    /// which takes the locale's own minus sign.
    static let shiftMinus = "\u{2212}"
    static let nbsp = "\u{00A0}"
    static let posix = Locale(identifier: "en_US_POSIX")

    static func decimal(_ v: Value) throws -> Decimal {
        switch v {
        case .int(let i): return Decimal(i)
        case .decimal(let d): return d
        case .double(let d):
            guard d.isFinite else { throw FormatError(code: "not-finite", message: "\(d) is not a finite number.") }
            return Decimal(string: "\(d)", locale: posix)!
        }
    }

    static func checkUnit(_ unit: String) throws {
        guard units[unit] != nil else {
            throw FormatError(code: "bad-unit", message: "\"\(unit)\" is not a unit these helpers know; use one of \(unitOrder.joined(separator: ", ")).")
        }
    }

    static func checkDecimals(_ d: Int, _ unit: String) throws {
        let top = maxDecimals[unit]!
        guard (0...top).contains(d) else {
            throw FormatError(code: "bad-decimals", message: "A value in \(unit) can show between 0 and \(top) decimal places, not \(d).")
        }
    }

    /// `|value|` rounded half away from zero to `decimals`, as plain digits.
    static func plain(_ value: Decimal, _ decimals: Int) -> (whole: String, frac: String) {
        var v = value < 0 ? -value : value
        var r = Decimal()
        NSDecimalRound(&r, &v, decimals, .plain)
        let s = NSDecimalNumber(decimal: r).description(withLocale: posix)
        let parts = s.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        var frac = parts.count > 1 ? parts[1] : ""
        if frac.count < decimals { frac += String(repeating: "0", count: decimals - frac.count) }
        return (parts[0], frac)
    }

    /// The absolute value to fixed decimals, with optional grouping (only
    /// over 9999: §4.2).
    static func digits(_ value: Decimal, _ decimals: Int, _ decimalSep: String, _ groupSep: String? = nil) -> String {
        var (whole, frac) = plain(value, decimals)
        if let g = groupSep, whole.count > 4 {
            let c = Array(whole)
            let head = c.count % 3 == 0 ? 3 : c.count % 3
            var out = String(c[0..<head])
            var i = head
            while i < c.count { out += g + String(c[i..<i + 3]); i += 3 }
            whole = out
        }
        return decimals > 0 ? whole + decimalSep + frac : whole
    }

    static func scaled(_ value: Decimal, _ unit: String) -> Decimal { value / Decimal(units[unit]!) }

    /// A frequency for display: `frequency(.int(14074000))` is
    /// `"14074.0\u{00A0}kHz"`. `unit` "auto" chooses kHz below 30 MHz and MHz
    /// from 30 MHz up; `decimals` nil takes the unit's default, and trailing
    /// zeros are kept so a dial does not change width as it tunes.
    public static func frequency(_ hz: Value, locale: String = "en", unit: String = "auto", decimals: Int? = nil) throws -> String {
        let value = try decimal(hz)
        guard value >= 0 else {
            throw FormatError(code: "negative-frequency", message: "A frequency cannot be negative; use offset() for a shift.")
        }
        let u = unit == "auto" ? (value >= Decimal(autoMHzFrom) ? "MHz" : "kHz") : unit
        try checkUnit(u)
        let d = decimals ?? defaultDecimals[u]!
        try checkDecimals(d, u)
        return digits(scaled(value, u), d, Symbols.of(locale).decimal) + nbsp + u
    }

    public static func frequency(_ hz: Int64, locale: String = "en", unit: String = "auto", decimals: Int? = nil) throws -> String {
        try frequency(.int(hz), locale: locale, unit: unit, decimals: decimals)
    }

    /// A signed shift or tone: `"+1500\u{00A0}Hz"`, `"\u{2212}600\u{00A0}kHz"`.
    /// Zero carries no sign. `decimals` nil shows as many decimals as the
    /// value needs, because shifts are round numbers (600 kHz, 7.6 MHz).
    public static func offset(_ hz: Value, unit: String = "Hz", locale: String = "en", decimals: Int? = nil) throws -> String {
        let value = try decimal(hz)
        try checkUnit(unit)
        let s = scaled(value, unit)
        let d: Int
        if let given = decimals {
            d = given
        } else {
            // As many decimals as the exact value has, at most the unit's
            // resolution (Python: the normalised exponent, clamped).
            let exact = NSDecimalNumber(decimal: s).description(withLocale: posix)
            var frac = Substring(exact.split(separator: ".", omittingEmptySubsequences: false).dropFirst().first ?? "")
            while frac.hasSuffix("0") { frac = frac.dropLast() }
            d = min(frac.count, maxDecimals[unit]!)
        }
        try checkDecimals(d, unit)
        let text = digits(s, d, Symbols.of(locale).decimal)
        let roundedZero = text.allSatisfy { $0 == "0" || $0 == "." || $0 == "," }
        let sign = roundedZero ? "" : (value > 0 ? "+" : shiftMinus)
        return sign + text + nbsp + unit
    }

    public static func offset(_ hz: Int64, unit: String = "Hz", locale: String = "en", decimals: Int? = nil) throws -> String {
        try offset(.int(hz), unit: unit, locale: locale, decimals: decimals)
    }

    /// A plain number (dB, W, MER, a count) in the locale's symbols, grouped
    /// only over 9999 so a year or 1500 W is never split.
    public static func number(_ x: Value, locale: String = "en", decimals: Int = 0) throws -> String {
        guard (0...12).contains(decimals) else {
            throw FormatError(code: "bad-decimals", message: "A number can show between 0 and 12 decimal places, not \(decimals).")
        }
        let value = try decimal(x)
        let sym = Symbols.of(locale)
        let text = digits(value, decimals, sym.decimal, sym.group)
        let negative = value < 0 && text.contains { ("1"..."9").contains($0) }
        return (negative ? sym.minus : "") + text
    }

    public static func number(_ x: Double, locale: String = "en", decimals: Int = 0) throws -> String {
        try number(.double(x), locale: locale, decimals: decimals)
    }

    /// A standing-wave ratio, one decimal: `swr(1.5)` is `"1.5:1"`.
    public static func swr(_ x: Value, locale: String = "en") throws -> String {
        let value = try decimal(x)
        guard value >= 1 else { throw FormatError(code: "bad-swr", message: "An SWR cannot be below 1:1, and \(value) is.") }
        return digits(value, 1, Symbols.of(locale).decimal) + ":1"
    }

    public static func swr(_ x: Double, locale: String = "en") throws -> String { try swr(.double(x), locale: locale) }

    static let unitsSentence = "Hz, kHz, MHz or GHz"

    /// What a person typed, as integer Hz. Either "." or "," is the decimal
    /// separator, whatever the locale (frequencies are never grouped, so
    /// there is no grouping symbol to confuse it with); spaces of any kind
    /// are ignored; a typed unit (any case) wins over the field's `unit`;
    /// with neither, the text is refused rather than guessed. Finer than
    /// 1 Hz is refused, not rounded.
    public static func parseFrequency(_ text: String, locale: String = "en", unit: String? = nil) throws -> Int64 {
        if let u = unit, u != "auto" { try checkUnit(u) }
        let fieldUnit = unit == "auto" ? nil : unit
        let compact = String(text.unicodeScalars.filter { !$0.properties.isWhitespace }.map(Character.init))
        guard !compact.isEmpty else { throw FormatError(code: "empty", message: "The frequency is empty; type a number such as 14074 kHz.") }
        // digits and separators, then letters only
        let numPart = String(compact.prefix { $0.isASCII && ($0.isNumber || $0 == "." || $0 == ",") })
        let typed = String(compact.dropFirst(numPart.count))
        let typedIsLetters = typed.unicodeScalars.allSatisfy { $0.properties.isAlphabetic }
        guard !numPart.isEmpty, typedIsLetters, numPart.contains(where: { $0.isNumber }) else {
            throw FormatError(code: "not-a-number",
                              message: "\"\(text)\" is not a frequency; type digits with at most one decimal separator and an optional unit (\(unitsSentence)).")
        }
        guard numPart.filter({ $0 == "." || $0 == "," }).count <= 1 else {
            throw FormatError(code: "two-separators",
                              message: "\"\(text)\" has more than one decimal separator; type the frequency with a single \".\" or \",\" and no digit grouping.")
        }
        let chosen: String
        if !typed.isEmpty {
            guard let u = unitOrder.first(where: { $0.lowercased() == typed.lowercased() }) else {
                throw FormatError(code: "unknown-unit", message: "\"\(typed)\" is not a frequency unit; use \(unitsSentence).")
            }
            chosen = u
        } else if let u = fieldUnit {
            chosen = u
        } else {
            throw FormatError(code: "no-unit", message: "\"\(text)\" has no unit; add \(unitsSentence).")
        }
        let n = numPart.replacingOccurrences(of: ",", with: ".")
        let parts = n.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        let whole = parts[0].isEmpty ? "0" : parts[0]
        let frac = parts.count > 1 && !parts[1].isEmpty ? parts[1] : "0"
        let hz = Decimal(string: "\(whole).\(frac)", locale: posix)! * Decimal(units[chosen]!)
        var h = hz
        var r = Decimal()
        NSDecimalRound(&r, &h, 0, .plain)
        guard r == hz else { throw FormatError(code: "too-precise", message: "\"\(text)\" is finer than 1 Hz; use fewer decimal places.") }
        return NSDecimalNumber(decimal: r).int64Value
    }
}

/// Decimal, group and minus symbols per locale: the same CLDR-derived table
/// as python/shack_i18n _cldr.py (where the values came from and how they
/// were checked is written there). Add a locale to every port, escaping
/// anything that is not printable ASCII so a reviewer can see which space
/// is which.
public struct Symbols: Equatable {
    public let decimal: String
    public let group: String
    public let minus: String

    static let hyphen = "-", minus = "\u{2212}", nbsp = "\u{00A0}", nnbsp = "\u{202F}"

    public static let table: [String: Symbols] = [
        "en": Symbols(decimal: ".", group: ",", minus: hyphen), "en-GB": Symbols(decimal: ".", group: ",", minus: hyphen),
        "en-US": Symbols(decimal: ".", group: ",", minus: hyphen),
        "de": Symbols(decimal: ",", group: ".", minus: hyphen), "de-AT": Symbols(decimal: ",", group: nbsp, minus: hyphen),
        "de-CH": Symbols(decimal: ".", group: "\u{2019}", minus: hyphen),
        "fr": Symbols(decimal: ",", group: nnbsp, minus: hyphen), "fr-CA": Symbols(decimal: ",", group: nbsp, minus: hyphen),
        "es": Symbols(decimal: ",", group: ".", minus: hyphen), "es-MX": Symbols(decimal: ".", group: ",", minus: hyphen),
        "it": Symbols(decimal: ",", group: ".", minus: hyphen), "nl": Symbols(decimal: ",", group: ".", minus: hyphen),
        "nb": Symbols(decimal: ",", group: nbsp, minus: minus), "nn": Symbols(decimal: ",", group: nbsp, minus: minus),
        "no": Symbols(decimal: ",", group: nbsp, minus: minus), "sv": Symbols(decimal: ",", group: nbsp, minus: minus),
        "da": Symbols(decimal: ",", group: ".", minus: hyphen), "fi": Symbols(decimal: ",", group: nbsp, minus: minus),
        "pl": Symbols(decimal: ",", group: nbsp, minus: hyphen),
        // CLDR's plain "pt" is Brazilian Portuguese; Portugal is the variant.
        "pt": Symbols(decimal: ",", group: ".", minus: hyphen), "pt-BR": Symbols(decimal: ",", group: ".", minus: hyphen),
        "pt-PT": Symbols(decimal: ",", group: nbsp, minus: hyphen),
        "ja": Symbols(decimal: ".", group: ",", minus: hyphen), "zh": Symbols(decimal: ".", group: ",", minus: hyphen),
        "ru": Symbols(decimal: ",", group: nbsp, minus: hyphen), "uk": Symbols(decimal: ",", group: nbsp, minus: hyphen),
        "cs": Symbols(decimal: ",", group: nbsp, minus: hyphen),
    ]

    /// Symbols for a BCP 47 or POSIX tag: exact tag, then language, then en.
    /// POSIX spellings (`de_DE.UTF-8`) lose the encoding and modifier; an
    /// unlisted region takes its language's symbols. A Foundation `Locale`
    /// gives its tag with `identifier`.
    public static func of(_ locale: String?) -> Symbols {
        guard let locale = locale, !locale.isEmpty else { return table["en"]! }
        let tag = (locale.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? "")
            .split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init)?
            .replacingOccurrences(of: "_", with: "-") ?? ""
        let parts = tag.split(separator: "-").map(String.init)
        guard let first = parts.first else { return table["en"]! }
        let lang = first.lowercased()
        if let region = parts.dropFirst().first(where: { $0.count == 2 && $0.allSatisfy(\.isLetter) })?.uppercased(),
           let s = table["\(lang)-\(region)"] {
            return s
        }
        return table[lang] ?? table["en"]!
    }
}
