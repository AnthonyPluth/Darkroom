import Foundation

public struct UploadBackgroundCheck: BackgroundCheck {
    private let status: @Sendable () async -> UploadStatus
    private let refresh: @Sendable () async -> UploadStatus

    public init(
        status: @escaping @Sendable () async -> UploadStatus,
        refresh: @escaping @Sendable () async -> UploadStatus
    ) {
        self.status = status
        self.refresh = refresh
    }

    public func run() async -> BackgroundOutcome {
        let before = await status().count(.uploaded)
        let after = await refresh()
        return Self.outcome(after: after, uploadedBefore: before)
    }

    public static func outcome(after: UploadStatus, uploadedBefore: Int) -> BackgroundOutcome {
        switch after.phase {
        case .needsSignIn: return .needsSignIn
        case .waitingForHomeNetwork: return .waitingForNetwork
        case .error, .photosAccessNeeded: return .failed
        case .idle, .uploading: return after.count(.uploaded) > uploadedBefore ? .uploaded : .nothingNew
        }
    }
}
