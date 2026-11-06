import Foundation

public protocol UploadRunner: Sendable {
    func run(ignoringBackoff: Bool) async throws -> UploadCoordinator.Result
}

extension UploadCoordinator: UploadRunner {}

public struct FailedResource: Equatable, Sendable {
    public let key: ResourceKey
    public let filename: String
    public let reason: String
    public let recoverable: Bool
    public let canRetry: Bool

    public init(key: ResourceKey, filename: String, reason: String, recoverable: Bool = false, canRetry: Bool = false) {
        self.key = key
        self.filename = filename
        self.reason = reason
        self.recoverable = recoverable
        self.canRetry = canRetry
    }
}

public struct UploadStatus: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        case uploading
        case waitingForHomeNetwork
        case needsSignIn
        case photosAccessNeeded
        case error(String)
    }

    public let phase: Phase
    public let counts: [ResourceState: Int]
    public let failures: [FailedResource]
    public let lastSync: Date?
    public let firstScanFinished: Bool

    public init(phase: Phase, counts: [ResourceState: Int], failures: [FailedResource], lastSync: Date?, firstScanFinished: Bool) {
        self.phase = phase
        self.counts = counts
        self.failures = failures
        self.lastSync = lastSync
        self.firstScanFinished = firstScanFinished
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
        case accessNeeded
        case failed(String)
    }

    private let runner: UploadRunner
    private let store: ResourceStore
    private let scanner: LibraryScanner?
    private let syncState: SyncStateStore
    private let clock: @Sendable () -> Date
    private var run = Run.idle

    public init(
        runner: UploadRunner,
        store: ResourceStore,
        scanner: LibraryScanner? = nil,
        syncState: SyncStateStore = InMemorySyncStateStore(),
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.runner = runner
        self.store = store
        self.scanner = scanner
        self.syncState = syncState
        self.clock = clock
    }

    public func status() async -> UploadStatus {
        let state = await syncState.load()
        do {
            let counts = try await store.counts()
            let failed = try await store.records(in: [.failed]).map {
                FailedResource(
                    key: $0.resource.key,
                    filename: $0.resource.filename,
                    reason: $0.failureReason ?? Self.noReasonRecorded,
                    recoverable: $0.recoverable,
                    canRetry: $0.recoverable && $0.resource.hasDescriptor
                )
            }
            return UploadStatus(
                phase: phase(), counts: counts, failures: failed,
                lastSync: state.lastSync, firstScanFinished: state.firstScanFinished
            )
        } catch {
            return UploadStatus(
                phase: .error(Self.queueUnreadable), counts: [:], failures: [],
                lastSync: state.lastSync, firstScanFinished: state.firstScanFinished
            )
        }
    }

    public func syncNow() async -> UploadStatus {
        await sync(manual: true, retrying: nil)
    }

    public func retry(_ keys: [ResourceKey]) async -> UploadStatus {
        await sync(manual: true, retrying: Set(keys))
    }

    public func refresh() async -> UploadStatus {
        await sync(manual: false, retrying: [])
    }

    public func signInChanged() {
        if case .finished(.needsSignIn) = run { run = .idle }
    }

    private func sync(manual: Bool, retrying: Set<ResourceKey>?) async -> UploadStatus {
        guard run != .running else { return await status() }
        let before = run
        run = .running
        do {
            if retrying?.isEmpty != true {
                _ = try? await store.retryFailures(keys: retrying)
            }
            try await scan()
            let result = try await runner.run(ignoringBackoff: manual)
            run = .finished(result.outcome)
            if result.outcome == .finished, result.checkinAccepted {
                var state = await syncState.load()
                state.lastSync = clock()
                try? await syncState.save(state)
            }
        } catch is CancellationError {
            run = before
        } catch PhotoLibraryError.accessDenied {
            run = .accessNeeded
        } catch {
            run = .failed(error is ResourceStoreError ? Self.queueUnreadable : Self.syncStopped)
        }
        return await status()
    }

    private func scan() async throws {
        guard let scanner else { return }
        _ = try await scanner.run()
        var state = await syncState.load()
        guard !state.firstScanFinished else { return }
        state.firstScanFinished = true
        try? await syncState.save(state)
    }

    private func phase() -> UploadStatus.Phase {
        switch run {
        case .running: return .uploading
        case .failed(let message): return .error(message)
        case .accessNeeded: return .photosAccessNeeded
        case .finished(.needsSignIn): return .needsSignIn
        case .finished(.waitingForNetwork): return .waitingForHomeNetwork
        case .finished(.finished), .idle: return .idle
        }
    }
}
