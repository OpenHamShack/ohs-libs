import Foundation

/// No answer from the controller: refused, unreachable, timed out, or the
/// connection dropped mid-answer. The one case the sync loop treats as "the
/// controller is away" rather than "the controller said no".
public struct Offline: Error, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

/// An error a person may see, with a machine `code` for the app's own
/// catalog (I18N.md: an app translates codes; the English `message` is the
/// fallback).
public struct ShackError: Error, CustomStringConvertible, Equatable {
    public let code: String
    public let message: String
    public init(code: String, message: String) { self.code = code; self.message = message }
    public var description: String { message }
}

public struct Response {
    public let status: Int
    public let body: JSONObject
    public init(status: Int, body: JSONObject) { self.status = status; self.body = body }
}

/// One request to a controller, answering (status, JSON object) for any
/// HTTP status and throwing `Offline` only when no answer came at all.
/// Synchronous: the library calls it from its sync thread or from a pairing
/// call an app runs off the main actor, never from a UI path.
public protocol Transport {
    func request(_ method: String, _ path: String, body: JSONObject?, query: [(String, String?)]?) throws -> Response
}

extension Transport {
    public func get(_ path: String, query: [(String, String?)]? = nil) throws -> Response {
        try request("GET", path, body: nil, query: query)
    }

    public func post(_ path: String, _ body: JSONObject) throws -> Response {
        try request("POST", path, body: body, query: nil)
    }
}

/// Makes the transport for a base URL and token (nil before pairing).
public typealias TransportFactory = (_ baseURL: String, _ token: String?) -> Transport

public enum URLs {
    /// `path` resolved against `baseURL` as a relative reference, never
    /// built absolute (CONTROLLER-CONTRACT.md §3): the same controller is
    /// reached under a path prefix on one address and at the root of another.
    public static func join(_ baseURL: String, _ path: String, query: [(String, String?)]? = nil) -> String {
        let base = baseURL.hasSuffix("/") ? baseURL : baseURL + "/"
        var rel = path
        while rel.hasPrefix("/") { rel.removeFirst() }
        var url = URL(string: rel, relativeTo: URL(string: base))?.absoluteString ?? base + rel
        let q = (query ?? []).compactMap { k, v in v.map { (k, $0) } }
        if !q.isEmpty {
            var allowed = CharacterSet.alphanumerics
            allowed.insert(charactersIn: "-._~")
            url += "?" + q.map { k, v in
                (k.addingPercentEncoding(withAllowedCharacters: allowed) ?? k) + "=" + (v.addingPercentEncoding(withAllowedCharacters: allowed) ?? v)
            }.joined(separator: "&")
        }
        return url
    }
}

/// HTTP over URLSession, waited on synchronously. Timeouts are §6.3's 10 s.
/// The token goes in an Authorization header, never a URL (§4.1).
public final class HTTPTransport: Transport {
    public static let timeout: TimeInterval = 10
    private let baseURL: String
    private let token: String?
    private let session: URLSession

    public init(_ baseURL: String, token: String? = nil, timeout: TimeInterval = HTTPTransport.timeout) {
        self.baseURL = baseURL
        self.token = token
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = timeout
        cfg.timeoutIntervalForResource = timeout
        cfg.waitsForConnectivity = false
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: cfg)
    }

    deinit { session.finishTasksAndInvalidate() }

    public static let factory: TransportFactory = { base, token in HTTPTransport(base, token: token) }

    public func request(_ method: String, _ path: String, body: JSONObject?, query: [(String, String?)]?) throws -> Response {
        guard let url = URL(string: URLs.join(baseURL, path, query: query)) else { throw Offline("bad URL \(baseURL) \(path)") }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let t = token { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
        if let b = body {
            req.httpBody = Data(JSON.dump(.object(b)).utf8)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let done = DispatchSemaphore(value: 0)
        var result: (Data?, URLResponse?, Error?) = (nil, nil, nil)
        let task = session.dataTask(with: req) { d, r, e in
            result = (d, r, e)
            done.signal()
        }
        task.resume()
        done.wait()
        if let e = result.2 { throw Offline(e.localizedDescription) }
        guard let http = result.1 as? HTTPURLResponse else { throw Offline("no HTTP answer") }
        return Response(status: http.statusCode, body: HTTPTransport.decode(result.0 ?? Data()))
    }

    static func decode(_ raw: Data) -> JSONObject {
        if raw.isEmpty { return [:] }
        guard let v = try? JSON.parse(data: raw) else {
            return ["error": "The controller answered with something that is not JSON.", "code": "bad_response"]
        }
        return v.objectValue ?? ["value": v]
    }
}
