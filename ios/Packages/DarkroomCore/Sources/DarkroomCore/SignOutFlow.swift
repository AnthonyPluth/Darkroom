import Foundation

public struct SignOutFlow: Sendable {
    public enum Outcome: Equatable, Sendable {
        case signedOut
        case pushNotForgotten
        case storage
    }

    private let push: PushTokenAPI?
    private let stopUploads: @Sendable () async -> Void
    private let clearSession: @Sendable () async throws -> Void
    private let restartUploads: @Sendable () async -> Void

    public init(
        push: PushTokenAPI?,
        stopUploads: @escaping @Sendable () async -> Void,
        clearSession: @escaping @Sendable () async throws -> Void,
        restartUploads: @escaping @Sendable () async -> Void
    ) {
        self.push = push
        self.stopUploads = stopUploads
        self.clearSession = clearSession
        self.restartUploads = restartUploads
    }

    public func signOut() async -> Outcome {
        if let push, !(await Self.forgot(push)) { return .pushNotForgotten }
        await stopUploads()
        do {
            try await clearSession()
        } catch {
            await restartUploads()
            return .storage
        }
        return .signedOut
    }

    private static func forgot(_ push: PushTokenAPI) async -> Bool {
        do {
            try await push.forgetPushToken()
            return true
        } catch let error as APIError {
            switch error {
            case .notSignedIn, .unauthorized, .revoked: return true
            default: return false
            }
        } catch {
            return false
        }
    }
}
