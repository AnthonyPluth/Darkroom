import XCTest
@testable import DarkroomCore

private actor ScriptedRunner: UploadRunner {
    private var script: [Result<UploadCoordinator.Result, Error>]
    private var waiting: CheckedContinuation<Void, Never>?
    private let holdsFirstRun: Bool
    private(set) var runs = 0
    private(set) var ignoringBackoff: [Bool] = []
    private let log: EventLog?

    init(_ script: [Result<UploadCoordinator.Result, Error>], holdsFirstRun: Bool = false, log: EventLog? = nil) {
        self.script = script
        self.holdsFirstRun = holdsFirstRun
        self.log = log
    }

    var isHeld: Bool { waiting != nil }

    func release() {
        waiting?.resume()
        waiting = nil
    }

    func run(ignoringBackoff: Bool) async throws -> UploadCoordinator.Result {
        runs += 1
        self.ignoringBackoff.append(ignoringBackoff)
        await log?.add("upload")
        if holdsFirstRun, runs == 1 {
            await withCheckedContinuation { waiting = $0 }
        }
        return try script.removeFirst().get()
    }
}

private actor EventLog {
    private(set) var events: [String] = []
    func add(_ event: String) { events.append(event) }
}

private actor ScriptedScanner: LibraryScanner {
    private var script: [Result<IngestSummary, Error>]
    private let log: EventLog?
    private(set) var runs = 0

    init(_ script: [Result<IngestSummary, Error>], log: EventLog? = nil) {
        self.script = script
        self.log = log
    }

    func run() async throws -> IngestSummary {
        runs += 1
        await log?.add("scan")
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

    private func result(_ outcome: UploadCoordinator.Outcome, checkedIn: Bool = true) -> UploadCoordinator.Result {
        var made = UploadCoordinator.Result()
        made.outcome = outcome
        made.checkinAccepted = checkedIn
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

    func testFailedResourcesAreListedWithTheirReasons() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a", .hashed), resource("b", .hashed)])
        try await store.setState(.failed, for: [ResourceKey(assetID: "a", kind: .photo)], failureReason: "the server refused it (413)")
        try await store.setState(.failed, for: [ResourceKey(assetID: "b", kind: .photo)], failureReason: nil)
        let status = await makeTracker([], store: store).status()
        XCTAssertEqual(status.count(.failed), 2)
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

    func testNeedingSignInIsShownUntilTheSignInChanges() async {
        let tracker = makeTracker([.success(result(.needsSignIn))])
        let stopped = await tracker.syncNow()
        XCTAssertEqual(stopped.phase, .needsSignIn)
        XCTAssertNil(stopped.lastSync)
        await tracker.signInChanged()
        let after = await tracker.status()
        XCTAssertEqual(after.phase, .idle)
    }

    func testAStoreFailureDuringASyncIsAnErrorThatNamesTheQueue() async {
        let status = await makeTracker([.failure(ResourceStoreError.database("disk"))]).syncNow()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.queueUnreadable))
        XCTAssertNil(status.lastSync)
    }

    func testACancelledSyncKeepsWhatWasShownBefore() async {
        let fresh = await makeTracker([.failure(CancellationError())]).syncNow()
        XCTAssertEqual(fresh.phase, .idle)
        XCTAssertNil(fresh.lastSync)
        let earlier: [Result<UploadCoordinator.Result, Error>] = [
            .success(result(.needsSignIn)), .success(result(.waitingForNetwork)), .failure(OtherFailure()),
        ]
        for first in earlier {
            let tracker = makeTracker([first, .failure(CancellationError())])
            let before = await tracker.syncNow()
            let after = await tracker.syncNow()
            XCTAssertNotEqual(before.phase, .idle)
            XCTAssertEqual(after, before)
        }
    }

    func testAnUnreadableQueueIsAnErrorNotZeroCounts() async {
        let status = await makeTracker([.success(result(.finished))], store: UnreadableStore()).syncNow()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.queueUnreadable))
        XCTAssertEqual(status.counts, [:])
        XCTAssertNil(status.lastSync)
    }

    func testAFailedRetryStopsTheSyncBeforeTheScanAndTheUpload() async {
        let scanner = ScriptedScanner([.success(IngestSummary())])
        let runner = ScriptedRunner([.success(result(.finished))])
        let tracker = UploadStatusTracker(runner: runner, store: UnreadableStore(), scanner: scanner, clock: { [moment] in moment })
        let status = await tracker.syncNow()
        let scans = await scanner.runs
        let runs = await runner.runs
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.queueUnreadable))
        XCTAssertNil(status.lastSync)
        XCTAssertEqual(scans, 0)
        XCTAssertEqual(runs, 0)
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

    private func scanning(
        _ scans: [Result<IngestSummary, Error>],
        _ uploads: [Result<UploadCoordinator.Result, Error>],
        store: ResourceStore = InMemoryResourceStore(),
        syncState: SyncStateStore = InMemorySyncStateStore(),
        log: EventLog = EventLog()
    ) -> (tracker: UploadStatusTracker, runner: ScriptedRunner, scanner: ScriptedScanner, log: EventLog) {
        let runner = ScriptedRunner(uploads, log: log)
        let scanner = ScriptedScanner(scans, log: log)
        let tracker = UploadStatusTracker(
            runner: runner, store: store, scanner: scanner, syncState: syncState, clock: { [moment] in moment }
        )
        return (tracker, runner, scanner, log)
    }

    func testASyncScansTheLibraryBeforeItUploads() async {
        let rig = scanning([.success(IngestSummary())], [.success(result(.finished))])
        _ = await rig.tracker.syncNow()
        let events = await rig.log.events
        XCTAssertEqual(events, ["scan", "upload"])
    }

    func testAManualSyncSkipsTheBackoffAndARefreshKeepsIt() async {
        let rig = scanning(
            [.success(IngestSummary()), .success(IngestSummary()), .success(IngestSummary())],
            [.success(result(.finished)), .success(result(.finished)), .success(result(.finished))]
        )
        _ = await rig.tracker.syncNow()
        _ = await rig.tracker.refresh()
        _ = await rig.tracker.retry([])
        let flags = await rig.runner.ignoringBackoff
        XCTAssertEqual(flags, [true, false, true])
    }

    func testTheFirstScanIsRecordedRightAfterTheScanEvenWhenTheUploadWaitsForTheNetwork() async {
        let state = InMemorySyncStateStore()
        let rig = scanning([.success(IngestSummary())], [.success(result(.waitingForNetwork))], syncState: state)
        let before = await rig.tracker.status()
        XCTAssertFalse(before.firstScanFinished)
        let after = await rig.tracker.syncNow()
        XCTAssertTrue(after.firstScanFinished)
        XCTAssertEqual(after.phase, .waitingForHomeNetwork)
        let saved = await state.load()
        XCTAssertTrue(saved.firstScanFinished)
        XCTAssertNil(saved.lastSync)
    }

    func testAFailedOrCancelledScanIsNotAFirstScanAndNothingUploads() async {
        for failure in [OtherFailure() as Error, CancellationError()] {
            let rig = scanning([.failure(failure)], [])
            let status = await rig.tracker.syncNow()
            XCTAssertFalse(status.firstScanFinished)
            let uploads = await rig.runner.runs
            XCTAssertEqual(uploads, 0)
            if failure is CancellationError {
                XCTAssertEqual(status.phase, .idle)
            } else {
                XCTAssertEqual(status.phase, .error(UploadStatusTracker.syncStopped))
            }
        }
    }

    func testAnUnreadableQueueDuringTheScanNamesTheQueue() async {
        let rig = scanning([.failure(ResourceStoreError.database("disk"))], [])
        let status = await rig.tracker.syncNow()
        XCTAssertEqual(status.phase, .error(UploadStatusTracker.queueUnreadable))
    }

    func testDeniedPhotoAccessIsItsOwnStateAndTheNextSyncCanRecover() async {
        let rig = scanning(
            [.failure(PhotoLibraryError.accessDenied), .success(IngestSummary())],
            [.success(result(.finished))]
        )
        let denied = await rig.tracker.syncNow()
        XCTAssertEqual(denied.phase, .photosAccessNeeded)
        XCTAssertFalse(denied.firstScanFinished)
        let uploads = await rig.runner.runs
        XCTAssertEqual(uploads, 0)
        let granted = await rig.tracker.refresh()
        XCTAssertEqual(granted.phase, .idle)
        XCTAssertTrue(granted.firstScanFinished)
    }

    func testLastSyncNeedsAFinishedPassAndAnAcceptedCheckin() async {
        let refused = await makeTracker([.success(result(.finished, checkedIn: false))]).syncNow()
        XCTAssertEqual(refused.phase, .idle)
        XCTAssertNil(refused.lastSync)
        let accepted = await makeTracker([.success(result(.finished))]).syncNow()
        XCTAssertEqual(accepted.lastSync, moment)
    }

    func testLastSyncSurvivesARelaunchAndAnUnreadableFileReadsAsNotYet() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("sync-state-\(UUID().uuidString).json")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        let first = scanning([.success(IngestSummary())], [.success(result(.finished))], syncState: FileSyncStateStore(url: url))
        _ = await first.tracker.syncNow()
        let relaunched = scanning([], [], syncState: FileSyncStateStore(url: url))
        let status = await relaunched.tracker.status()
        XCTAssertEqual(status.lastSync, moment)
        XCTAssertTrue(status.firstScanFinished)
        try Data("not json".utf8).write(to: url)
        let damaged = await scanning([], [], syncState: FileSyncStateStore(url: url)).tracker.status()
        XCTAssertNil(damaged.lastSync)
        XCTAssertFalse(damaged.firstScanFinished)
    }

    func testFailuresSayWhetherTheyWillBeRetriedAndWhetherAButtonCanRetryThem() async throws {
        let store = InMemoryResourceStore()
        let unreadable = LocalResource(key: ResourceKey(assetID: "u", kind: .photo), sha1: "", bytes: 0, filename: "u.heic", state: .failed)
        try await store.upsert([resource("g", .hashed), resource("r", .hashed), unreadable])
        try await store.setState(.failed, for: [unreadable.key], failureReason: "the file could not be read")
        try await store.setRecoverable(true, for: [unreadable.key])
        for asset in ["g", "r"] {
            try await store.setState(.failed, for: [ResourceKey(assetID: asset, kind: .photo)], failureReason: "why")
        }
        try await store.setRecoverable(true, for: [ResourceKey(assetID: "g", kind: .photo)])
        let status = await makeTracker([], store: store).status()
        let flags = status.failures.map { "\($0.key.assetID) \($0.recoverable) \($0.canRetry)" }
        XCTAssertEqual(flags, ["g true true", "r false false", "u true false"])
    }

    private func gaveUp(_ store: InMemoryResourceStore, _ assets: [String]) async throws {
        try await store.upsert(assets.map { resource($0, .hashed) })
        let keys = assets.map { ResourceKey(assetID: $0, kind: .photo) }
        try await store.setState(.failed, for: keys, failureReason: "gave up after 3 attempts: server error 503")
        try await store.setRecoverable(true, for: keys)
    }

    func testSyncNowRetriesEveryRecoverableFailureOnceAndARefreshRetriesNone() async throws {
        let store = InMemoryResourceStore()
        try await gaveUp(store, ["a", "b"])
        let rig = scanning([.success(IngestSummary()), .success(IngestSummary())], [.success(result(.finished)), .success(result(.finished))], store: store)
        _ = await rig.tracker.refresh()
        var counts = try await store.counts()
        XCTAssertEqual(counts[.failed], 2)
        _ = await rig.tracker.syncNow()
        counts = try await store.counts()
        XCTAssertEqual(counts[.failed] ?? 0, 0)
        XCTAssertEqual(counts[.hashed], 2)
    }

    func testRetryingOneFileLeavesTheOthersFailed() async throws {
        let store = InMemoryResourceStore()
        try await gaveUp(store, ["a", "b"])
        let rig = scanning([.success(IngestSummary())], [.success(result(.finished))], store: store)
        let status = await rig.tracker.retry([ResourceKey(assetID: "b", kind: .photo)])
        XCTAssertEqual(status.failures.map(\.key.assetID), ["a"])
        let retried = try await store.record(for: ResourceKey(assetID: "b", kind: .photo))
        XCTAssertEqual(retried?.resource.state, .hashed)
        XCTAssertEqual(retried?.attempts, 0)
    }

    func testANewDeviceSessionResetsTheQueueAndTheLastSyncButNotTheFirstScan() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a", .uploaded), resource("n", .notLocal)])
        let state = InMemorySyncStateStore()
        try await state.save(SyncState(firstScanFinished: true, lastSync: moment))
        try await NewDeviceSession.reset(store: store, syncState: state)
        let counts = try await store.counts()
        XCTAssertEqual(counts, [.hashed: 1, .notLocal: 1])
        let saved = await state.load()
        XCTAssertEqual(saved, SyncState(firstScanFinished: true, lastSync: nil))
    }
}
