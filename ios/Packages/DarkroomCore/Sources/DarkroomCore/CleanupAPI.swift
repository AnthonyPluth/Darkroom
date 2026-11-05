import Foundation

public protocol CleanupAPI: Sendable {
    func eligible(_ request: EligibleRequest) async throws -> EligibleReply
    func confirm(_ request: ConfirmRequest) async throws -> ConfirmReply
    func deleted(_ request: DeletedRequest) async throws -> DeletedReply
}

extension APIClient: CleanupAPI {}
