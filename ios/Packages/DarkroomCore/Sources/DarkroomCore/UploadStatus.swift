import Foundation

public protocol UploadRunner: Sendable {
    func run() async throws -> UploadCoordinator.Result
}

extension UploadCoordinator: UploadRunner {}

public struct FailedResource: Equatable, Sendable {
    public let key: ResourceKey
    public let filename: String
    public let reason: String

    public init(key: ResourceKey, filename: String, reason: String) {
        self.key = key
        self.filename = filename
        self.reason = reason
    }
}

public struct UploadStatus: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        case uploading
        case waitingForHomeNetwork
        case needsSignIn
        case error(String)
    }

    public let phase: Phase
    public let counts: [ResourceState: Int]
    public let failures: [FailedResource]
    public let lastSync: Date?

    public init(phase: Phase, counts: [ResourceState: Int], failures: [FailedResource], lastSync: Date?) {
        self.phase = phase
        self.counts = counts
        self.failures = failures
        self.lastSync = lastSync
    }

    public func count(_ state: ResourceState) -> Int {
        counts[state] ?? 0
    }
}

public actor UploadStatusTracker {
    public static let queueUnreadable = "The upload queue could not be read."
    public static let syncStopped = "The last sync stopped unexpectedly."
    public static let noReasonRecorded = "No reason was recorded."

    private enum Run: Equatable {
        case idle
        case running
        case finished(UploadCoordinator.Outcome)
        case failed(String)
    }

    private let runner: UploadRunner
    private let store: ResourceStore
    private let clock: @Sendable () -> Date
    private var run = Run.idle
    private var lastSync: Date?

    public init(runner: UploadRunner, store: ResourceStore, clock: @escaping @Sendable () -> Date = { Date() }) {
        self.runner = runner
        self.store = store
        self.clock = clock
    }

    public func status() async -> UploadStatus {
        do {
            let counts = try await store.counts()
            let failed = try await store.records(in: [.failed]).map {
                FailedResource(
                    key: $0.resource.key,
                    filename: $0.resource.filename,
                    reason: $0.failureReason ?? Self.noReasonRecorded
                )
            }
            return UploadStatus(phase: phase(), counts: counts, failures: failed, lastSync: lastSync)
        } catch {
            return UploadStatus(phase: .error(Self.queueUnreadable), counts: [:], failures: [], lastSync: lastSync)
        }
    }

    public func syncNow() async -> UploadStatus {
        guard run != .running else { return await status() }
        let before = run
        run = .running
        do {
            let result = try await runner.run()
            run = .finished(result.outcome)
            if result.outcome == .finished { lastSync = clock() }
        } catch is CancellationError {
            run = before
        } catch {
            run = .failed(error is ResourceStoreError ? Self.queueUnreadable : Self.syncStopped)
        }
        return await status()
    }

    public func signInChanged() {
        if case .finished(.needsSignIn) = run { run = .idle }
    }

    private func phase() -> UploadStatus.Phase {
        switch run {
        case .running: return .uploading
        case .failed(let message): return .error(message)
        case .finished(.needsSignIn): return .needsSignIn
        case .finished(.waitingForNetwork): return .waitingForHomeNetwork
        case .finished(.finished), .idle: return .idle
        }
    }
}
