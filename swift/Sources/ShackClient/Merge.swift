import Foundation

/// A record in the wire shape of SYNC.md §6.1, held as a JSON object:
///
///     {"collection": "logbook", "id": "<uuid>", "fields": {...},
///      "fieldHlc": {...}, "deletedHlc": null | "<hlc>",
///      "createdAt": "<iso>", "origin": {...}}
///
/// plus any envelope keys a later contract version adds, carried through
/// untouched (§8 T19). An object rather than a struct for that reason: a
/// typed record would drop what it does not know.
public typealias Record = JSONObject

/// The merge every replica runs (SYNC.md §4), a port of python/shack_client
/// merge.py: client stores and the controller run the same function from
/// the same vectors, so they cannot drift.
///
/// State-based and per field: for each field, the value with the greater
/// HLC. Commutative, associative and idempotent (MergeTests checks all
/// three on generated records).
public enum Merge {
    public static let envelope: Set<String> = ["collection", "id", "fields", "fieldHlc", "deletedHlc", "createdAt", "origin"]

    /// The profile's two maps whose entries merge one by one (§4.2): two
    /// devices adding different pad layouts, or answering different licence
    /// conditions, must both be kept. Value: how many levels are split.
    public static let profileSplit: [String: Int] = ["conditions": 1, "controllerMaps": 2]

    public static func fields(_ r: Record) -> JSONObject { r["fields"]?.obj ?? [:] }

    public static func fieldHlc(_ r: Record) -> [String: String] {
        (r["fieldHlc"]?.obj ?? [:]).compactMapValues { $0.stringValue }
    }

    public static func deletedHlc(_ r: Record) -> String? {
        guard let s = r["deletedHlc"]?.stringValue, !s.isEmpty else { return nil }
        return s
    }

    /// Envelope keys this version does not know, to carry through.
    public static func extraOf(_ r: Record) -> JSONObject { r.filter { !envelope.contains($0.key) && $0.key != "seq" } }

    public static func maxHlc(_ hlcs: [String?]) -> String? { hlcs.compactMap { $0 }.filter { !$0.isEmpty }.max() }

    /// Every stamp a record carries: its fields' and its tombstone's.
    public static func stamps(_ r: Record) -> [String] { Array(fieldHlc(r).values) + [deletedHlc(r)].compactMap { $0 } }

    /// Deleted only while the tombstone is later than every field edit: a
    /// later edit brings a contact back (§5, edit beats delete).
    public static func isLive(_ r: Record) -> Bool {
        guard let d = deletedHlc(r) else { return true }
        return fieldHlc(r).values.contains { $0 > d }
    }

    /// The merged record. Either side may be nil (a record one replica has
    /// never seen).
    public static func merge(_ a: Record?, _ b: Record?) -> Record {
        guard let a = a else { return b ?? [:] }
        guard let b = b else { return a }
        let ha = fieldHlc(a), hb = fieldHlc(b)
        var fields: JSONObject = [:]
        var hlcs: JSONObject = [:]
        for f in Set(ha.keys).union(hb.keys) {
            let x = ha[f], y = hb[f]
            // Equal stamps mean the same edit seen twice: either side's value
            // is the same value. Ties never happen between different edits.
            let src = (y == nil || (x != nil && x! >= y!)) ? a : b
            hlcs[f] = .string(fieldHlc(src)[f]!)
            if let v = Merge.fields(src)[f] { fields[f] = v }
        }
        let ca = a["createdAt"]?.str ?? "", cb = b["createdAt"]?.str ?? ""
        // The origin is the earlier creation's; a tie (two copies of one
        // creation) breaks on the origin's sorted JSON, so the merge stays
        // commutative even for copies that disagree. The JSON is Python's
        // (JSON.dump), so a Swift client and the Python controller agree.
        func key(_ r: Record) -> (Bool, String, String) {
            let c = r["createdAt"]?.str ?? ""
            return (c.isEmpty, c, JSON.dump(r["origin"]?.obj ?? [:]))
        }
        let ka = key(a), kb = key(b)
        let aFirst = (ka.0 ? 1 : 0, ka.1, ka.2) <= (kb.0 ? 1 : 0, kb.1, kb.2)
        let first = aFirst ? a : b
        var out: Record = [:]
        out["collection"] = a["collection"] ?? .null
        out["id"] = a["id"] ?? .null
        out["fields"] = .object(fields)
        out["fieldHlc"] = .object(hlcs)
        out["deletedHlc"] = maxHlc([deletedHlc(a), deletedHlc(b)]).map { .string($0) } ?? .null
        out["createdAt"] = .string([ca, cb].filter { !$0.isEmpty }.min() ?? "")
        out["origin"] = .object(first["origin"]?.obj ?? [:])
        // Unknown envelope keys: the side with the later state wins each, so
        // a newer client's addition survives an older one's push.
        let (newer, older) = latest(a) >= latest(b) ? (a, b) : (b, a)
        for (k, v) in extraOf(older) { out[k] = v }
        for (k, v) in extraOf(newer) { out[k] = v }
        return out
    }

    private static func latest(_ r: Record) -> String { maxHlc([deletedHlc(r)] + fieldHlc(r).values.map { $0 }) ?? "" }

    /// Whether two states are the same for sync purposes (a merge that
    /// returns the stored state changed nothing and takes no new seq).
    public static func same(_ a: Record?, _ b: Record?) -> Bool {
        guard let a = a, let b = b else { return a == nil && b == nil }
        return fields(a) == fields(b) && fieldHlc(a) == fieldHlc(b) && deletedHlc(a) == deletedHlc(b) &&
            (a["createdAt"]?.str ?? "") == (b["createdAt"]?.str ?? "") && (a["origin"]?.obj ?? [:]) == (b["origin"]?.obj ?? [:]) &&
            extraOf(a) == extraOf(b)
    }

    /// A local edit as a record state: the same merge, with the edit as a
    /// one-op record. A `.null` value clears a field (an edit with its own
    /// HLC: SERVICES.md §3.2 PATCH).
    public static func applyEdit(_ rec: Record?, collection: String, id: String, kind: String, fields: JSONObject,
                                 hlc: String, origin: JSONObject, nowIso: String) -> Record {
        var oneFields: JSONObject = [:]
        var oneHlc: JSONObject = [:]
        var deleted: JSONValue = .null
        if kind == "delete" {
            deleted = .string(hlc)
        } else {
            for (f, v) in fields {
                oneHlc[f] = .string(hlc)
                if !v.isNull { oneFields[f] = v }
            }
        }
        let one: Record = ["collection": .string(collection), "id": .string(id), "fields": .object(oneFields),
                           "fieldHlc": .object(oneHlc), "deletedHlc": deleted,
                           "createdAt": rec == nil ? .string(nowIso) : (rec!["createdAt"] ?? .string(nowIso)),
                           "origin": rec == nil ? .object(origin) : (rec!["origin"] ?? .object(origin))]
        return merge(rec, one)
    }

    /// The fields an arriving op set that a later-HLC edit from another
    /// store had already set (SERVICES.md §3.2 `lost`).
    public static func lostFields(op: JSONObject, state: Record) -> [String] {
        let hlc = op["hlc"]?.str ?? ""
        let node = Hlc.nodeOf(hlc)
        let sh = fieldHlc(state)
        return (op["fields"]?.obj ?? [:]).keys.sorted().filter { f in
            guard let h = sh[f] else { return false }
            return h > hlc && Hlc.nodeOf(h) != node
        }
    }

    // MARK: profile paths

    /// A profile patch as record fields: top-level keys, except the two maps
    /// of §4.2, which become dotted paths ("conditions.nearLondon",
    /// "controllerMaps.qo100wb.ajazz-akp03e").
    public static func splitProfile(_ partial: JSONObject) -> JSONObject {
        var out: JSONObject = [:]
        for (k, v) in partial {
            if let depth = profileSplit[k], let m = v.objectValue {
                flatten(m, depth, []) { path, leaf in out[k + "." + path.joined(separator: ".")] = leaf }
            } else {
                out[k] = v
            }
        }
        return out
    }

    private static func flatten(_ d: JSONObject, _ depth: Int, _ prefix: [String], _ emit: ([String], JSONValue) -> Void) {
        for (k, v) in d {
            if depth > 1, let m = v.objectValue { flatten(m, depth - 1, prefix + [k], emit) } else { emit(prefix + [k], v) }
        }
    }

    /// Record fields back into the SERVICES.md §2.1 shape.
    public static func joinProfile(_ fields: JSONObject) -> JSONObject {
        var out: JSONObject = [:]
        for k in fields.keys.sorted() {
            let parts = k.split(separator: ".", maxSplits: 1).map(String.init)
            if parts.count == 2, let depth = profileSplit[parts[0]] {
                let path = parts[1].split(separator: ".", maxSplits: depth - 1).map(String.init)
                out[parts[0]] = .object(insert(out[parts[0]]?.obj ?? [:], path, fields[k]!))
            } else {
                out[k] = fields[k]!
            }
        }
        return out
    }

    private static func insert(_ node: JSONObject, _ path: [String], _ v: JSONValue) -> JSONObject {
        var n = node
        if path.count == 1 { n[path[0]] = v; return n }
        n[path[0]] = .object(insert(n[path[0]]?.obj ?? [:], Array(path.dropFirst()), v))
        return n
    }
}
