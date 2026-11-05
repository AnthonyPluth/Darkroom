import Foundation

public enum CleanupPhase: String, Sendable {
    case idle
    case fetched
    case confirming
    case deleting
    case reporting
    case finished
}

public struct EligibleList: Equatable, Sendable {
    public static let maxAge: TimeInterval = 60 * 60
    public static let maxAssets = 500

    public struct Entry: Equatable, Sendable {
        public let asset: AssetDescriptor
        public let bytes: Int64

        public var id: String { asset.id }
    }

    public let entries: [Entry]
    public let generatedAt: Date

    public var ids: [String] { entries.map(\.id) }
    public var totalBytes: Int64 { entries.reduce(0) { $0 + $1.bytes } }

    public func isStale(at now: Date) -> Bool {
        let age = now.timeIntervalSince(generatedAt)
        return age < 0 || age >= Self.maxAge
    }
}

public struct CleanupReport: Equatable, Sendable {
    public static let notConfirmedReason = "not_confirmed_for_deletion"
    public static let notConfirmedMessage = "Darkroom did not confirm this photo for removal."

    public var changed: [String] = []
    public var refused: [RejectedAsset] = []
    public var removed: [String] = []
    public var notRemoved: [String] = []
    public var recorded: [String] = []
    public var unrecorded: [RejectedAsset] = []

    public init() {}
}

public enum CleanupState: Equatable, Sendable {
    case idle
    case fetched(EligibleList)
    case confirming(EligibleList, sent: [String], report: CleanupReport)
    case deleting(confirmed: [String], report: CleanupReport)
    case reporting(DeletedRequest, report: CleanupReport)
    case finished(CleanupReport)

    public var phase: CleanupPhase {
        switch self {
        case .idle: return .idle
        case .fetched: return .fetched
        case .confirming: return .confirming
        case .deleting: return .deleting
        case .reporting: return .reporting
        case .finished: return .finished
        }
    }
}

public enum CleanupEvent: Equatable, Sendable {
    case listed(EligibleReply, asked: [AssetDescriptor], at: Date)
    case listFailed
    case remove(shown: [String], current: [AssetDescriptor], at: Date)
    case confirmed(ConfirmReply)
    case confirmFailed
    case deletionEnded(removed: [String], changed: [String] = [])
    case reported(DeletedReply)
    case reportFailed
    case retryReport
    case reset
}

public enum CleanupCommand: Equatable, Sendable {
    case fetchEligible
    case confirm(ConfirmRequest)
    case delete(assetIDs: [String])
    case report(DeletedRequest)
}

public enum CleanupFlowError: Error, Equatable, Sendable {
    case notNow(CleanupPhase)
}

public struct CleanupFlow: Equatable, Sendable {
    public private(set) var state: CleanupState = .idle
    public private(set) var cleanupEnabled: Bool?

    public init() {}

    public var phase: CleanupPhase { state.phase }

    public mutating func handle(_ event: CleanupEvent) throws -> CleanupCommand? {
        switch event {
        case let .listed(reply, asked, at):
            try require([.idle, .fetched, .finished])
            return list(reply, asked: asked, at: at)
        case .listFailed, .reset:
            try require([.idle, .fetched, .finished])
            state = .idle
            return nil
        case let .remove(shown, current, at):
            guard case let .fetched(list) = state else { throw CleanupFlowError.notNow(phase) }
            return remove(from: list, shown: shown, current: current, at: at)
        case let .confirmed(reply):
            guard case let .confirming(_, sent, report) = state else { throw CleanupFlowError.notNow(phase) }
            return confirmed(reply, sent: sent, report: report)
        case .confirmFailed:
            guard case let .confirming(list, _, _) = state else { throw CleanupFlowError.notNow(phase) }
            state = .fetched(list)
            return nil
        case let .deletionEnded(removed, changed):
            guard case let .deleting(confirmed, report) = state else { throw CleanupFlowError.notNow(phase) }
            return deletionEnded(removed: removed, changed: changed, confirmed: confirmed, report: report)
        case let .reported(reply):
            guard case let .reporting(request, report) = state else { throw CleanupFlowError.notNow(phase) }
            var finished = report
            let sent = Set(request.assetIDs)
            finished.recorded = reply.recorded.filter { sent.contains($0) }
            finished.unrecorded = reply.rejected.filter { sent.contains($0.assetID) }
            state = .finished(finished)
            return nil
        case .reportFailed:
            guard case .reporting = state else { throw CleanupFlowError.notNow(phase) }
            return nil
        case .retryReport:
            guard case let .reporting(request, _) = state else { throw CleanupFlowError.notNow(phase) }
            return .report(request)
        }
    }

    private func require(_ phases: Set<CleanupPhase>) throws {
        guard phases.contains(phase) else { throw CleanupFlowError.notNow(phase) }
    }

    private mutating func list(_ reply: EligibleReply, asked: [AssetDescriptor], at: Date) -> CleanupCommand? {
        cleanupEnabled = reply.cleanupEnabled
        guard reply.cleanupEnabled else {
            state = .idle
            return nil
        }
        var askedByID: [String: AssetDescriptor] = [:]
        for asset in asked where askedByID[asset.id] == nil {
            askedByID[asset.id] = asset
        }
        var seen: Set<String> = []
        var entries: [EligibleList.Entry] = []
        for offered in reply.eligible where entries.count < EligibleList.maxAssets {
            guard let asset = askedByID[offered.assetID], seen.insert(offered.assetID).inserted else { continue }
            entries.append(EligibleList.Entry(asset: asset, bytes: offered.bytes))
        }
        state = entries.isEmpty ? .idle : .fetched(EligibleList(entries: entries, generatedAt: at))
        return nil
    }

    private mutating func remove(from list: EligibleList, shown: [String], current: [AssetDescriptor], at: Date) -> CleanupCommand? {
        if list.isStale(at: at) {
            state = .idle
            return .fetchEligible
        }
        let shownIDs = Set(shown)
        let picked = list.entries.filter { shownIDs.contains($0.id) }
        guard !picked.isEmpty else { return nil }
        var currentByID: [String: AssetDescriptor] = [:]
        for asset in current where currentByID[asset.id] == nil {
            currentByID[asset.id] = asset
        }
        var report = CleanupReport()
        var sending: [AssetDescriptor] = []
        for entry in picked {
            if let now = currentByID[entry.id], Self.sameResources(now, entry.asset) {
                sending.append(entry.asset)
            } else {
                report.changed.append(entry.id)
            }
        }
        guard !sending.isEmpty else {
            state = .finished(report)
            return nil
        }
        state = .confirming(list, sent: sending.map(\.id), report: report)
        return .confirm(ConfirmRequest(assets: sending))
    }

    private mutating func confirmed(_ reply: ConfirmReply, sent: [String], report: CleanupReport) -> CleanupCommand? {
        var report = report
        let passed = Set(reply.confirmed)
        let sentIDs = Set(sent)
        let confirmed = sent.filter { passed.contains($0) }
        var refused: [String: RejectedAsset] = [:]
        for rejected in reply.rejected where sentIDs.contains(rejected.assetID) && refused[rejected.assetID] == nil {
            refused[rejected.assetID] = rejected
        }
        report.refused = sent.filter { !passed.contains($0) }.map {
            refused[$0] ?? RejectedAsset(
                assetID: $0, reason: CleanupReport.notConfirmedReason, message: CleanupReport.notConfirmedMessage
            )
        }
        guard !confirmed.isEmpty else {
            state = .finished(report)
            return nil
        }
        state = .deleting(confirmed: confirmed, report: report)
        return .delete(assetIDs: confirmed)
    }

    private mutating func deletionEnded(
        removed: [String],
        changed: [String],
        confirmed: [String],
        report: CleanupReport
    ) -> CleanupCommand? {
        var report = report
        let gone = Set(removed)
        let moved = Set(changed).subtracting(gone)
        report.changed += confirmed.filter { moved.contains($0) }
        report.removed = confirmed.filter { gone.contains($0) }
        report.notRemoved = confirmed.filter { !gone.contains($0) && !moved.contains($0) }
        guard !report.removed.isEmpty else {
            state = .finished(report)
            return nil
        }
        let request = DeletedRequest(assetIDs: report.removed)
        state = .reporting(request, report: report)
        return .report(request)
    }

    static func sameResources(_ left: AssetDescriptor, _ right: AssetDescriptor) -> Bool {
        guard left.id == right.id, left.resources.count == right.resources.count else { return false }
        let byKind = { (asset: AssetDescriptor) in asset.resources.sorted { $0.kind.order < $1.kind.order } }
        return byKind(left) == byKind(right)
    }
}
