import XCTest
@testable import DarkroomCore

private actor ScriptedRunner: UploadRunner {
    private var script: [Result<UploadCoordinator.Result, Error>]
    private var waiting: CheckedContinuation<Void, Never>?
    private let holdsFirstRun: Bool
    private(set) var runs = 0

    init(_ script: [Result<UploadCoordinator.Result, Error>], holdsFirstRun: Bool = false) {
        self.script = script
        self.holdsFirstRun = holdsFirstRun
    }

    var isHeld: Bool { waiting != nil }

    func release() {
        waiting?.resume()
        waiting = nil
    }

    func run() async throws -> UploadCoordinator.Result {
        runs += 1
        if holdsFirstRun, runs == 1 {
            await withCheckedContinuation { waiting = $0 }
        }
        return try script.removeFirst().get()
    }
}

private struct UnreadableStore: ResourceStore {
    func upsert(_ resources: [LocalResource]) async throws { throw ResourceStoreError.database("x") }
    func record(for key: ResourceKey) async throws -> ResourceRecord? { throw ResourceStoreError.database("x") }
    func records(in states: Set<ResourceState>) async throws -> [ResourceRecord] { throw ResourceStoreError.database("x") }
    func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        throw ResourceStoreError.database("x")
    }
    func remove(_ keys: [ResourceKey]) async throws { throw ResourceStoreError.database("x") }
    func recordAttempt(for keys: [ResourceKey]) async throws { throw ResourceStoreError.database("x") }
    func recordRehash(for key: ResourceKey) async throws { throw ResourceStoreError.database("x") }
    func counts() async throws -> [ResourceState: Int] { throw ResourceStoreError.database("x") }
}

private struct OtherFailure: Error {}

final class UploadStatusTests: XCTestCase {
    private let moment = Date(timeIntervalSince1970: 2_000_000)

    private func result(_ outcome: UploadCoordinator.Outcome) -> UploadCoordinator.Result {
        var made = UploadCoordinator.Result()
        made.outcome = outcome
        return made
    }

    private func resource(_ id: String, _ state: ResourceState, kind: ResourceKind = .photo) -> LocalResource {
        LocalResource(key: ResourceKey(assetID: id, kind: kind), sha1: "sha-\(id)", bytes: 10, filename: "\(id).heic", state: state)
    }

    private func makeTracker(
        _ script: [Result<UploadCoordinator.Result, Error>],
        store: ResourceStore = InMemoryResourceStore()
    ) -> UploadStatusTracker {
        UploadStatusTracker(runner: ScriptedRunner(script), store: store, clock: { [moment] in moment })
    }

    private func waitUntil(_ condition: () async -> Bool, file: StaticString = #filePath, line: UInt = #line) async {
        for _ in 0..<10_000 {
            if await condition() { return }
            await Task.yield()
        }
        XCTFail("timed out waiting", file: file, line: line)
    }

    func testBeforeAnySyncItIsIdleWithNothingCounted() async {
        let status = await makeTracker([]).status()
        XCTAssertEqual(status, UploadStatus(phase: .idle, counts: [:], failures: [], lastSync: nil))
    }

    func testTheCountsAreTheStoresCountsPerState() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([
            resource("a", .hashed), resource("b", .hashed), resource("c", .checked),
            resource("d", .uploaded), resource("e", .notLocal),
        ])
        let status = await makeTracker([], store: store).status()
        XCTAssertEqual(status.count(.hashed), 2)
        XCTAssertEqual(status.count(.checked), 1)
        XCTAssertEqual(status.count(.uploaded), 1)
        XCTAssertEqual(status.count(.notLocal), 1)
        XCTAssertEqual(status.count(.failed), 0)
    }

    func testFailedResourcesAreListedWithTheirReasons() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a", .hashed), resource("b", .hashed)])
        try await store.setState(.failed, for: [ResourceKey(assetID: "a", kind: .photo)], failureReason: "the server refused it (413)")
        try await store.setState(.failed, for: [ResourceKey(assetID: "b", kind: .photo)], failureReason: nil)
        let status = await makeTracker([], store: store).status()
        XCTAssertEqual(status.failures, [
            FailedResource(key: ResourceKey(assetID: "a", kind: .photo), filename: "a.heic", reason: "the server refused it (413)"),
            FailedResource(key: ResourceKey(assetID: "b", kind: .photo), filename: "b.heic", reason: UploadStatusTracker.noReasonRecorded),
        ])
    }

    func testAFinishedSyncIsIdleAndRecordsWhenItFinished() async {
        let status = await makeTracker([.success(result(.finished))]).syncNow()
        XCTAssertEqual(status.phase, .idle)
        XCTAssertEqual(status.lastSync, moment)
    }

    func testWaitingForTheNetworkIsNotAnErrorAndIsNotASync() async {
        let status = await makeTracker([.success(result(.waitingForNetwork))]).syncNow()
        XCTAssertEqual(status.phase, .waitingForHomeNetwork)
        XCTAssertNil(status.lastSync)
    }

    func testAWaitingStatusClearsWhenALaterSyncFinishes() async {
        let tracker = makeTracker([.success(result(.waitingForNetwork)), .success(result(.finished))])
        _ = await tracker.syncNow()
        let status = await tracker.syncNow()
        XCTAssertEqual(status.phase, .idle)
        XCTAssertEqual(status.lastSync, moment)
    }

    func testAnEarlierSyncTimeSurvivesALaterWait() async {
        let tracker = makeTracker([.success(result(.finished)), .success(result(.waitingForNetwork))])
        _ = await tracker.syncNow()
        let status = await tracker.syncNow()
        XCTAssertEqual(status.phase, .waitingForHomeNetwork)
        XCTAssertEqual(status.lastSync, moment)
    }

    func testNeedingSignInIsShownUntilTheSignInChanges() async {
        let tracker = makeTracker([.success(result(.needsSignIn))])
        let stopped = await tracker.syncNow()
        XCTAssertEqual(stopped.phase, .needsSignIn)
        XCTAssertNil(stopped.lastSync)
        await tracker.signInChanged()
        let after = await tracker.status()
        XCTAssertEqual(after.phase, .idle)
    }

    func testSignInChangingDoesNotHideAnError() async {
        let tracker = makeTracker([.failure(OtherFailure())])
        _ = await tracker.syncNow()
        await tracker.signInChanged()
        let status = await tracker.status()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.syncStopped))
    }

    func testAStoreFailureDuringASyncIsAnErrorThatNamesTheQueue() async {
        let status = await makeTracker([.failure(ResourceStoreError.database("disk"))]).syncNow()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.queueUnreadable))
        XCTAssertNil(status.lastSync)
    }

    func testAnyOtherFailureIsAnErrorThatLeaksNothingAboutIt() async {
        let status = await makeTracker([.failure(OtherFailure())]).syncNow()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.syncStopped))
    }

    func testACancelledSyncLeavesNoErrorBehind() async {
        let status = await makeTracker([.failure(CancellationError())]).syncNow()
        XCTAssertEqual(status.phase, .idle)
        XCTAssertNil(status.lastSync)
    }

    func testAnErrorClearsWhenTheNextSyncFinishes() async {
        let tracker = makeTracker([.failure(OtherFailure()), .success(result(.finished))])
        _ = await tracker.syncNow()
        let status = await tracker.syncNow()
        XCTAssertEqual(status.phase, .idle)
    }

    func testAnUnreadableQueueIsAnErrorNotZeroCounts() async {
        let status = await makeTracker([.success(result(.finished))], store: UnreadableStore()).syncNow()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.queueUnreadable))
        XCTAssertEqual(status.counts, [:])
        XCTAssertEqual(status.lastSync, moment)
    }

    func testWhileASyncRunsItIsUploadingAndASecondTapStartsNothing() async {
        let runner = ScriptedRunner([.success(result(.finished))], holdsFirstRun: true)
        let tracker = UploadStatusTracker(runner: runner, store: InMemoryResourceStore(), clock: { [moment] in moment })
        let first = Task { await tracker.syncNow() }
        await waitUntil { await runner.isHeld }
        let during = await tracker.status()
        XCTAssertEqual(during.phase, .uploading)
        let second = await tracker.syncNow()
        XCTAssertEqual(second.phase, .uploading)
        await runner.release()
        let done = await first.value
        let runs = await runner.runs
        XCTAssertEqual(done.phase, .idle)
        XCTAssertEqual(runs, 1)
    }
}
