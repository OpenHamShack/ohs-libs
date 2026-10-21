import Foundation

/// A controller (or Classic box) found on the LAN.
public struct Controller: Equatable {
    public let name: String
    /// The advert's 8-hex `id`; the full controllerId comes from `wellKnown`.
    public let id: String
    public let url: String
    public var kinds: [String] = []
    public var version: String = ""
    public var contract: String = ""
    /// A Classic 0.9.x box, not a controller.
    public var classic: Bool = false
    public var service: String = Discovery.service
    public var address: String = ""
    public var port: Int = 0
}

/// Reading what DNS-SD found (CONTROLLER-CONTRACT.md §2), a port of
/// python/shack_client discovery.py's pure half.
///
/// Two record types are browsed: `_openhamshack._tcp` (a controller, always
/// with TXT `platform=1`) and the legacy `_openhamshack._tcp`, which is a Classic
/// 0.9.x box when it has no `platform` key (shown as one qo100wb app, PIN
/// session on port 8080) and, with `platform=1`, PLATFORM-PLAN's draft name
/// for the same advert. When both resolve to one host, the `_openhamshack`
/// record wins.
///
/// Browsing is the app's (NWBrowser / NetService in Remote): it hands each
/// resolved service to `parseAdvert` with its TXT record as a dictionary
/// (`NetService.dictionary(fromTXTRecord:)` gives `[String: Data]`).
public enum Discovery {
    public static let service = "_openhamshack._tcp"
    public static let legacy = "_openhamshack._tcp"

    private static func txt(_ props: [String: Any]) -> [String: String] {
        props.mapValues { v in
            switch v {
            case let d as Data: return String(decoding: d, as: UTF8.self)
            case let s as String: return s
            default: return "\(v)"
            }
        }
    }

    private static func host(_ a: String) -> String { a.contains(":") && !a.hasPrefix("[") ? "[\(a)]" : a }

    /// One resolved DNS-SD record as a Controller, or nil if it is not ours.
    /// `serviceType` may carry `.local.` and a leading or trailing dot;
    /// `instance` is the service instance name (the callsign). TXT values are
    /// Data (NetService) or String.
    public static func parseAdvert(serviceType: String, instance: String, address: String, port: Int,
                                   txt props: [String: Any]) -> Controller? {
        let t = txt(props)
        var st = serviceType
        while st.hasPrefix(".") { st.removeFirst() }
        let base = (st.components(separatedBy: "._tcp").first ?? st) + "._tcp"
        let short = base.components(separatedBy: ".").first ?? base
        let fromInstance = instance.components(separatedBy: "." + short).first ?? instance
        let name = (t["name"] ?? "").isEmpty ? fromInstance : t["name"]!
        let isShack = base == service, isLegacy = base == legacy
        if isShack || (isLegacy && t["platform"] == "1") {
            guard t["platform"] == "1" else { return nil }
            let url = (t["https"] ?? "").isEmpty ? "http://\(host(address)):\(port)/" : "https://\(host(address)):\(t["https"]!)/"
            let kinds = (t["kinds"] ?? "").split(separator: ",").map(String.init).filter { !$0.isEmpty }.sorted()
            return Controller(name: name, id: t["id"] ?? "", url: url, kinds: kinds, version: t["version"] ?? "",
                              contract: t["contract"] ?? "", classic: false, service: isShack ? service : legacy,
                              address: address, port: port)
        }
        if isLegacy {
            // Classic box: REMOTE-API §1 TXT `name`, `version`, `port`.
            let p = Int(t["port"] ?? "") ?? (port != 0 ? port : 8080)
            return Controller(name: name, id: "", url: "http://\(host(address)):\(p)/", kinds: ["qo100wb"], version: t["version"] ?? "",
                              contract: "", classic: true, service: legacy, address: address, port: p)
        }
        return nil
    }

    /// One entry per host: the `_openhamshack` record beats a `_openhamshack` one
    /// with platform=1 for the same address and port; Classic boxes stay as
    /// they are.
    public static func mergeAdverts(_ found: [Controller]) -> [Controller] {
        struct Key: Hashable { let a: String; let p: Int; let c: Bool }
        var best: [Key: Controller] = [:]
        var order: [Key] = []
        for c in found {
            let k = Key(a: c.address, p: c.port, c: c.classic)
            if let cur = best[k] {
                if c.service == service && cur.service != service { best[k] = c }
            } else {
                best[k] = c
                order.append(k)
            }
        }
        return order.map { best[$0]! }.sorted { ($0.name, $0.url) < ($1.name, $1.url) }
    }

    /// GET <base>.well-known/openhamshack, with every relative reference in
    /// it resolved against the base URL (not the document's own URL: by RFC
    /// 3986 "api/shack/token" against ".../.well-known/openhamshack" lands
    /// under .well-known). Throws ShackError (`not_a_controller`,
    /// `controller_too_new`) or Offline.
    public static func wellKnown(_ baseURL: String, transport: Transport? = nil) throws -> JSONObject {
        let t = transport ?? HTTPTransport(baseURL, timeout: 5)
        let r = try t.get(".well-known/openhamshack")
        guard r.status == 200, let v = r.body["openhamshack"] else {
            throw ShackError(code: "not_a_controller", message: "\(baseURL) is not an OpenHamShack controller")
        }
        guard v.int64Value == 1 else {
            throw ShackError(code: "controller_too_new", message: "This controller is newer than this app: update the app to use it")
        }
        var out = r.body
        var auth = r.body["auth"]?.obj ?? [:]
        for k in ["token", "pair"] { if let s = auth[k]?.stringValue { auth[k] = .string(URLs.join(baseURL, s)) } }
        out["auth"] = .object(auth)
        if var services = r.body["services"]?.objectValue, let b = services["base"]?.stringValue {
            services["base"] = .string(URLs.join(baseURL, b))
            out["services"] = .object(services)
        }
        out["apps"] = .array((r.body["apps"]?.arr ?? []).map { a in
            var m = a.obj
            m["base"] = .string(URLs.join(baseURL, m["base"]?.str ?? ""))
            if let icon = m["icon"]?.stringValue, !icon.isEmpty { m["icon"] = .string(URLs.join(baseURL, icon)) }
            return .object(m)
        })
        return out
    }
}
