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

    func eligible(_ request: EligibleRequest) async throws -> EligibleReply {
        eligibleCalls.append(request)
        if let eligibleError { throw eligibleError }
        let offered = enabled ? request.assets.filter { eligibleIDs.contains($0.id) } : []
        return EligibleReply(cleanupEnabled: enabled, eligible: offered.map { EligibleAsset(assetID: $0.id, bytes: 10) })
    }

    func confirm(_ request: ConfirmRequest) async throws -> ConfirmReply {
        confirmCalls.append(request)
        if let confirmError { throw confirmError }
        let ids = request.assets.map(\.id)
        return ConfirmReply(
            confirmed: ids.filter { refuse[$0] == nil },
            rejected: ids.compactMap { id in refuse[id].map { RejectedAsset(assetID: id, reason: $0, message: "Refused.") } }
        )
    }

    func deleted(_ request: DeletedRequest) async throws -> DeletedReply {
        deletedCalls.append(request)
        if !deletedErrors.isEmpty { throw deletedErrors.removeFirst() }
        return DeletedReply(recorded: request.assetIDs, rejected: [])
    }
}

private actor FakeLibrary: PhotoDeleter {
    var assets: Set<String>
    var keep: Set<String> = []
    private(set) var deleteCalls: [[String]] = []

    init(_ assets: Set<String>) {
        self.assets = assets
    }

    func set(keep: Set<String>) { self.keep = keep }
    func drop(_ id: String) { assets.remove(id) }

    func present(_ assetIDs: [String]) -> Set<String> {
        Set(assetIDs).intersection(assets)
    }

    func delete(_ assetIDs: [String]) -> [String] {
        deleteCalls.append(assetIDs)
        let removed = assetIDs.filter { assets.contains($0) && !keep.contains($0) }
        assets.subtract(removed)
        return removed
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
        let library: FakeLibrary
        let pending = InMemoryPendingDeletionReport()
        let clock = Clock()

        func runner() -> CleanupRunner {
            let clock = clock
            return CleanupRunner(api: api, store: store, deleter: library, pending: pending, clock: { clock.read() })
        }
    }

    private func resource(_ id: String, _ kind: ResourceKind = .photo, state: ResourceState = .uploaded) -> LocalResource {
        LocalResource(key: ResourceKey(assetID: id, kind: kind), sha1: "sha-\(id)-\(kind.rawValue)", bytes: 10, filename: "\(id).HEIC", state: state)
    }

    private func rig(_ ids: [String], eligible: Set<String>? = nil) async throws -> Rig {
        let rig = Rig(library: FakeLibrary(Set(ids)))
        try await rig.store.upsert(ids.map { resource($0) })
        await rig.api.set(eligible: eligible ?? Set(ids))
        return rig
    }

    func testTheListAsksOnlyForPresentAssetsWhoseEveryResourceIsUploaded() async throws {
        let rig = try await rig(["a", "b"])
        try await rig.store.upsert([resource("c"), resource("c", .liveVideo, state: .checked), resource("d", .liveVideo), resource("gone")])
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
        try await rig.store.upsert([resource("b", .adjusted, state: .hashed)])
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

    func testAReportLeftFromAnEarlierLaunchIsSentBeforeANewList() async throws {
        let rig = try await rig(["b"])
        await rig.pending.save(DeletedRequest(assetIDs: ["a"]))
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
