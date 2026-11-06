import Foundation
import XCTest
@testable import DarkroomCore

private actor FakeCleanupAPI: CleanupAPI {
    var eligibleIDs: Set<String> = []
    var enabled = true
    var refuse: [String: String] = [:]
    var eligibleError: Error?
    var confirmError: Error?
    var deletedErrors: [Error] = []
    var holdDeleted = false
    var onConfirm: (@Sendable () async -> Void)?
    private var held: [CheckedContinuation<Void, Never>] = []
    private(set) var eligibleCalls: [EligibleRequest] = []
    private(set) var confirmCalls: [ConfirmRequest] = []
    private(set) var deletedCalls: [DeletedRequest] = []

    func set(eligible: Set<String>, enabled: Bool = true) {
        eligibleIDs = eligible
        self.enabled = enabled
    }

    func set(refuse: [String: String]) { self.refuse = refuse }
    func set(eligibleError: Error?) { self.eligibleError = eligibleError }
    func set(confirmError: Error?) { self.confirmError = confirmError }
    func set(deletedErrors: [Error]) { self.deletedErrors = deletedErrors }
    func set(holdDeleted: Bool) { self.holdDeleted = holdDeleted }
    func set(onConfirm: (@Sendable () async -> Void)?) { self.onConfirm = onConfirm }
    var heldCount: Int { held.count }

    func release() {
        holdDeleted = false
        let waiting = held
        held = []
        waiting.forEach { $0.resume() }
    }

    func eligible(_ request: EligibleRequest) async throws -> EligibleReply {
        eligibleCalls.append(request)
        if let eligibleError { throw eligibleError }
        let offered = enabled ? request.assets.filter { eligibleIDs.contains($0.id) } : []
        return EligibleReply(cleanupEnabled: enabled, eligible: offered.map { EligibleAsset(assetID: $0.id, bytes: 10) })
    }

    func confirm(_ request: ConfirmRequest) async throws -> ConfirmReply {
        confirmCalls.append(request)
        if let onConfirm { await onConfirm() }
        if let confirmError { throw confirmError }
        let ids = request.assets.map(\.id)
        return ConfirmReply(
            confirmed: ids.filter { refuse[$0] == nil },
            rejected: ids.compactMap { id in refuse[id].map { RejectedAsset(assetID: id, reason: $0, message: "Refused.") } }
        )
    }

    func deleted(_ request: DeletedRequest) async throws -> DeletedReply {
        deletedCalls.append(request)
        if holdDeleted {
            await withCheckedContinuation { held.append($0) }
        }
        if !deletedErrors.isEmpty { throw deletedErrors.removeFirst() }
        return DeletedReply(recorded: request.assetIDs, rejected: [])
    }
}

private actor FakeLibrary: PhotoDeleter {
    var assets: [String: [PhotoResource]] = [:]
    var keep: Set<String> = []
    var resourcesError: Error?
    var watched: PendingDeletionReport?
    var onDelete: (@Sendable () async -> Void)?
    private(set) var deleteCalls: [[String]] = []
    private(set) var recordsAtDelete: [PendingDeletion?] = []

    func set(keep: Set<String>) { self.keep = keep }
    func set(resourcesError: Error?) { self.resourcesError = resourcesError }
    func set(watched: PendingDeletionReport?) { self.watched = watched }
    func set(onDelete: (@Sendable () async -> Void)?) { self.onDelete = onDelete }
    func drop(_ id: String) { assets[id] = nil }

    func add(_ resource: PhotoResource) {
        assets[resource.assetID, default: []].removeAll { $0.kind == resource.kind }
        assets[resource.assetID, default: []].append(resource)
    }

    func resources(of assetIDs: [String]) throws -> [PhotoResource] {
        if let resourcesError { throw resourcesError }
        return assetIDs.flatMap { assets[$0] ?? [] }
    }

    func delete(_ assetIDs: [String]) async -> [String] {
        if let hook = onDelete {
            onDelete = nil
            await hook()
        }
        if let watched { recordsAtDelete.append(try? await watched.load()) }
        deleteCalls.append(assetIDs)
        let removed = assetIDs.filter { assets[$0] != nil && !keep.contains($0) }
        removed.forEach { assets[$0] = nil }
        return removed
    }
}

private actor FakeVersions: IngestVersions {
    private var storage: [ResourceKey: String] = [:]
    private var onLookup: (@Sendable () async -> Void)?

    func set(onLookup: (@Sendable () async -> Void)?) { self.onLookup = onLookup }

    func version(for key: ResourceKey) async -> String? {
        if let hook = onLookup {
            onLookup = nil
            await hook()
        }
        return storage[key]
    }

    func setVersion(_ version: String?, for key: ResourceKey) {
        storage[key] = version
    }
}

private actor FailingPendingReport: PendingDeletionReport {
    func load() -> PendingDeletion? { nil }

    func save(_ pending: PendingDeletion?) throws {
        throw CocoaError(.fileWriteOutOfSpace)
    }
}

private actor UnclearablePendingReport: PendingDeletionReport {
    private var stored: PendingDeletion?
    private var clearsFail = true
    private(set) var clearAttempts = 0

    func set(clearsFail: Bool) { self.clearsFail = clearsFail }

    func load() -> PendingDeletion? { stored }

    func save(_ pending: PendingDeletion?) throws {
        guard pending == nil else {
            stored = pending
            return
        }
        clearAttempts += 1
        if clearsFail { throw CocoaError(.fileWriteNoPermission) }
        stored = nil
    }
}

private actor Outcomes {
    private(set) var errors: [CleanupFlowError?] = []

    func add(_ error: CleanupFlowError?) { errors.append(error) }
}

private final class Clock: @unchecked Sendable {
    private let lock = NSLock()
    private var now = Date(timeIntervalSince1970: 2_000_000)

    func read() -> Date {
        lock.lock()
        defer { lock.unlock() }
        return now
    }

    func advance(_ seconds: TimeInterval) {
        lock.lock()
        now = now.addingTimeInterval(seconds)
        lock.unlock()
    }
}

final class CleanupRunnerTests: XCTestCase {
    private struct Rig {
        let api = FakeCleanupAPI()
        let store = InMemoryResourceStore()
        let library = FakeLibrary()
        let versions = FakeVersions()
        let pending: PendingDeletionReport
        let clock = Clock()

        init(pending: PendingDeletionReport = InMemoryPendingDeletionReport()) {
            self.pending = pending
        }

        func runner() -> CleanupRunner {
            let clock = clock
            return CleanupRunner(
                api: api, store: store, versions: versions, deleter: library, pending: pending, clock: { clock.read() }
            )
        }

        func onPhone(_ id: String, _ kind: ResourceKind = .photo, version: String = "v1") async {
            await library.add(PhotoResource(assetID: id, kind: kind, originalFilename: "\(id).HEIC", version: version))
        }

        func ingested(_ resources: [LocalResource], version: String = "v1") async throws {
            try await store.upsert(resources)
            for resource in resources {
                await versions.setVersion(version, for: resource.key)
            }
        }

        func both(_ resources: [LocalResource]) async throws {
            try await ingested(resources)
            for resource in resources {
                await onPhone(resource.key.assetID, resource.key.kind)
            }
        }
    }

    private func resource(_ id: String, _ kind: ResourceKind = .photo, state: ResourceState = .uploaded) -> LocalResource {
        LocalResource(key: ResourceKey(assetID: id, kind: kind), sha1: "sha-\(id)-\(kind.rawValue)", bytes: 10, filename: "\(id).HEIC", state: state)
    }

    private func rig(
        _ ids: [String],
        eligible: Set<String>? = nil,
        pending: PendingDeletionReport = InMemoryPendingDeletionReport()
    ) async throws -> Rig {
        let rig = Rig(pending: pending)
        try await rig.both(ids.map { resource($0) })
        await rig.api.set(eligible: eligible ?? Set(ids))
        return rig
    }

    func testTwoRetriesAtOnceSendTheReportOnce() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(deletedErrors: [APIError.transport("-1001")])
        let runner = rig.runner()
        _ = try await runner.refresh()
        _ = try await runner.remove(shown: ["a"])
        await rig.api.set(holdDeleted: true)
        let first = Task { try await runner.retryReport() }
        while await rig.api.heldCount == 0 { await Task.yield() }
        let second = Task { try await runner.retryReport() }
        for _ in 0..<200 { await Task.yield() }
        await rig.api.release()
        let outcomes = [try await first.value, try await second.value]
        let reports = await rig.api.deletedCalls.count
        XCTAssertEqual(reports, 2)
        for outcome in outcomes {
            guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
            XCTAssertEqual(report.recorded, ["a"])
        }
    }

    func testAReportLeftFromAnEarlierLaunchIsSentBeforeANewList() async throws {
        let rig = try await rig(["b"])
        try await rig.pending.save(.deleted(DeletedRequest(assetIDs: ["a"])))
        await rig.api.set(deletedErrors: [APIError.transport("-1009")])
        let first = try await rig.runner().refresh()
        guard case let .reportPending(report) = first else { return XCTFail("not pending: \(first)") }
        XCTAssertEqual(report.removed, ["a"])
        let asked = await rig.api.eligibleCalls.count
        XCTAssertEqual(asked, 0)
        let second = try await rig.runner().refresh()
        guard case .listed = second else { return XCTFail("not listed: \(second)") }
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"]), DeletedRequest(assetIDs: ["a"])])
        let cleared = try await rig.pending.load()
        XCTAssertNil(cleared)
    }

    func testARoundInFlightCannotBeListedAgain() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(deletedErrors: [APIError.transport("-1001")])
        let runner = rig.runner()
        _ = try await runner.refresh()
        _ = try await runner.remove(shown: ["a"])
        do {
            _ = try await runner.refresh()
            XCTFail("expected notNow")
        } catch {
            XCTAssertEqual(error as? CleanupFlowError, .notNow(.reporting))
        }
    }

    func testTheConfirmedIdsAreSavedBeforePhotoKitDeletes() async throws {
        let rig = try await rig(["a", "b"])
        await rig.library.set(watched: rig.pending)
        await rig.library.set(keep: ["b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a", "b"])
        let records = await rig.library.recordsAtDelete
        XCTAssertEqual(records, [.deleting(["a", "b"])])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.removed, ["a"])
        let saved = try await rig.pending.load()
        XCTAssertNil(saved)
    }

    func testPhotosDeletedBeforeAKillAreReportedOnRelaunch() async throws {
        let rig = try await rig(["a", "b", "c"])
        try await rig.pending.save(.deleting(["a", "b"]))
        await rig.library.drop("a")
        let outcome = try await rig.runner().refresh()
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"])])
        guard case let .listed(list) = outcome else { return XCTFail("not listed: \(outcome)") }
        XCTAssertEqual(list.ids, ["b", "c"])
        let saved = try await rig.pending.load()
        XCTAssertNil(saved)
    }

    func testAnIntentWhosePhotosAreAllStillThereReportsNothingAndIsCleared() async throws {
        let rig = try await rig(["a"])
        try await rig.pending.save(.deleting(["a"]))
        let outcome = try await rig.runner().refresh()
        let reports = await rig.api.deletedCalls
        XCTAssertTrue(reports.isEmpty)
        guard case .listed = outcome else { return XCTFail("not listed: \(outcome)") }
        let saved = try await rig.pending.load()
        XCTAssertNil(saved)
    }

    func testAnIntentIsKeptWhenTheLibraryCannotBeRead() async throws {
        let rig = try await rig(["a"])
        try await rig.pending.save(.deleting(["a"]))
        await rig.library.drop("a")
        await rig.library.set(resourcesError: CocoaError(.fileReadNoPermission))
        do {
            _ = try await rig.runner().refresh()
            XCTFail("expected the library error")
        } catch {}
        let reports = await rig.api.deletedCalls
        let asked = await rig.api.eligibleCalls
        XCTAssertTrue(reports.isEmpty)
        XCTAssertTrue(asked.isEmpty)
        let saved = try await rig.pending.load()
        XCTAssertEqual(saved, .deleting(["a"]))
    }

    func testTheReportReplacesTheIntentAfterTheDeletionAndIsSentOnRelaunch() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(deletedErrors: [APIError.transport("-1001")])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case .reportPending = outcome else { return XCTFail("not pending: \(outcome)") }
        let saved = try await rig.pending.load()
        XCTAssertEqual(saved, .deleted(DeletedRequest(assetIDs: ["a"])))
        let relaunched = try await rig.runner().refresh()
        XCTAssertEqual(relaunched, .idle)
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"]), DeletedRequest(assetIDs: ["a"])])
    }

    func testWhenEveryShownPhotoVanishedTheRoundFinishesAsChanged() async throws {
        let rig = try await rig(["a", "b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        await rig.library.drop("a")
        await rig.library.drop("b")
        let outcome = try await runner.remove(shown: ["a", "b"])
        let confirms = await rig.api.confirmCalls
        let deletes = await rig.library.deleteCalls
        XCTAssertTrue(confirms.isEmpty)
        XCTAssertTrue(deletes.isEmpty)
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.changed, ["a", "b"])
        let state = await runner.state
        XCTAssertEqual(state.phase, .finished)
    }

    func testAShownPhotoThatVanishedIsReportedChangedWhileTheRestProceed() async throws {
        let rig = try await rig(["a", "b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        await rig.library.drop("b")
        let outcome = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        XCTAssertEqual(confirmed, [["a"]])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.changed, ["b"])
        XCTAssertEqual(report.removed, ["a"])
    }

    func testADeliveredReportWhoseClearFailsSaysSoAndIsNotSentAgain() async throws {
        let pending = UnclearablePendingReport()
        let rig = try await rig(["a", "b"], pending: pending)
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case let .recordNotCleared(report) = outcome else { return XCTFail("not surfaced: \(outcome)") }
        XCTAssertEqual(report.recorded, ["a"])
        let again = try await runner.refresh()
        XCTAssertEqual(again, .recordNotCleared(report))
        let asked = await rig.api.eligibleCalls.count
        XCTAssertEqual(asked, 1)
        await pending.set(clearsFail: false)
        let cleared = try await runner.refresh()
        guard case let .listed(list) = cleared else { return XCTFail("not listed: \(cleared)") }
        XCTAssertEqual(list.ids, ["b"])
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"])])
        let attempts = await pending.clearAttempts
        XCTAssertEqual(attempts, 3)
        let saved = await pending.load()
        XCTAssertNil(saved)
    }

    func testAnEarlierReportWhoseClearFailsBlocksANewListUntilItIsCleared() async throws {
        let pending = UnclearablePendingReport()
        let rig = try await rig(["b"], pending: pending)
        try await pending.save(.deleted(DeletedRequest(assetIDs: ["a"])))
        let runner = rig.runner()
        let first = try await runner.refresh()
        guard case let .recordNotCleared(report) = first else { return XCTFail("not surfaced: \(first)") }
        XCTAssertEqual(report.recorded, ["a"])
        let retried = try await runner.retryReport()
        XCTAssertEqual(retried, .recordNotCleared(report))
        let asked = await rig.api.eligibleCalls.count
        XCTAssertEqual(asked, 0)
        await pending.set(clearsFail: false)
        let cleared = try await runner.refresh()
        guard case .listed = cleared else { return XCTFail("not listed: \(cleared)") }
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"])])
    }

    func testACancelledDeletionWhoseIntentCannotBeClearedSaysSoAndReportsNothing() async throws {
        let pending = UnclearablePendingReport()
        let rig = try await rig(["a"], pending: pending)
        await rig.library.set(keep: ["a"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case let .recordNotCleared(report) = outcome else { return XCTFail("not surfaced: \(outcome)") }
        XCTAssertEqual(report.notRemoved, ["a"])
        await pending.set(clearsFail: false)
        let next = try await runner.refresh()
        guard case .listed = next else { return XCTFail("not listed: \(next)") }
        let reports = await rig.api.deletedCalls
        XCTAssertTrue(reports.isEmpty)
    }

    func testAnUnreadablePendingReportBlocksCleanupUntilItCanBeRead() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("pending-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let file = FilePendingDeletionReport(url: url)
        let rig = try await rig(["b"], pending: file)
        let absent = try await rig.runner().refresh()
        guard case .listed = absent else { return XCTFail("not listed: \(absent)") }
        let damaged = Data("{\"stage\":".utf8)
        try damaged.write(to: url)
        let runner = rig.runner()
        let blocked = try await runner.refresh()
        XCTAssertEqual(blocked, .reportUnreadable)
        let retried = try await runner.retryReport()
        XCTAssertEqual(retried, .reportUnreadable)
        let asked = await rig.api.eligibleCalls.count
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(asked, 1)
        XCTAssertTrue(reports.isEmpty)
        XCTAssertEqual(try Data(contentsOf: url), damaged)
        try await file.save(.deleted(DeletedRequest(assetIDs: ["a"])))
        let resolved = try await runner.refresh()
        guard case .listed = resolved else { return XCTFail("not listed: \(resolved)") }
        let sent = await rig.api.deletedCalls
        XCTAssertEqual(sent, [DeletedRequest(assetIDs: ["a"])])
    }

    func testAPendingReportThatCannotBeReadThrowsButAMissingOneIsNone() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("pending-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        do {
            _ = try await FilePendingDeletionReport(url: directory).load()
            XCTFail("expected a read error")
        } catch {}
        let missing = try await FilePendingDeletionReport(url: directory.appendingPathComponent("none.json")).load()
        XCTAssertNil(missing)
        let elsewhere = try await FilePendingDeletionReport(url: directory.appendingPathComponent("no/none.json")).load()
        XCTAssertNil(elsewhere)
    }

    func testARetryWhilePhotoKitDeletesLeavesTheRoundsIntentInPlace() async throws {
        let rig = try await rig(["a"])
        await rig.library.set(keep: ["a"])
        await rig.library.set(watched: rig.pending)
        let runner = rig.runner()
        let seen = Outcomes()
        await rig.library.set(onDelete: {
            do {
                _ = try await runner.retryReport()
                await seen.add(nil)
            } catch {
                await seen.add(error as? CleanupFlowError)
            }
        })
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        let errors = await seen.errors
        XCTAssertEqual(errors, [.notNow(.deleting)])
        let records = await rig.library.recordsAtDelete
        XCTAssertEqual(records, [.deleting(["a"])])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.notRemoved, ["a"])
        let reports = await rig.api.deletedCalls
        XCTAssertTrue(reports.isEmpty)
    }

    func testARetryWhenNoRoundIsRunningStillSendsAnEarlierReport() async throws {
        let rig = try await rig(["b"])
        try await rig.pending.save(.deleted(DeletedRequest(assetIDs: ["a"])))
        let outcome = try await rig.runner().retryReport()
        XCTAssertEqual(outcome, .idle)
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"])])
    }
}
