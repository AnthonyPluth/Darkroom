import Foundation

public struct ServerAddress: Equatable, Sendable {
    public enum Problem: Error, Equatable, Sendable {
        case empty
        case notHTTPS
        case invalid
    }

    public let url: URL

    public init(parsing text: String) throws {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw Problem.empty }
        let withScheme = trimmed.contains("://") ? trimmed : "https://" + trimmed
        guard var components = URLComponents(string: withScheme) else { throw Problem.invalid }
        guard let scheme = components.scheme?.lowercased() else { throw Problem.invalid }
        guard scheme == "https" else { throw Problem.notHTTPS }
        guard let host = components.host, !host.isEmpty else { throw Problem.invalid }
        guard components.user == nil, components.password == nil, components.query == nil, components.fragment == nil else {
            throw Problem.invalid
        }
        components.scheme = "https"
        while components.percentEncodedPath.hasSuffix("/") {
            components.percentEncodedPath.removeLast()
        }
        guard let url = components.url else { throw Problem.invalid }
        self.url = url
    }

    public var text: String {
        url.absoluteString
    }
}
