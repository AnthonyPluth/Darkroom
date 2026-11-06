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
    case stateMismatch
    case expired
    case noPerson
    case notAllowed
    case storage
}

public actor DeviceSession {
    public enum Phase: Equatable, Sendable {
        case signedOut
        case signingIn(ServerAddress)
        case signedIn(ServerAddress)
    }

    public static let attemptLifetime: TimeInterval = 600

    private let storage: SessionStorage
    private let makeState: @Sendable () -> String
    private let clock: @Sendable () -> Date
    private var token: String?
    private var attempt: (state: String, startedAt: Date)?
    public private(set) var phase = Phase.signedOut
    public private(set) var savedServer: ServerAddress?

    public init(
        storage: SessionStorage,
        makeState: @escaping @Sendable () -> String = DeviceSignIn.newState,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.storage = storage
        self.makeState = makeState
        self.clock = clock
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
        let state = makeState()
        guard let start = DeviceSignIn.startURL(server: server, deviceName: deviceName, state: state) else {
            throw SignInError.server(.invalid)
        }
        attempt = (state, clock())
        savedServer = server
        phase = .signingIn(server)
        return start
    }

    public func complete(callback: URL) throws {
        guard case .signingIn(let server) = phase, let current = attempt else { throw SignInError.notSigningIn }
        attempt = nil
        let refusal = DeviceSignIn.refusal(from: callback)
        let received = DeviceSignIn.callback(from: callback)
        guard let state = refusal?.state ?? received?.state else { throw failed(.noToken) }
        guard Self.sameState(state, current.state) else { throw failed(.stateMismatch) }
        guard clock().timeIntervalSince(current.startedAt) <= Self.attemptLifetime else { throw failed(.expired) }
        if let refusal {
            throw failed(refusal.reason == .noPerson ? .noPerson : .notAllowed)
        }
        guard let issued = received?.token else { throw failed(.noToken) }
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

    private func failed(_ error: SignInError) -> SignInError {
        phase = .signedOut
        return error
    }

    public func cancel() {
        if case .signingIn = phase {
            phase = .signedOut
            attempt = nil
        }
    }

    public func signOut() throws {
        do {
            try storage.delete(.deviceToken)
        } catch {
            throw SignInError.storage
        }
        token = nil
        attempt = nil
        phase = .signedOut
    }

    private static func sameState(_ left: String, _ right: String) -> Bool {
        let a = Array(left.utf8)
        let b = Array(right.utf8)
        var difference = a.count ^ b.count
        for index in 0..<max(a.count, b.count) {
            difference |= Int(index < a.count ? a[index] : 0) ^ Int(index < b.count ? b[index] : 0)
        }
        return difference == 0
    }

    public func tokenRejected(_ rejected: String) throws {
        guard token == rejected else { return }
        try signOut()
    }
}
