import Foundation

/// The smallest HTTP/1.1 server that serves the reference controller on a
/// loopback port, so the client's URLSession transport is tested over a
/// real socket (pairing, a 401's body, a dropped answer). BSD sockets on
/// threads: one request per connection, then close. A handler that answers
/// nil closes the connection without a reply, which is how a "request
/// applied, answer lost" fault looks to the client.
final class LoopbackHTTPServer {
    typealias Handler = (_ method: String, _ path: String, _ headers: [String: String], _ body: Data) -> (Int, Data)?

    let port: Int
    private let fd: Int32
    private let handler: Handler
    private var stopped = false
    private let lock = NSLock()

    init(handler: @escaping Handler) throws {
        self.handler = handler
        let s = socket(AF_INET, SOCK_STREAM, 0)
        guard s >= 0 else { throw POSIXError(.EIO) }
        var one: Int32 = 1
        setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        guard bound == 0, listen(s, 16) == 0 else { close(s); throw POSIXError(.EADDRINUSE) }
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(s, $0, &len) } }
        fd = s
        port = Int(UInt16(bigEndian: addr.sin_port))
        let t = Thread { [weak self] in self?.acceptLoop() }
        t.start()
    }

    private var isStopped: Bool { lock.lock(); defer { lock.unlock() }; return stopped }

    private func acceptLoop() {
        while !isStopped {
            let c = accept(fd, nil, nil)
            if c < 0 { if isStopped { return }; continue }
            var nosig: Int32 = 1
            setsockopt(c, SOL_SOCKET, SO_NOSIGPIPE, &nosig, socklen_t(MemoryLayout<Int32>.size))
            Thread { [weak self] in self?.serve(c) }.start()
        }
    }

    private func readSome(_ c: Int32) -> Data? {
        var buf = [UInt8](repeating: 0, count: 65536)
        let n = read(c, &buf, buf.count)
        return n > 0 ? Data(buf[0..<n]) : nil
    }

    private func serve(_ c: Int32) {
        defer { close(c) }
        var data = Data()
        let sep = Data("\r\n\r\n".utf8)
        while data.range(of: sep) == nil {
            guard let more = readSome(c) else { return }
            data.append(more)
        }
        let headEnd = data.range(of: sep)!
        let head = String(decoding: data[..<headEnd.lowerBound], as: UTF8.self)
        var body = Data(data[headEnd.upperBound...])
        let lines = head.components(separatedBy: "\r\n")
        let first = lines[0].split(separator: " ").map(String.init)
        guard first.count >= 2 else { return }
        var headers: [String: String] = [:]
        for l in lines.dropFirst() {
            guard let colon = l.firstIndex(of: ":") else { continue }
            headers[String(l[..<colon])] = l[l.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        let length = Int(headers.first { $0.key.lowercased() == "content-length" }?.value ?? "0") ?? 0
        while body.count < length {
            guard let more = readSome(c) else { return }
            body.append(more)
        }
        guard let answer = handler(first[0], first[1], headers, body) else { return }
        let (status, out) = answer
        let reason = status < 300 ? "OK" : "Error"
        var resp = Data("HTTP/1.1 \(status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(out.count)\r\nConnection: close\r\n\r\n".utf8)
        resp.append(out)
        resp.withUnsafeBytes { p in
            var off = 0
            while off < resp.count {
                let n = write(c, p.baseAddress! + off, resp.count - off)
                if n <= 0 { return }
                off += n
            }
        }
    }

    func stop() {
        lock.lock(); stopped = true; lock.unlock()
        shutdown(fd, SHUT_RDWR)
        close(fd)
    }
}
