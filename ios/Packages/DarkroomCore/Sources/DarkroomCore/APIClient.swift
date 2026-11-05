import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public typealias TokenProvider = @Sendable () async -> String?

public struct APIClient: Sendable {
    public static let sha1Header = "X-Darkroom-SHA1"
    public static let filenameHeader = "X-Darkroom-Filename"

    public let baseURL: URL
    private let session: URLSession
    private let tokenProvider: TokenProvider

    public init(baseURL: URL, session: URLSession = .shared, tokenProvider: @escaping TokenProvider) {
        self.baseURL = baseURL
        self.session = session
        self.tokenProvider = tokenProvider
    }

    public func probe() async throws {
        _ = try await send(path: "/healthz", method: "GET", body: nil, authenticated: false)
    }

    public func check(_ request: CheckRequest) async throws -> CheckReply {
        try await post("/api/device/assets/check", request)
    }

    public func checkin(_ request: CheckinRequest) async throws {
        _ = try await send(path: "/api/device/checkin", method: "POST", body: encode(request))
    }

    public func eligible(_ request: EligibleRequest) async throws -> EligibleReply {
        try await post("/api/device/eligible", request)
    }

    public func confirm(_ request: ConfirmRequest) async throws -> ConfirmReply {
        try await post("/api/device/eligible/confirm", request)
    }

    public func deleted(_ request: DeletedRequest) async throws -> DeletedReply {
        try await post("/api/device/deleted", request)
    }

    public func uploadRequest(assetID: String, kind: ResourceKind, sha1: String, filename: String) async throws -> URLRequest {
        guard let token = await tokenProvider() else { throw APIError.notSignedIn }
        let path = "/api/device/assets/\(Self.escape(assetID))/resources/\(kind.rawValue)"
        var request = URLRequest(url: try url(for: path))
        request.httpMethod = "PUT"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(sha1, forHTTPHeaderField: Self.sha1Header)
        request.setValue(Self.encodeFilename(filename), forHTTPHeaderField: Self.filenameHeader)
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func post<Request: Encodable, Reply: Decodable>(_ path: String, _ body: Request) async throws -> Reply {
        let data = try await send(path: path, method: "POST", body: encode(body))
        do {
            return try JSONDecoder().decode(Reply.self, from: data)
        } catch {
            throw APIError.decoding
        }
    }

    private func encode<Body: Encodable>(_ body: Body) throws -> Data {
        do {
            return try JSONEncoder().encode(body)
        } catch {
            throw APIError.decoding
        }
    }

    private func send(path: String, method: String, body: Data?, authenticated: Bool = true) async throws -> Data {
        var request = URLRequest(url: try url(for: path))
        request.httpMethod = method
        if authenticated {
            guard let token = await tokenProvider() else { throw APIError.notSignedIn }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            if Task.isCancelled || error is CancellationError { throw CancellationError() }
            throw APIError.transport(String(describing: (error as? URLError)?.code.rawValue ?? -1))
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        switch http.statusCode {
        case 200..<300: return data
        case 401: throw APIError.unauthorized
        case 400..<500: throw APIError.rejected(status: http.statusCode)
        default: throw APIError.server(status: http.statusCode)
        }
    }

    private func url(for path: String) throws -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw APIError.invalidResponse
        }
        let prefix = components.percentEncodedPath.hasSuffix("/")
            ? String(components.percentEncodedPath.dropLast())
            : components.percentEncodedPath
        components.percentEncodedPath = prefix + path
        guard let url = components.url else { throw APIError.invalidResponse }
        return url
    }

    private static func escape(_ segment: String) -> String {
        segment.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? segment
    }

    static func encodeFilename(_ filename: String) -> String {
        let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return filename.addingPercentEncoding(withAllowedCharacters: unreserved) ?? ""
    }
}
