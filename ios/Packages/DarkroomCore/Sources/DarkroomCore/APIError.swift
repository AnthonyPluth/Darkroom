import Foundation

public enum APIError: Error, Equatable, Sendable {
    case notSignedIn
    case unauthorized
    case revoked
    case rejected(status: Int)
    case server(status: Int)
    case transport(String)
    case decoding
    case invalidResponse
}
