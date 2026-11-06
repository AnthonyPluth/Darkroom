import Foundation

public enum SessionItem: String, Sendable {
    case serverURL = "server-url"
    case deviceToken = "device-token"
}

public protocol SessionStorage: Sendable {
    func load(_ item: SessionItem) throws -> String?
    func save(_ value: String, as item: SessionItem) throws
    func delete(_ item: SessionItem) throws
}

public enum SignInError: Error, Equatable, Sendable {
    case server(ServerAddress.Problem)
    case alreadySignedIn
    case notSigningIn
    case noToken
    case storage
}

public actor DeviceSession {
    public enum Phase: Equatable, Sendable {
        case signedOut
        case signingIn(ServerAddress)
        case signedIn(ServerAddress)
    }

    private let storage: SessionStorage
    private var token: String?
    public private(set) var phase = Phase.signedOut
    public private(set) var savedServer: ServerAddress?

    public init(storage: SessionStorage) {
        self.storage = storage
    }

    public var currentToken: String? {
        token
    }

    public nonisolated func tokenProvider() -> TokenProvider {
        { [self] in await currentToken }
    }

    @discardableResult
    public func restore() throws -> Phase {
        let serverText: String?
        let storedToken: String?
        do {
            serverText = try storage.load(.serverURL)
            storedToken = try storage.load(.deviceToken)
        } catch {
            throw SignInError.storage
        }
        savedServer = serverText.flatMap { try? ServerAddress(parsing: $0) }
        if let server = savedServer, let storedToken, !storedToken.isEmpty {
            token = storedToken
            phase = .signedIn(server)
        } else {
            token = nil
            phase = .signedOut
        }
        return phase
    }

    public func begin(serverText: String, deviceName: String) throws -> URL {
        if case .signedIn = phase { throw SignInError.alreadySignedIn }
        let server: ServerAddress
        do {
            server = try ServerAddress(parsing: serverText)
        } catch let problem as ServerAddress.Problem {
            throw SignInError.server(problem)
        } catch {
            throw SignInError.server(.invalid)
        }
        guard let start = DeviceSignIn.startURL(server: server, deviceName: deviceName) else {
            throw SignInError.server(.invalid)
        }
        savedServer = server
        phase = .signingIn(server)
        return start
    }

    public func complete(callback: URL) throws {
        guard case .signingIn(let server) = phase else { throw SignInError.notSigningIn }
        guard let issued = DeviceSignIn.token(fromCallback: callback) else {
            phase = .signedOut
            throw SignInError.noToken
        }
        do {
            try storage.save(server.text, as: .serverURL)
            try storage.save(issued, as: .deviceToken)
        } catch {
            try? storage.delete(.deviceToken)
            phase = .signedOut
            throw SignInError.storage
        }
        token = issued
        phase = .signedIn(server)
    }

    public func cancel() {
        if case .signingIn = phase { phase = .signedOut }
    }

    public func signOut() throws {
        do {
            try storage.delete(.deviceToken)
        } catch {
            throw SignInError.storage
        }
        token = nil
        phase = .signedOut
    }

    public func tokenRejected(_ rejected: String) throws {
        guard token == rejected else { return }
        try signOut()
    }
}
