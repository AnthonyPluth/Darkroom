import Foundation

public enum NotificationAuthorization: Equatable, Sendable {
    case notAsked
    case allowed
    case denied
}

public protocol NotificationAuthorizer: Sendable {
    func current() async -> NotificationAuthorization
    func request() async -> Bool
}

public actor NotificationPermission {
    private let authorizer: NotificationAuthorizer
    public private(set) var state = NotificationAuthorization.notAsked

    public init(authorizer: NotificationAuthorizer) {
        self.authorizer = authorizer
    }

    @discardableResult
    public func ensureAsked() async -> NotificationAuthorization {
        var known = await authorizer.current()
        if known == .notAsked {
            known = await authorizer.request() ? .allowed : .denied
        }
        state = known
        return known
    }
}
