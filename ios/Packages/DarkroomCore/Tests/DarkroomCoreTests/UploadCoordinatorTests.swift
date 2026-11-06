import XCTest
@testable import DarkroomCore

private actor FakeAPI: DeviceAPI {
    var needed: Set<ResourceKey> = []
    var checkError: Error?
    var checkinError: Error?
    var refused: Set<String> = []
    private(set) var checkins: [CheckinRequest] = []
    private(set) var requests: [CheckRequest] = []
    var duringCheck: (@Sendable () async throws -> Void)?

    func setNeeded(_ keys: Set<ResourceKey>) { needed = keys }
    func setCheckError(_ error: Error?) { checkError = error }
    func setCheckinError(_ error: Error?) { checkinError = error }
    func refuse(_ assetIDs: Set<String>) { refused = assetIDs }

    func setDuringCheck(_ action: (@Sendable () async throws -> Void)?) { duringCheck = action }

    func check(_ request: CheckRequest) async throws -> CheckReply {
        requests.append(request)
        try await duringCheck?()
        if let checkError { throw checkError }
        if request.assets.contains(where: { refused.contains($0.id) }) { throw APIError.rejected(status: 400) }
        let keys = request.assets.flatMap { asset in asset.resources.map { ResourceKey(assetID: asset.id, kind: $0.kind) } }
        return CheckReply(needed: keys.filter { needed.contains($0) }.map { NeededResource(id: $0.assetID, kind: $0.kind) })
    }

    func checkin(_ request: CheckinRequest) async throws {
        checkins.append(request)
        if let checkinError { throw checkinError }
    }
}

private actor FakeTransport: UploadTransport {
    var errors: [ResourceKey: Error] = [:]
    var duringUpload: (@Sendable () async throws -> Void)?
    func setDuringUpload(_ action: (@Sendable () async throws -> Void)?) { duringUpload = action }
    private(set) var sent: [ResourceKey] = []

    func fail(_ key: ResourceKey, with error: Error?) { errors[key] = error }

    func upload(_ resource: LocalResource) async throws {
        try await duringUpload?()
        if let error = errors[resource.key] { throw error }
        sent.append(resource.key)
    }
}

private struct FakeRehasher: ResourceRehasher {
    var result: LocalResource?
    func rehash(_ key: ResourceKey) async throws -> LocalResource? { result }
}

private struct ReplacingRehasher: ResourceRehasher {
    let store: InMemoryResourceStore
    let newest: LocalResource
    let result: LocalResource
    func rehash(_ key: ResourceKey) async throws -> LocalResource? {
        try await store.upsert([newest])
        return result
    }
}

private actor Probes {
    var answer = true
    private(set) var count = 0
    func set(_ answer: Bool) { self.answer = answer }
    func next() -> Bool { count += 1; return answer }
}

final class UploadCoordinatorTests: XCTestCase {
    private let a = ResourceKey(assetID: "a", kind: .photo)
    private let b = ResourceKey(assetID: "b", kind: .photo)
    private let now = Date(timeIntervalSince1970: 1_000_000)

    private func hashed(_ key: ResourceKey, sha1: String = "h") -> LocalResource {
        LocalResource(key: key, sha1: sha1, bytes: 5, filename: "\(key.assetID).HEIC", state: .hashed)
    }

    private struct Rig {
        let api = FakeAPI()
        let transport = FakeTransport()
        let store = InMemoryResourceStore()
        let probes = Probes()
    }

    private func coordinator(_ rig: Rig, rehash: LocalResource? = nil, probe: ServerProbe? = nil) -> UploadCoordinator {
        let probes = rig.probes
        let now = self.now
        return UploadCoordinator(
            api: rig.api, transport: rig.transport, rehasher: FakeRehasher(result: rehash), store: rig.store,
            probe: probe ?? ServerProbe { await probes.next() }, appVersion: "1.0", clock: { now }
        )
    }

    private func assertCancelled(_ task: Task<UploadCoordinator.Result, Error>, file: StaticString = #filePath, line: UInt = #line) async {
        switch await task.result {
        case .success(let result): XCTFail("the run finished as \(result.outcome) instead of stopping", file: file, line: line)
        case .failure(let error): XCTAssertTrue(error is CancellationError, "threw \(error)", file: file, line: line)
        }
    }

    func testUploadsWhatTheServerNeedsAndMarksTheRest() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a), hashed(b)])
        await rig.api.setNeeded([a])
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.uploaded, 1)
        XCTAssertEqual(result.alreadyOnServer, 1)
        XCTAssertEqual(result.outcome, .finished)
        let sent = await rig.transport.sent
        XCTAssertEqual(sent, [a])
        let counts = try await rig.store.counts()
        XCTAssertEqual(counts[.uploaded], 2)
        let checkins = await rig.api.checkins
        XCTAssertEqual(checkins.map(\.pendingUploads), [0])
        XCTAssertEqual(checkins.first?.appVersion, "1.0")
    }

    func testACheckIsSentWithTheAssetsWholeResourceSet() async throws {
        let rig = Rig()
        let video = ResourceKey(assetID: "a", kind: .liveVideo)
        var uploaded = hashed(a)
        uploaded.state = .uploaded
        try await rig.store.upsert([uploaded, hashed(video)])
        await rig.api.setNeeded([video])
        _ = try await coordinator(rig).run()
        let requests = await rig.api.requests
        XCTAssertEqual(requests.first?.assets.first?.resources.map(\.kind), [.photo, .liveVideo])
        let counts = try await rig.store.counts()
        XCTAssertEqual(counts[.uploaded], 2)
    }

    func testAResourceChangedWhileTheCheckWasInFlightIsNotMarkedUploaded() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a, sha1: "old")])
        let store = rig.store
        let changed = hashed(a, sha1: "new")
        await rig.api.setDuringCheck { try await store.upsert([changed]) }
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.alreadyOnServer, 0)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.sha1, "new")
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    func testAResourceReplacedWhileItUploadedKeepsItsNewHashAndState() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a, sha1: "old")])
        await rig.api.setNeeded([a])
        let store = rig.store
        let replacement = hashed(a, sha1: "new")
        await rig.transport.setDuringUpload { try await store.upsert([replacement]) }
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.uploaded, 0)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.sha1, "new")
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    func testARehashDoesNotOverwriteARowReplacedWhileItRan() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a, sha1: "old")])
        await rig.api.setNeeded([a])
        await rig.transport.fail(a, with: APIError.rejected(status: 422))
        let newest = hashed(a, sha1: "newest")
        let store = rig.store
        let rehasher = ReplacingRehasher(store: store, newest: newest, result: hashed(a, sha1: "rehashed"))
        let coordinator = UploadCoordinator(
            api: rig.api, transport: rig.transport, rehasher: rehasher, store: rig.store,
            probe: ServerProbe { true }, appVersion: "1.0", clock: { self.now }
        )
        _ = try await coordinator.run()
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.sha1, "newest")
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    func testConcurrentRunsShareOnePassAndNeverRecoverTheLiveUpload() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.transport.setDuringUpload { try await Task.sleep(nanoseconds: 200_000_000) }
        let coordinator = coordinator(rig)
        let first = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 50_000_000)
        let second = Task { try await coordinator.run() }
        let firstResult = try await first.value
        let secondResult = try await second.value
        XCTAssertEqual(firstResult, secondResult)
        XCTAssertEqual(firstResult.uploaded, 1)
        let sent = await rig.transport.sent
        XCTAssertEqual(sent, [a])
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .uploaded)
        let third = try await coordinator.run()
        XCTAssertEqual(third.uploaded, 0)
    }

    func testACancelledPassDoesNotCancelACallerThatJoinedIt() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.transport.setDuringUpload { try await Task.sleep(nanoseconds: 200_000_000) }
        let coordinator = coordinator(rig)
        let first = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 50_000_000)
        let second = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 20_000_000)
        first.cancel()
        let joined = try await second.value
        XCTAssertEqual(joined.outcome, .finished)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .uploaded)
    }

    func testInterruptedUploadsAreRecoveredAndRetried() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        try await rig.store.setState(.checked, for: [a], failureReason: nil)
        try await rig.store.setState(.uploading, for: [a], failureReason: nil)
        await rig.probes.set(false)
        let offline = try await coordinator(rig).run()
        XCTAssertEqual(offline.outcome, .waitingForNetwork)
        let recovered = try await rig.store.record(for: a)
        XCTAssertEqual(recovered?.resource.state, .checked)

        await rig.probes.set(true)
        let online = try await coordinator(rig).run()
        XCTAssertEqual(online.uploaded, 1)
    }

    func testOfflineWaitsAndDoesNotProbeAgainBeforeTheDelay() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.probes.set(false)
        let coordinator = coordinator(rig)
        let first = try await coordinator.run()
        let second = try await coordinator.run()
        XCTAssertEqual(first.outcome, .waitingForNetwork)
        XCTAssertEqual(second.outcome, .waitingForNetwork)
        let probes = await rig.probes.count
        XCTAssertEqual(probes, 1)
        let status = await coordinator.reachability
        XCTAssertEqual(status, .waiting(failures: 1))
        let sent = await rig.transport.sent
        XCTAssertEqual(sent, [])
    }

    func testAManualRunProbesAgainDuringTheBackoffAndAnAutomaticOneDoesNot() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.probes.set(false)
        let coordinator = coordinator(rig)
        let first = try await coordinator.run()
        let skipped = try await coordinator.run()
        XCTAssertEqual(first.outcome, .waitingForNetwork)
        XCTAssertEqual(skipped.outcome, .waitingForNetwork)
        var probes = await rig.probes.count
        XCTAssertEqual(probes, 1)
        await rig.probes.set(true)
        let stillWaiting = try await coordinator.run()
        XCTAssertEqual(stillWaiting.outcome, .waitingForNetwork)
        probes = await rig.probes.count
        XCTAssertEqual(probes, 1)
        let manual = try await coordinator.run(ignoringBackoff: true)
        XCTAssertEqual(manual.outcome, .finished)
        XCTAssertEqual(manual.uploaded, 1)
        probes = await rig.probes.count
        XCTAssertEqual(probes, 2)
        let status = await coordinator.reachability
        XCTAssertEqual(status, .reachable)
    }

    func testTheResultSaysWhetherDarkroomAcceptedTheCheckin() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        let accepted = try await coordinator(rig).run()
        XCTAssertEqual(accepted.outcome, .finished)
        XCTAssertTrue(accepted.checkinAccepted)
        for failure in [APIError.rejected(status: 400), APIError.server(status: 500)] {
            await rig.api.setCheckinError(failure)
            let refused = try await coordinator(rig).run()
            XCTAssertEqual(refused.outcome, .finished)
            XCTAssertFalse(refused.checkinAccepted)
        }
        await rig.api.setCheckinError(APIError.unauthorized)
        let signedOut = try await coordinator(rig).run()
        XCTAssertEqual(signedOut.outcome, .needsSignIn)
        XCTAssertFalse(signedOut.checkinAccepted)
        let waiting = try await coordinator(rig, probe: ServerProbe { false }).run()
        XCTAssertEqual(waiting.outcome, .waitingForNetwork)
        XCTAssertFalse(waiting.checkinAccepted)
    }

    func testUnauthorizedStopsAndKeepsTheResourceChecked() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.transport.fail(a, with: APIError.unauthorized)
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.outcome, .needsSignIn)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .checked)
    }

    func testServerErrorsRetryThenFailAfterTheAttemptLimit() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.transport.fail(a, with: APIError.server(status: 503))
        let coordinator = coordinator(rig)
        for expected in [ResourceState.checked, .checked, .failed] {
            _ = try await coordinator.run()
            let record = try await rig.store.record(for: a)
            XCTAssertEqual(record?.resource.state, expected)
        }
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.attempts, 3)
        XCTAssertEqual(record?.failureReason?.contains("503"), true)
    }

    func testAFileThatGaveUpAfterServerErrorsCanBeRetriedButARefusedOneCannot() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a), hashed(b)])
        await rig.api.setNeeded([a, b])
        await rig.transport.fail(a, with: APIError.server(status: 503))
        await rig.transport.fail(b, with: APIError.rejected(status: 400))
        let coordinator = coordinator(rig)
        for _ in 0..<3 { _ = try await coordinator.run() }
        let gaveUp = try await rig.store.record(for: a)
        let refused = try await rig.store.record(for: b)
        XCTAssertEqual(gaveUp?.resource.state, .failed)
        XCTAssertEqual(gaveUp?.recoverable, true)
        XCTAssertEqual(refused?.resource.state, .failed)
        XCTAssertEqual(refused?.recoverable, false)
    }

    func testAFailedSiblingWithoutADescriptorIsLeftOutOfTheCheckAndTheValidOneUploads() async throws {
        let rig = Rig()
        let video = ResourceKey(assetID: "a", kind: .liveVideo)
        let unreadable = LocalResource(key: video, sha1: "", bytes: 0, filename: "a.MOV", state: .failed)
        try await rig.store.upsert([hashed(a), unreadable])
        await rig.api.setNeeded([a])
        let result = try await coordinator(rig).run()
        let requests = await rig.api.requests
        XCTAssertEqual(requests.flatMap(\.assets).flatMap(\.resources).map(\.kind), [.photo])
        XCTAssertEqual(result.uploaded, 1)
        XCTAssertEqual(result.failed, 0)
        let photo = try await rig.store.record(for: a)
        let sibling = try await rig.store.record(for: video)
        XCTAssertEqual(photo?.resource.state, .uploaded)
        XCTAssertEqual(sibling?.resource, unreadable)
    }

    func testTransportErrorStopsTheRunAsOffline() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a), hashed(b)])
        await rig.api.setNeeded([a, b])
        await rig.transport.fail(a, with: APIError.transport("-1009"))
        let coordinator = coordinator(rig)
        let result = try await coordinator.run()
        XCTAssertEqual(result.outcome, .waitingForNetwork)
        let sent = await rig.transport.sent
        XCTAssertEqual(sent, [])
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .checked)
    }

    func testHashMismatchWithAChangedFileStartsOver() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a, sha1: "old")])
        await rig.api.setNeeded([a])
        await rig.transport.fail(a, with: APIError.rejected(status: 422))
        let result = try await coordinator(rig, rehash: hashed(a, sha1: "new")).run()
        XCTAssertEqual(result.failed, 0)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .hashed)
        XCTAssertEqual(record?.resource.sha1, "new")
    }

    func testHashMismatchWithTheSameHashFails() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a, sha1: "same")])
        await rig.api.setNeeded([a])
        await rig.transport.fail(a, with: APIError.rejected(status: 422))
        let result = try await coordinator(rig, rehash: hashed(a, sha1: "same")).run()
        XCTAssertEqual(result.failed, 1)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .failed)
    }

    func testNotCheckedGoesBackToHashedAndBadRequestFails() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a), hashed(b)])
        await rig.api.setNeeded([a, b])
        await rig.transport.fail(a, with: APIError.rejected(status: 404))
        await rig.transport.fail(b, with: APIError.rejected(status: 400))
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.failed, 1)
        let first = try await rig.store.record(for: a)
        let second = try await rig.store.record(for: b)
        XCTAssertEqual(first?.resource.state, .hashed)
        XCTAssertEqual(second?.resource.state, .failed)
    }

    func testCheckErrorsThatNeedSignInStopTheRun() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setCheckError(APIError.unauthorized)
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.outcome, .needsSignIn)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    func testARevokedDeviceAsksForSignInBeforeUploading() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setCheckError(APIError.revoked)
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.outcome, .needsSignIn)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    func testAJoinedCallerThatIsCancelledStopsWaitingWithoutCancellingThePass() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.transport.setDuringUpload { try await Task.sleep(nanoseconds: 500_000_000) }
        let coordinator = coordinator(rig)
        let first = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 50_000_000)
        let second = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 20_000_000)
        second.cancel()
        await assertCancelled(second)
        let during = try await rig.store.record(for: a)
        XCTAssertEqual(during?.resource.state, .uploading)
        let result = try await first.value
        XCTAssertEqual(result.outcome, .finished)
        XCTAssertEqual(result.uploaded, 1)
    }

    func testACheckinThatLosesTheNetworkWaitsForIt() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.api.setCheckinError(APIError.transport("-1009"))
        let coordinator = coordinator(rig)
        let result = try await coordinator.run()
        XCTAssertEqual(result.uploaded, 1)
        XCTAssertEqual(result.outcome, .waitingForNetwork)
        let status = await coordinator.reachability
        XCTAssertEqual(status, .waiting(failures: 1))
    }

    func testSignInAndNetworkStopsDoNotUseUpUploadAttempts() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setNeeded([a])
        await rig.transport.fail(a, with: APIError.unauthorized)
        for _ in 0..<2 {
            let result = try await coordinator(rig).run()
            XCTAssertEqual(result.outcome, .needsSignIn)
        }
        await rig.transport.fail(a, with: APIError.transport("-1009"))
        for _ in 0..<2 {
            let result = try await coordinator(rig).run()
            XCTAssertEqual(result.outcome, .waitingForNetwork)
        }
        await rig.transport.fail(a, with: APIError.server(status: 503))
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.failed, 0)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .checked)
        XCTAssertEqual(record?.attempts, 1)
    }

    func testACancelledCheckIsNotTakenForALostNetwork() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        await rig.api.setDuringCheck {
            do {
                try await Task.sleep(nanoseconds: 2_000_000_000)
            } catch {
                throw APIError.transport("-999")
            }
        }
        let coordinator = coordinator(rig)
        let run = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 50_000_000)
        run.cancel()
        await assertCancelled(run)
        let status = await coordinator.reachability
        XCTAssertEqual(status, .reachable)
        let record = try await rig.store.record(for: a)
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    func testACancelledProbeIsNotTakenForALostNetwork() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a)])
        let slowProbe = ServerProbe {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            return false
        }
        let coordinator = coordinator(rig, probe: slowProbe)
        let run = Task { try await coordinator.run() }
        try await Task.sleep(nanoseconds: 50_000_000)
        run.cancel()
        await assertCancelled(run)
        let status = await coordinator.reachability
        XCTAssertEqual(status, .unknown)
    }

    func testACheckRefusedForOneAssetFailsThatAssetAndChecksTheRest() async throws {
        let rig = Rig()
        let bad = ResourceKey(assetID: String(repeating: "x", count: 201), kind: .photo)
        let c = ResourceKey(assetID: "c", kind: .photo)
        try await rig.store.upsert([hashed(a), hashed(bad), hashed(b), hashed(c)])
        await rig.api.setNeeded([a, b, c])
        await rig.api.refuse([bad.assetID])
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.outcome, .finished)
        XCTAssertEqual(result.uploaded, 3)
        XCTAssertEqual(result.failed, 1)
        let sent = await rig.transport.sent
        XCTAssertEqual(Set(sent), [a, b, c])
        let record = try await rig.store.record(for: bad)
        XCTAssertEqual(record?.resource.state, .failed)
        XCTAssertEqual(record?.failureReason?.contains("400"), true)
        let checkins = await rig.api.checkins
        XCTAssertEqual(checkins.map(\.pendingUploads), [0])
    }

    func testACheckRefusedForAnotherReasonLeavesTheResourcesToRetry() async throws {
        let rig = Rig()
        try await rig.store.upsert([hashed(a), hashed(b)])
        await rig.api.setCheckError(APIError.rejected(status: 404))
        let result = try await coordinator(rig).run()
        XCTAssertEqual(result.failed, 0)
        let requests = await rig.api.requests
        XCTAssertEqual(requests.count, 1)
        let counts = try await rig.store.counts()
        XCTAssertEqual(counts[.hashed], 2)
    }
}
