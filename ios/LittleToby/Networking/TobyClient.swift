import Foundation

/// Why a request didn't work, in terms the app can explain and act on.
enum TobyError: Error, Equatable {
    /// This phone has no internet connection.
    case phoneOffline
    /// The computer didn't answer: asleep, off, offline, or Tailscale is off.
    case computerUnreachable
    /// The computer answered but Toby isn't running there (or its phone
    /// connection is off), e.g. Tailscale's proxy reported a bad gateway.
    case tobyNotRunning
    /// This phone isn't paired (any more). Pair again.
    case unpaired
    /// Too many wrong attempts; wait a minute.
    case locked
    /// The computer can't do this (yet), and says so.
    case unavailable(String)
    /// The screen view is turned off on the computer.
    case screenViewOff
    /// Something else, with the computer's own explanation when it gave one.
    case server(Int, String)
    case badResponse

    var message: String {
        switch self {
        case .phoneOffline: return "Your phone is offline."
        case .computerUnreachable: return "Can't reach your computer. It may be asleep, turned off, or offline."
        case .tobyNotRunning: return "Your computer is on, but Toby isn't running there."
        case .unpaired: return "This phone isn't paired with that computer any more."
        case .locked: return "Too many wrong attempts. Wait a minute and try again."
        case .unavailable(let why): return why
        case .screenViewOff: return "Screen view is turned off on your computer."
        case .server(_, let why): return why.isEmpty ? "Toby couldn't do that." : why
        case .badResponse: return "Toby sent something this app didn't understand."
        }
    }
}

/// Talks to one computer's Toby over HTTPS (through Tailscale) or, on the
/// same network, plain HTTP. Every call is async and throws TobyError.
final class TobyClient {
    let baseURL: URL
    var token: String?
    private let session: URLSession

    init(baseURL: URL, token: String? = nil, session: URLSession? = nil) {
        self.baseURL = baseURL
        self.token = token
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.default
            config.timeoutIntervalForRequest = 40      // long polls take up to 20 s
            config.timeoutIntervalForResource = 60
            config.waitsForConnectivity = false
            config.requestCachePolicy = .reloadIgnoringLocalCacheData
            config.httpMaximumConnectionsPerHost = 4
            self.session = URLSession(configuration: config)
        }
    }

    func url(_ path: String, query: [URLQueryItem] = []) -> URL {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        let base = components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path
        components.path = base + path
        components.queryItems = query.isEmpty ? nil : query
        return components.url!
    }

    // -- requests -----------------------------------------------------------------
    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], timeout: TimeInterval = 15,
                           auth: Bool = true) async throws -> T {
        let data = try await raw("GET", path, query: query, body: nil, timeout: timeout, auth: auth)
        return try decode(data)
    }

    func post<T: Decodable>(_ path: String, _ body: [String: Any] = [:], timeout: TimeInterval = 15,
                            auth: Bool = true) async throws -> T {
        let payload = try JSONSerialization.data(withJSONObject: body)
        let data = try await raw("POST", path, query: [], body: payload, timeout: timeout, auth: auth)
        return try decode(data)
    }

    func data(_ path: String, query: [URLQueryItem] = [], timeout: TimeInterval = 15) async throws -> Data {
        try await raw("GET", path, query: query, body: nil, timeout: timeout, auth: true)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        do {
            return try JSON.decoder.decode(T.self, from: data)
        } catch {
            throw TobyError.badResponse
        }
    }

    private func raw(_ method: String, _ path: String, query: [URLQueryItem], body: Data?,
                     timeout: TimeInterval, auth: Bool) async throws -> Data {
        var request = URLRequest(url: url(path, query: query), timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if auth, let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            throw Self.map(error)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw TobyError.computerUnreachable
        }
        guard let http = response as? HTTPURLResponse else { throw TobyError.badResponse }
        if (200..<300).contains(http.statusCode) { return data }
        throw Self.map(status: http.statusCode, body: data)
    }

    // -- error mapping (unit tested) ---------------------------------------------------
    static func map(_ error: URLError) -> Error {
        switch error.code {
        case .cancelled:
            return CancellationError()
        case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff:
            return TobyError.phoneOffline
        default:
            return TobyError.computerUnreachable
        }
    }

    static func map(status: Int, body: Data) -> TobyError {
        let info = try? JSON.decoder.decode(APIErrorBody.self, from: body)
        let message = info?.error ?? ""
        switch status {
        case 401: return .unpaired
        case 429: return .locked
        case 403 where info?.code == "screen_view_off": return .screenViewOff
        case 501: return .unavailable(message.isEmpty ? "That isn't available on this computer yet." : message)
        case 502, 503, 504:
            // Tailscale's proxy answers 502 when nothing is listening behind
            // it; Toby itself answers 503 with a reason for some failures.
            return info?.error != nil ? .server(status, message) : .tobyNotRunning
        default: return .server(status, message)
        }
    }
}
