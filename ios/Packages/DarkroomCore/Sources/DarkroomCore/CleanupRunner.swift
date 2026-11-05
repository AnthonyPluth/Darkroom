import Foundation

public protocol PhotoDeleter: Sendable {
    func resources(of assetIDs: [String]) async throws -> [PhotoResource]
    func delete(_ assetIDs: [String]) async -> [String]
}

public struct PendingDeletion: Codable, Equatable, Sendable {
    public enum Stage: String, Codable, Sendable {
        case deleting
        case deleted
    }

    public let stage: Stage
    public let request: DeletedRequest

    public init(stage: Stage, request: DeletedRequest) {
        self.stage = stage
        self.request = request
    }

    public static func deleting(_ assetIDs: [String]) -> PendingDeletion {
        PendingDeletion(stage: .deleting, request: DeletedRequest(assetIDs: assetIDs))
    }

    public static func deleted(_ request: DeletedRequest) -> PendingDeletion {
        PendingDeletion(stage: .deleted, request: request)
    }
}

public protocol PendingDeletionReport: Sendable {
    func load() async throws -> PendingDeletion?
    func save(_ pending: PendingDeletion?) async throws
}

public actor InMemoryPendingDeletionReport: PendingDeletionReport {
    private var stored: PendingDeletion?

    public init() {}

    public func load() -> PendingDeletion? {
        stored
    }

    public func save(_ pending: PendingDeletion?) {
        stored = pending
    }
}

public actor FilePendingDeletionReport: PendingDeletionReport {
    private let url: URL

    public init(url: URL) {
        self.url = url
    }

    public func load() throws -> PendingDeletion? {
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            return nil
        }
        return try JSONDecoder().decode(PendingDeletion.self, from: data)
    }

    public func save(_ pending: PendingDeletion?) throws {
        guard let pending else {
            if FileManager.default.fileExists(atPath: url.path) {
                try FileManager.default.removeItem(at: url)
            }
            return
        }
        try JSONEncoder().encode(pending).write(to: url, options: .atomic)
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
        case reportUnsaved(CleanupReport)
        case recordNotCleared(CleanupReport)
        case reportUnreadable
        case needsSignIn
    }

    public static let reportRefusedReason = "report_refused"
    public static let reportRefusedMessage = "Darkroom refused the report of this removal."

    private struct Current {
        var ready: [AssetDescriptor] = []
    }

    private struct Uncleared {
        let record: PendingDeletion
        let report: CleanupReport
    }

    private let api: CleanupAPI
    private let store: ResourceStore
    private let versions: IngestVersions
    private let deleter: PhotoDeleter
    private let pending: PendingDeletionReport
    private let clock: @Sendable () -> Date
    private var flow = CleanupFlow()
    private var reportInFlight: Task<Outcome?, Error>?
    private var confirmedAs: [String: AssetDescriptor] = [:]
    private var written: PendingDeletion?
    private var uncleared: Uncleared?

    public init(
        api: CleanupAPI,
        store: ResourceStore,
        versions: IngestVersions,
        deleter: PhotoDeleter,
        pending: PendingDeletionReport,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.api = api
        self.store = store
        self.versions = versions
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
        let asked = try await current(among: nil).ready
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
        let now = try await current(among: shown)
        var command = try flow.handle(.remove(shown: shown, current: now.ready, at: clock()))
        while let next = command {
            switch next {
            case .fetchEligible:
                let refreshed = try await refresh()
                if case .listed = refreshed { return .refetched }
                return refreshed
            case let .confirm(request):
                confirmedAs = Dictionary(request.assets.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
                let reply: ConfirmReply
                do {
                    reply = try await api.confirm(request)
                } catch {
                    _ = try flow.handle(.confirmFailed)
                    return Self.needsSignIn(error) ? .needsSignIn : .confirmFailed
                }
                command = try flow.handle(.confirmed(reply))
            case let .delete(assetIDs):
                _ = await keep(.deleting(assetIDs))
                let unchanged = await stillAsConfirmed(assetIDs)
                let changed = assetIDs.filter { !unchanged.contains($0) }
                let kept = assetIDs.filter { unchanged.contains($0) }
                var removed: [String] = []
                if !kept.isEmpty { removed = await deleter.delete(kept) }
                command = try flow.handle(.deletionEnded(removed: removed, changed: changed))
            case let .report(request):
                return try await send(request)
            }
        }
        if case let .finished(report) = flow.state, let notCleared = await clear(showing: report) {
            return notCleared
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
        try await singleFlight { try await self.deliver(request) } ?? outcome()
    }

    private func sendEarlierReport() async throws -> Outcome? {
        try await singleFlight { try await self.deliverEarlier() }
    }

    private func singleFlight(_ work: @escaping @Sendable () async throws -> Outcome?) async throws -> Outcome? {
        if let running = reportInFlight { return try await running.value }
        let task = Task(operation: work)
        reportInFlight = task
        defer { reportInFlight = nil }
        return try await task.value
    }

    private func deliver(_ request: DeletedRequest) async throws -> Outcome? {
        let kept = await keep(.deleted(request)) || written != nil
        let reply: DeletedReply
        do {
            reply = try await api.deleted(request)
        } catch {
            guard let refused = Self.refusal(error, for: request) else {
                _ = try flow.handle(.reportFailed)
                guard case let .reporting(_, report) = flow.state else { return outcome() }
                if Self.needsSignIn(error) { return .needsSignIn }
                return kept ? .reportPending(report) : .reportUnsaved(report)
            }
            reply = refused
        }
        _ = try flow.handle(.reported(reply))
        guard case let .finished(report) = flow.state else { return outcome() }
        return await clear(showing: report) ?? outcome()
    }

    private func deliverEarlier() async throws -> Outcome? {
        let saved: PendingDeletion?
        do {
            saved = try await pending.load()
        } catch {
            return .reportUnreadable
        }
        written = saved
        guard let saved else {
            uncleared = nil
            return nil
        }
        if let uncleared, uncleared.record == saved {
            return await clear(showing: uncleared.report)
        }
        let request: DeletedRequest
        switch saved.stage {
        case .deleted:
            request = saved.request
        case .deleting:
            let present = Set(try await deleter.resources(of: saved.request.assetIDs).map(\.assetID))
            let gone = saved.request.assetIDs.filter { !present.contains($0) }
            guard !gone.isEmpty else { return await clear(showing: CleanupReport()) }
            request = DeletedRequest(assetIDs: gone)
            _ = await keep(.deleted(request))
        }
        var report = CleanupReport()
        report.removed = request.assetIDs
        let reply: DeletedReply
        do {
            reply = try await api.deleted(request)
        } catch {
            guard let refused = Self.refusal(error, for: request) else {
                return Self.needsSignIn(error) ? .needsSignIn : .reportPending(report)
            }
            reply = refused
        }
        let sent = Set(request.assetIDs)
        report.recorded = reply.recorded.filter { sent.contains($0) }
        report.unrecorded = reply.rejected.filter { sent.contains($0.assetID) }
        if let notCleared = await clear(showing: report) { return notCleared }
        return report.unrecorded.isEmpty ? nil : .finished(report)
    }

    private func keep(_ record: PendingDeletion) async -> Bool {
        do {
            try await pending.save(record)
        } catch {
            return false
        }
        written = record
        return true
    }

    private func clear(showing report: CleanupReport) async -> Outcome? {
        guard let record = written else { return nil }
        do {
            try await pending.save(nil)
        } catch {
            uncleared = Uncleared(record: record, report: report)
            return .recordNotCleared(report)
        }
        written = nil
        uncleared = nil
        return nil
    }

    private func stillAsConfirmed(_ assetIDs: [String]) async -> Set<String> {
        let sent = confirmedAs
        guard let now = try? await current(among: assetIDs) else { return [] }
        var unchanged: Set<String> = []
        for asset in now.ready {
            guard let confirmed = sent[asset.id], CleanupFlow.sameResources(asset, confirmed) else { continue }
            unchanged.insert(asset.id)
        }
        return unchanged
    }

    private func outcome() -> Outcome {
        switch flow.state {
        case let .fetched(list): return .listed(list)
        case let .finished(report): return .finished(report)
        case let .reporting(_, report): return .reportPending(report)
        case .idle, .confirming, .deleting: return .idle
        }
    }

    private struct Rows {
        var order: [String] = []
        var stored: [String: [ResourceKind: LocalResource]] = [:]
        var known: [String] = []
    }

    private func storedRows(among wanted: Set<String>?) async throws -> Rows {
        var rows = Rows()
        var settled: Set<String> = []
        for record in try await store.records(in: Set(ResourceState.allCases)) {
            let id = record.resource.key.assetID
            if let wanted, !wanted.contains(id) { continue }
            if rows.stored[id] == nil {
                rows.order.append(id)
                settled.insert(id)
            }
            rows.stored[id, default: [:]][record.resource.key.kind] = record.resource
            if record.resource.state != .uploaded { settled.remove(id) }
        }
        rows.known = rows.order.filter { settled.contains($0) && rows.stored[$0]?[.photo] != nil }
        return rows
    }

    private func current(among ids: [String]?) async throws -> Current {
        let wanted = ids.map { Set($0) }
        let before = try await storedRows(among: wanted)
        var ingested: [ResourceKey: String] = [:]
        for id in before.known {
            for key in (before.stored[id] ?? [:]).values.map(\.key) {
                ingested[key] = await versions.version(for: key)
            }
        }
        let after = try await storedRows(among: wanted)
        let stored = after.stored
        let known = after.known
        var library: [String: [PhotoResource]] = [:]
        for resource in try await deleter.resources(of: ids ?? known) {
            library[resource.assetID, default: []].append(resource)
        }
        var result = Current()
        for id in known {
            guard let now = library[id], let rows = stored[id], now.contains(where: { $0.kind == .photo }) else { continue }
            var kinds: Set<ResourceKind> = []
            var descriptors: [ResourceDescriptor] = []
            for resource in now {
                guard kinds.insert(resource.kind).inserted, let row = rows[resource.kind],
                      ingested[resource.key] == resource.version else { break }
                descriptors.append(row.descriptor)
            }
            guard descriptors.count == now.count else { continue }
            result.ready.append(AssetDescriptor(id: id, resources: descriptors.sorted { $0.kind.order < $1.kind.order }))
        }
        return result
    }

    private static func refusal(_ error: Error, for request: DeletedRequest) -> DeletedReply? {
        guard case let .rejected(status)? = error as? APIError, status != 408, status != 429 else { return nil }
        return DeletedReply(recorded: [], rejected: request.assetIDs.map {
            RejectedAsset(assetID: $0, reason: reportRefusedReason, message: reportRefusedMessage)
        })
    }

    private static func needsSignIn(_ error: Error) -> Bool {
        guard let api = error as? APIError else { return false }
        return api == .unauthorized || api == .notSignedIn
    }
}
