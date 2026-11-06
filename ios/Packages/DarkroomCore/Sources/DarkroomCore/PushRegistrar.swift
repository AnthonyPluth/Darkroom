import Foundation

public enum PushToken {
    public static func hex(_ data: Data) -> String {
        data.map { (byte: UInt8) -> String in
            let digits = String(byte, radix: 16)
            return digits.count == 1 ? "0" + digits : digits
        }.joined()
    }

    public static func isValid(_ token: String) -> Bool {
        let bytes = token.utf8
        guard (64...200).contains(bytes.count) else { return false }
        return bytes.allSatisfy { ($0 >= 48 && $0 <= 57) || ($0 >= 97 && $0 <= 102) || ($0 >= 65 && $0 <= 70) }
    }
}

/// Keeps Darkroom's copy of this iPhone's APNs token current. `api` returns nil while no device is signed in.
public actor PushRegistrar {
    public enum Outcome: Equatable, Sendable {
        case registered
        case waiting
        case needsSignIn
        case heldByAnother
        case rejected
        case retryLater
    }

    public enum ForgetOutcome: Equatable, Sendable {
        case forgotten
        case alreadyEnded
        case failed
    }

    private let api: @Sendable () async -> PushTokenAPI?
    private var token: String?
    private var registered: String?
    private var generation = 0

    public init(api: @escaping @Sendable () async -> PushTokenAPI?) {
        self.api = api
    }

    public var currentToken: String? {
        token
    }

    public func tokenReceived(_ hex: String) {
        guard PushToken.isValid(hex) else { return }
        let lowered = hex.lowercased()
        if lowered != token {
            token = lowered
            registered = nil
        }
    }

    public func deviceSessionChanged() {
        generation += 1
        registered = nil
    }

    @discardableResult
    public func sync() async -> Outcome {
        guard let current = token, let client = await api() else { return .waiting }
        if registered == current { return .registered }
        let started = generation
        do {
            try await client.registerPushToken(current)
        } catch let error as APIError {
            return Self.outcome(of: error)
        } catch {
            return .retryLater
        }
        if token == current, generation == started { registered = current }
        return .registered
    }

    public func forget() async -> ForgetOutcome {
        generation += 1
        registered = nil
        guard let client = await api() else { return .alreadyEnded }
        do {
            try await client.forgetPushToken()
            return .forgotten
        } catch let error as APIError {
            switch error {
            case .notSignedIn, .unauthorized, .revoked: return .alreadyEnded
            case .rejected, .server, .transport, .decoding, .invalidResponse: return .failed
            }
        } catch {
            return .failed
        }
    }

    private static func outcome(of error: APIError) -> Outcome {
        switch error {
        case .notSignedIn, .unauthorized, .revoked: return .needsSignIn
        case .rejected(let status): return status == 409 ? .heldByAnother : .rejected
        case .server, .transport, .decoding, .invalidResponse: return .retryLater
        }
    }
}

/// Sign out in the order the design asks for: forget the push token while the bearer token still works, stop uploads, then
/// clear the session. If Darkroom can't be asked to forget the token the person stays signed in unless `forcing`.
public struct SignOutFlow: Sendable {
    public enum Outcome: Equatable, Sendable {
        case signedOut(pushForgotten: Bool)
        case pushNotForgotten
        case storage
    }

    private let push: PushRegistrar
    private let stopUploads: @Sendable () async -> Void
    private let clearSession: @Sendable () async throws -> Void

    public init(
        push: PushRegistrar,
        stopUploads: @escaping @Sendable () async -> Void = {},
        clearSession: @escaping @Sendable () async throws -> Void
    ) {
        self.push = push
        self.stopUploads = stopUploads
        self.clearSession = clearSession
    }

    public func signOut(forcing: Bool = false) async -> Outcome {
        let forgot = await push.forget()
        if forgot == .failed, !forcing { return .pushNotForgotten }
        await stopUploads()
        do {
            try await clearSession()
        } catch {
            return .storage
        }
        return .signedOut(pushForgotten: forgot != .failed)
    }
}
