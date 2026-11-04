import Foundation

public enum CleanupPhase: String, Sendable {
    case idle
    case fetched
    case confirming
    case deleting
    case reporting
}

public protocol CleanupFlow: Sendable {
    func phase() async -> CleanupPhase
}
