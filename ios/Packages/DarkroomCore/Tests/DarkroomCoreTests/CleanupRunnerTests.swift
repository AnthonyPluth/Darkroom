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
    private(set) var deleteCalls: [[String]] = []

    func set(keep: Set<String>) { self.keep = keep }
    func drop(_ id: String) { assets[id] = nil }

    func add(_ resource: PhotoResource) {
        assets[resource.assetID, default: []].removeAll { $0.kind == resource.kind }
        assets[resource.assetID, default: []].append(resource)
    }

    func resources(of assetIDs: [String]) -> [PhotoResource] {
        assetIDs.flatMap { assets[$0] ?? [] }
    }

    func delete(_ assetIDs: [String]) -> [String] {
        deleteCalls.append(assetIDs)
        let removed = assetIDs.filter { assets[$0] != nil && !keep.contains($0) }
        removed.forEach { assets[$0] = nil }
        return removed
    }
}

private actor FailingPendingReport: PendingDeletionReport {
    func load() -> DeletedRequest? { nil }

    func save(_ request: DeletedRequest?) throws {
        throw CocoaError(.fileWriteOutOfSpace)
    }
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
        let versions = InMemoryIngestVersions()
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

    func testTheListAsksOnlyForPresentAssetsWhoseEveryResourceIsUploaded() async throws {
        let rig = try await rig(["a", "b"])
        try await rig.both([resource("c"), resource("c", .liveVideo, state: .checked), resource("d", .liveVideo)])
        try await rig.ingested([resource("gone")])
        await rig.api.set(eligible: ["a", "b", "c", "d", "gone"])
        let outcome = try await rig.runner().refresh()
        let asked = await rig.api.eligibleCalls.flatMap { $0.assets.map(\.id) }
        XCTAssertEqual(asked, ["a", "b"])
        guard case let .listed(list) = outcome else { return XCTFail("not listed: \(outcome)") }
        XCTAssertEqual(list.ids, ["a", "b"])
    }

    func testALargeLibraryIsAskedInBatchesOfFiveHundred() async throws {
        let ids = (0..<501).map { "id\($0)" }
        let rig = try await rig(ids, eligible: ["id0", "id500"])
        let outcome = try await rig.runner().refresh()
        let sizes = await rig.api.eligibleCalls.map(\.assets.count)
        XCTAssertEqual(sizes, [500, 1])
        guard case let .listed(list) = outcome else { return XCTFail("not listed") }
        XCTAssertEqual(list.ids, ["id0", "id500"])
    }

    func testCleanupOffOnTheServerListsNothing() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(eligible: ["a"], enabled: false)
        let runner = rig.runner()
        let outcome = try await runner.refresh()
        XCTAssertEqual(outcome, .idle)
        let enabled = await runner.cleanupEnabled
        XCTAssertEqual(enabled, false)
        do {
            _ = try await runner.remove(shown: ["a"])
            XCTFail("expected notNow")
        } catch {
            XCTAssertEqual(error as? CleanupFlowError, .notNow(.idle))
        }
        let confirms = await rig.api.confirmCalls
        XCTAssertTrue(confirms.isEmpty)
    }

    func testARoundConfirmsTheShownDeletesTheConfirmedAndReportsTheRemoved() async throws {
        let rig = try await rig(["a", "b", "c"])
        await rig.api.set(refuse: ["b": "file_missing"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        let deletes = await rig.library.deleteCalls
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(confirmed, [["a", "b"]])
        XCTAssertEqual(deletes, [["a"]])
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"])])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.removed, ["a"])
        XCTAssertEqual(report.recorded, ["a"])
        XCTAssertEqual(report.refused.map(\.assetID), ["b"])
        let saved = await rig.pending.load()
        XCTAssertNil(saved)
    }

    func testALivePhotoWhoseVideoHasNoStoreRowIsNeverAskedAbout() async throws {
        let rig = try await rig(["a", "b"])
        await rig.onPhone("b", .liveVideo)
        let runner = rig.runner()
        let outcome = try await runner.refresh()
        let asked = await rig.api.eligibleCalls.flatMap { $0.assets.map(\.id) }
        XCTAssertEqual(asked, ["a"])
        guard case let .listed(list) = outcome else { return XCTFail("not listed: \(outcome)") }
        XCTAssertEqual(list.ids, ["a"])
        _ = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        let deletes = await rig.library.deleteCalls
        XCTAssertEqual(confirmed, [["a"]])
        XCTAssertEqual(deletes, [["a"]])
    }

    func testAnAssetWithNoStoreRowAtAllIsNeverAskedAbout() async throws {
        let rig = try await rig(["a"])
        await rig.onPhone("x")
        await rig.onPhone("x", .adjusted)
        _ = try await rig.runner().refresh()
        let asked = await rig.api.eligibleCalls.flatMap { $0.assets.map(\.id) }
        XCTAssertEqual(asked, ["a"])
    }

    func testAResourceChangedSinceItsIngestIsNeverAskedAbout() async throws {
        let rig = try await rig(["a", "b"])
        await rig.onPhone("b", version: "v2")
        _ = try await rig.runner().refresh()
        let asked = await rig.api.eligibleCalls.flatMap { $0.assets.map(\.id) }
        XCTAssertEqual(asked, ["a"])
    }

    func testOnlyTheResourcesStillOnThePhoneAreDescribed() async throws {
        let rig = try await rig(["a"])
        try await rig.ingested([resource("a", .adjusted)])
        _ = try await rig.runner().refresh()
        let kinds = await rig.api.eligibleCalls.flatMap { $0.assets.flatMap { $0.resources.map(\.kind) } }
        XCTAssertEqual(kinds, [.photo])
    }

    func testAnEditOnThePhoneSinceTheListIsNotConfirmedOrDeleted() async throws {
        let rig = try await rig(["a", "b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        await rig.onPhone("b", .adjusted)
        let outcome = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        let deletes = await rig.library.deleteCalls
        XCTAssertEqual(confirmed, [["a"]])
        XCTAssertEqual(deletes, [["a"]])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.changed, ["b"])
    }

    func testALiveVideoOnThePhoneSinceTheListIsNotConfirmedOrDeleted() async throws {
        let rig = try await rig(["a"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        await rig.onPhone("a", .liveVideo)
        let outcome = try await runner.remove(shown: ["a"])
        let confirms = await rig.api.confirmCalls
        let deletes = await rig.library.deleteCalls
        XCTAssertTrue(confirms.isEmpty)
        XCTAssertTrue(deletes.isEmpty)
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.changed, ["a"])
    }

    func testAnEditDuringTheConfirmCallIsNotDeleted() async throws {
        let rig = try await rig(["a", "b"])
        let library = rig.library
        await rig.api.set(onConfirm: {
            await library.add(PhotoResource(assetID: "b", kind: .adjusted, originalFilename: "b.HEIC", version: "v1"))
        })
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        let deletes = await rig.library.deleteCalls
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(confirmed, [["a", "b"]])
        XCTAssertEqual(deletes, [["a"]])
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"])])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.changed, ["b"])
        XCTAssertEqual(report.removed, ["a"])
        XCTAssertEqual(report.notRemoved, [])
    }

    func testAVersionChangeDuringTheConfirmCallDeletesNothing() async throws {
        let rig = try await rig(["a"])
        let library = rig.library
        await rig.api.set(onConfirm: {
            await library.add(PhotoResource(assetID: "a", kind: .photo, originalFilename: "a.HEIC", version: "v2"))
        })
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        let deletes = await rig.library.deleteCalls
        let reports = await rig.api.deletedCalls
        XCTAssertTrue(deletes.isEmpty)
        XCTAssertTrue(reports.isEmpty)
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.changed, ["a"])
    }

    func testAPhotoGoneFromThePhoneSinceTheListIsNotConfirmed() async throws {
        let rig = try await rig(["a", "b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        await rig.library.drop("b")
        _ = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        XCTAssertEqual(confirmed, [["a"]])
    }

    func testAPhotoEditedSinceTheListIsNotConfirmed() async throws {
        let rig = try await rig(["a", "b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        try await rig.both([resource("b", .adjusted, state: .hashed)])
        let outcome = try await runner.remove(shown: ["a", "b"])
        let confirmed = await rig.api.confirmCalls.map { $0.assets.map(\.id) }
        XCTAssertEqual(confirmed, [["a"]])
        guard case let .finished(report) = outcome else { return XCTFail("not finished") }
        XCTAssertEqual(report.changed, ["b"])
    }

    func testAStaleListIsFetchedAgainAndNothingIsConfirmed() async throws {
        let rig = try await rig(["a"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        rig.clock.advance(EligibleList.maxAge)
        let outcome = try await runner.remove(shown: ["a"])
        XCTAssertEqual(outcome, .refetched)
        let eligibleCalls = await rig.api.eligibleCalls.count
        let confirms = await rig.api.confirmCalls.count
        let deletes = await rig.library.deleteCalls.count
        XCTAssertEqual(eligibleCalls, 2)
        XCTAssertEqual(confirms, 0)
        XCTAssertEqual(deletes, 0)
        let state = await runner.state
        guard case .fetched = state else { return XCTFail("not fetched again: \(state)") }
    }

    func testARefetchThatFailsSaysWhyInsteadOfRefetched() async throws {
        let rig = try await rig(["a"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        rig.clock.advance(EligibleList.maxAge)
        await rig.api.set(eligibleError: APIError.unauthorized)
        let signedOut = try await runner.remove(shown: ["a"])
        XCTAssertEqual(signedOut, .needsSignIn)
        await rig.api.set(eligibleError: nil)
        await rig.api.set(eligible: ["a"], enabled: false)
        _ = try await runner.refresh()
        await rig.api.set(eligible: ["a"])
        _ = try await runner.refresh()
        rig.clock.advance(EligibleList.maxAge)
        await rig.api.set(eligible: [])
        let nothingLeft = try await runner.remove(shown: ["a"])
        XCTAssertEqual(nothingLeft, .idle)
        let confirms = await rig.api.confirmCalls.count
        XCTAssertEqual(confirms, 0)
    }

    func testCancellingTheSystemDialogReportsNothing() async throws {
        let rig = try await rig(["a", "b"])
        await rig.library.set(keep: ["a", "b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a", "b"])
        let reports = await rig.api.deletedCalls
        let saved = await rig.pending.load()
        XCTAssertTrue(reports.isEmpty)
        XCTAssertNil(saved)
        guard case let .finished(report) = outcome else { return XCTFail("not finished") }
        XCTAssertEqual(report.notRemoved, ["a", "b"])
    }

    func testAPartialDeletionReportsOnlyWhatWasRemoved() async throws {
        let rig = try await rig(["a", "b", "c"])
        await rig.library.set(keep: ["b"])
        let runner = rig.runner()
        _ = try await runner.refresh()
        _ = try await runner.remove(shown: ["a", "b", "c"])
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a", "c"])])
    }

    func testAFailedConfirmDeletesNothing() async throws {
        for (error, expected) in [(APIError.server(status: 503), CleanupRunner.Outcome.confirmFailed), (.unauthorized, .needsSignIn)] {
            let rig = try await rig(["a"])
            await rig.api.set(confirmError: error)
            let runner = rig.runner()
            _ = try await runner.refresh()
            let outcome = try await runner.remove(shown: ["a"])
            XCTAssertEqual(outcome, expected)
            let deletes = await rig.library.deleteCalls
            XCTAssertTrue(deletes.isEmpty)
            let state = await runner.state
            guard case .fetched = state else { return XCTFail("not back to the list: \(state)") }
        }
    }

    func testAFailedListLeavesNothingToConfirm() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(eligibleError: APIError.unauthorized)
        let runner = rig.runner()
        let outcome = try await runner.refresh()
        XCTAssertEqual(outcome, .needsSignIn)
        await rig.api.set(eligibleError: APIError.transport("-1009"))
        do {
            _ = try await runner.refresh()
            XCTFail("expected the transport error")
        } catch {
            XCTAssertEqual(error as? APIError, .transport("-1009"))
        }
        let state = await runner.state
        XCTAssertEqual(state, .idle)
    }

    func testAFailedReportIsKeptAndRetriedWithTheSameIds() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(deletedErrors: [APIError.transport("-1001")])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case let .reportPending(report) = outcome else { return XCTFail("not pending: \(outcome)") }
        XCTAssertEqual(report.removed, ["a"])
        let saved = await rig.pending.load()
        XCTAssertEqual(saved, DeletedRequest(assetIDs: ["a"]))
        let retried = try await runner.retryReport()
        guard case let .finished(finished) = retried else { return XCTFail("not finished: \(retried)") }
        XCTAssertEqual(finished.recorded, ["a"])
        let reports = await rig.api.deletedCalls
        XCTAssertEqual(reports, [DeletedRequest(assetIDs: ["a"]), DeletedRequest(assetIDs: ["a"])])
        let cleared = await rig.pending.load()
        XCTAssertNil(cleared)
    }

    func testAFilePendingReportSurvivesARelaunchAndIsClearedWhenDone() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("pending-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let first = FilePendingDeletionReport(url: url)
        let none = await first.load()
        XCTAssertNil(none)
        try await first.save(DeletedRequest(assetIDs: ["a", "b"]))
        let reopened = await FilePendingDeletionReport(url: url).load()
        XCTAssertEqual(reopened?.assetIDs, ["a", "b"])
        try await first.save(nil)
        let cleared = await FilePendingDeletionReport(url: url).load()
        XCTAssertNil(cleared)
        try await first.save(nil)
    }

    func testAFilePendingReportThatCannotBeWrittenThrows() async throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("missing-\(UUID().uuidString)")
            .appendingPathComponent("pending.json")
        do {
            try await FilePendingDeletionReport(url: url).save(DeletedRequest(assetIDs: ["a"]))
            XCTFail("expected a write error")
        } catch {}
    }

    func testAReportThatCouldNotBeSavedSaysSoAndIsStillRetried() async throws {
        let rig = try await rig(["a"], pending: FailingPendingReport())
        await rig.api.set(deletedErrors: [APIError.transport("-1001")])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case let .reportUnsaved(report) = outcome else { return XCTFail("not unsaved: \(outcome)") }
        XCTAssertEqual(report.removed, ["a"])
        let retried = try await runner.retryReport()
        guard case let .finished(finished) = retried else { return XCTFail("not finished: \(retried)") }
        XCTAssertEqual(finished.recorded, ["a"])
    }

    func testAReportTheServerRefusesForGoodIsClearedAndShown() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(deletedErrors: [APIError.rejected(status: 409)])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case let .finished(report) = outcome else { return XCTFail("not finished: \(outcome)") }
        XCTAssertEqual(report.unrecorded.map(\.assetID), ["a"])
        XCTAssertEqual(report.unrecorded.map(\.reason), [CleanupRunner.reportRefusedReason])
        let saved = await rig.pending.load()
        XCTAssertNil(saved)
    }

    func testAnEarlierReportTheServerRefusesForGoodDoesNotBlockCleanup() async throws {
        let rig = try await rig(["b"])
        try await rig.pending.save(DeletedRequest(assetIDs: ["a"]))
        await rig.api.set(deletedErrors: [APIError.rejected(status: 400)])
        let first = try await rig.runner().refresh()
        guard case let .finished(report) = first else { return XCTFail("not finished: \(first)") }
        XCTAssertEqual(report.unrecorded.map(\.assetID), ["a"])
        let saved = await rig.pending.load()
        XCTAssertNil(saved)
        let second = try await rig.runner().refresh()
        guard case .listed = second else { return XCTFail("not listed: \(second)") }
    }

    func testARateLimitedReportIsKeptForLater() async throws {
        let rig = try await rig(["a"])
        await rig.api.set(deletedErrors: [APIError.rejected(status: 429)])
        let runner = rig.runner()
        _ = try await runner.refresh()
        let outcome = try await runner.remove(shown: ["a"])
        guard case .reportPending = outcome else { return XCTFail("not pending: \(outcome)") }
        let saved = await rig.pending.load()
        XCTAssertEqual(saved, DeletedRequest(assetIDs: ["a"]))
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
        try await rig.pending.save(DeletedRequest(assetIDs: ["a"]))
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
        let cleared = await rig.pending.load()
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
}
