import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public protocol PushTokenAPI: Sendable {
    func registerPushToken(_ token: String) async throws
    func forgetPushToken() async throws
}

/// The two push-token routes of `docs/api/device.md`, with the same bearer header and 401 handling as `APIClient`.
public struct PushClient: PushTokenAPI {
    public static let path = "/api/device/push-token"

    private struct Body: Encodable {
        let token: String
    }

    private let baseURL: URL
    private let session: URLSession
    private let tokenProvider: TokenProvider

    public init(baseURL: URL, session: URLSession = .shared, tokenProvider: @escaping TokenProvider) {
        self.baseURL = baseURL
        self.session = session
        self.tokenProvider = tokenProvider
    }

    public func registerPushToken(_ token: String) async throws {
        let body: Data
        do {
            body = try JSONEncoder().encode(Body(token: token))
        } catch {
            throw APIError.decoding
        }
        try await send(method: "PUT", body: body)
    }

    public func forgetPushToken() async throws {
        try await send(method: "DELETE", body: nil)
    }

    private func send(method: String, body: Data?) async throws {
        guard let bearer = await tokenProvider() else { throw APIError.notSignedIn }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw APIError.invalidResponse
        }
        let prefix = components.percentEncodedPath.hasSuffix("/")
            ? String(components.percentEncodedPath.dropLast())
            : components.percentEncodedPath
        components.percentEncodedPath = prefix + Self.path
        guard let url = components.url else { throw APIError.invalidResponse }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
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
        case 200..<300: return
        case 401: throw APIClient.refusal(of: data)
        case 400..<500: throw APIError.rejected(status: http.statusCode)
        default: throw APIError.server(status: http.statusCode)
        }
    }
}
