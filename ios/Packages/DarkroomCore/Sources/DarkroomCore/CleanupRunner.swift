import Foundation

public protocol PhotoDeleter: Sendable {
    func present(_ assetIDs: [String]) async -> Set<String>
    func delete(_ assetIDs: [String]) async -> [String]
}

public protocol PendingDeletionReport: Sendable {
    func load() async -> DeletedRequest?
    func save(_ request: DeletedRequest?) async
}

public actor InMemoryPendingDeletionReport: PendingDeletionReport {
    private var stored: DeletedRequest?

    public init() {}

    public func load() -> DeletedRequest? {
        stored
    }

    public func save(_ request: DeletedRequest?) {
        stored = request
    }
}

public actor FilePendingDeletionReport: PendingDeletionReport {
    private let url: URL

    public init(url: URL) {
        self.url = url
    }

    public func load() -> DeletedRequest? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(DeletedRequest.self, from: data)
    }

    public func save(_ request: DeletedRequest?) {
        guard let request else {
            try? FileManager.default.removeItem(at: url)
            return
        }
        guard let data = try? JSONEncoder().encode(request) else { return }
        try? data.write(to: url, options: .atomic)
    }
}

public actor CleanupRunner {
    public enum Outcome: Equatable, Sendable {
        case idle
        case listed(EligibleList)
        case refetched
        case confirmFailed
        case finished(CleanupReport)
        case reportPending(CleanupReport)
        case needsSignIn
    }

    private let api: CleanupAPI
    private let store: ResourceStore
    private let deleter: PhotoDeleter
    private let pending: PendingDeletionReport
    private let clock: @Sendable () -> Date
    private var flow = CleanupFlow()

    public init(
        api: CleanupAPI,
        store: ResourceStore,
        deleter: PhotoDeleter,
        pending: PendingDeletionReport,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.api = api
        self.store = store
        self.deleter = deleter
        self.pending = pending
        self.clock = clock
    }

    public var state: CleanupState { flow.state }
    public var cleanupEnabled: Bool? { flow.cleanupEnabled }

    public func refresh() async throws -> Outcome {
        if flow.phase == .finished || flow.phase == .fetched {
            _ = try flow.handle(.reset)
        }
        guard flow.phase == .idle else { throw CleanupFlowError.notNow(flow.phase) }
        if let earlier = try await sendEarlierReport() { return earlier }
        let asked = try await candidates()
        guard !asked.isEmpty else { return .idle }
        var merged: [EligibleAsset] = []
        var enabled = true
        for start in stride(from: 0, to: asked.count, by: EligibleList.maxAssets) where enabled {
            try Task.checkCancellation()
            let batch = Array(asked[start..<min(start + EligibleList.maxAssets, asked.count)])
            do {
                let reply = try await api.eligible(EligibleRequest(assets: batch))
                enabled = enabled && reply.cleanupEnabled
                merged += reply.eligible
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                _ = try flow.handle(.listFailed)
                if Self.needsSignIn(error) { return .needsSignIn }
                throw error
            }
        }
        let reply = EligibleReply(cleanupEnabled: enabled, eligible: enabled ? merged : [])
        _ = try flow.handle(.listed(reply, asked: asked, at: clock()))
        return outcome()
    }

    public func remove(shown: [String]) async throws -> Outcome {
        guard case .fetched = flow.state else { throw CleanupFlowError.notNow(flow.phase) }
        let present = await deleter.present(shown)
        let current = try await candidates().filter { present.contains($0.id) }
        var command = try flow.handle(.remove(shown: shown.filter { present.contains($0) }, current: current, at: clock()))
        while let next = command {
            switch next {
            case .fetchEligible:
                let refreshed = try await refresh()
                if case .listed = refreshed { return .refetched }
                return refreshed
            case let .confirm(request):
                let reply: ConfirmReply
                do {
                    reply = try await api.confirm(request)
                } catch {
                    _ = try flow.handle(.confirmFailed)
                    return Self.needsSignIn(error) ? .needsSignIn : .confirmFailed
                }
                command = try flow.handle(.confirmed(reply))
            case let .delete(assetIDs):
                let removed = await deleter.delete(assetIDs)
                command = try flow.handle(.deletionEnded(removed: removed))
            case let .report(request):
                return try await send(request)
            }
        }
        return outcome()
    }

    public func retryReport() async throws -> Outcome {
        if case .reporting = flow.state, case let .report(request)? = try flow.handle(.retryReport) {
            return try await send(request)
        }
        return try await sendEarlierReport() ?? outcome()
    }

    private func send(_ request: DeletedRequest) async throws -> Outcome {
        await pending.save(request)
        let reply: DeletedReply
        do {
            reply = try await api.deleted(request)
        } catch {
            _ = try flow.handle(.reportFailed)
            guard case let .reporting(_, report) = flow.state else { return outcome() }
            return Self.needsSignIn(error) ? .needsSignIn : .reportPending(report)
        }
        await pending.save(nil)
        _ = try flow.handle(.reported(reply))
        return outcome()
    }

    private func sendEarlierReport() async throws -> Outcome? {
        guard let saved = await pending.load() else { return nil }
        do {
            _ = try await api.deleted(saved)
        } catch {
            var report = CleanupReport()
            report.removed = saved.assetIDs
            return Self.needsSignIn(error) ? .needsSignIn : .reportPending(report)
        }
        await pending.save(nil)
        return nil
    }

    private func outcome() -> Outcome {
        switch flow.state {
        case let .fetched(list): return .listed(list)
        case let .finished(report): return .finished(report)
        case let .reporting(_, report): return .reportPending(report)
        case .idle, .confirming, .deleting: return .idle
        }
    }

    private func candidates() async throws -> [AssetDescriptor] {
        let records = try await store.records(in: Set(ResourceState.allCases))
        var order: [String] = []
        var byAsset: [String: [LocalResource]] = [:]
        for record in records {
            let id = record.resource.key.assetID
            if byAsset[id] == nil { order.append(id) }
            byAsset[id, default: []].append(record.resource)
        }
        let ready = order.filter { id in
            let resources = byAsset[id] ?? []
            return resources.contains { $0.key.kind == .photo } && resources.allSatisfy { $0.state == .uploaded }
        }
        let present = await deleter.present(ready)
        return ready.filter { present.contains($0) }.map { id in
            AssetDescriptor(
                id: id,
                resources: (byAsset[id] ?? []).sorted { $0.key.kind.order < $1.key.kind.order }.map(\.descriptor)
            )
        }
    }

    private static func needsSignIn(_ error: Error) -> Bool {
        guard let api = error as? APIError else { return false }
        return api == .unauthorized || api == .notSignedIn
    }
}
