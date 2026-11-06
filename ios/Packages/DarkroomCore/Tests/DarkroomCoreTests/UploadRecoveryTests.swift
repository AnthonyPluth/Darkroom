import XCTest
@testable import DarkroomCore

final class UploadRecoveryTests: XCTestCase {
    private func resource(_ asset: String, _ kind: ResourceKind = .photo, state: ResourceState = .hashed, sha1: String? = nil) -> LocalResource {
        ResourceStoreContract.resource(asset, kind, state: state, sha1: sha1 ?? asset)
    }

    private func key(_ asset: String, _ kind: ResourceKind = .photo) -> ResourceKey {
        ResourceStoreContract.key(asset, kind)
    }

    private func fail(_ store: InMemoryResourceStore, _ keys: [ResourceKey], recoverable: Bool) async throws {
        try await store.setState(.failed, for: keys, failureReason: "why")
        try await store.setRecoverable(recoverable, for: keys)
    }

    func testRetryingMovesRecoverableFailuresBackToHashedWithFreshAttempts() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a"), resource("b"), resource("c"), resource("d", state: .uploaded)])
        try await store.recordAttempt(for: [key("a")])
        try await fail(store, [key("a"), key("c")], recoverable: true)
        try await fail(store, [key("b")], recoverable: false)
        let retried = try await store.retryFailures()
        XCTAssertEqual(retried, 2)
        for asset in ["a", "c"] {
            let record = try await store.record(for: key(asset))
            XCTAssertEqual(record?.resource.state, .hashed)
            XCTAssertEqual(record?.attempts, 0)
            XCTAssertNil(record?.failureReason)
            XCTAssertEqual(record?.recoverable, false)
        }
        let permanent = try await store.record(for: key("b"))
        XCTAssertEqual(permanent?.resource.state, .failed)
        XCTAssertEqual(permanent?.failureReason, "why")
        let uploaded = try await store.record(for: key("d"))
        XCTAssertEqual(uploaded?.resource.state, .uploaded)
    }

    func testRetryingOnlyTheGivenKeys() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a"), resource("b")])
        try await fail(store, [key("a"), key("b")], recoverable: true)
        let retried = try await store.retryFailures(keys: [key("b")])
        XCTAssertEqual(retried, 1)
        let a = try await store.record(for: key("a"))
        let b = try await store.record(for: key("b"))
        XCTAssertEqual(a?.resource.state, .failed)
        XCTAssertEqual(b?.resource.state, .hashed)
    }

    func testAFailureWithoutADescriptorIsNeverMovedToHashed() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a", sha1: "")])
        try await fail(store, [key("a")], recoverable: true)
        let retried = try await store.retryFailures()
        XCTAssertEqual(retried, 0)
        let record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.resource.state, .failed)
    }

    func testRecoverableOnlyMarksFailedRowsAndIsClearedWhenTheRowMovesOn() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a"), resource("b")])
        try await store.setRecoverable(true, for: [key("a")])
        var record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.recoverable, false)
        try await fail(store, [key("b")], recoverable: true)
        record = try await store.record(for: key("b"))
        XCTAssertEqual(record?.recoverable, true)
        try await store.setState(.hashed, for: [key("b")], failureReason: nil)
        record = try await store.record(for: key("b"))
        XCTAssertEqual(record?.recoverable, false)
    }

    func testANewDeviceSessionSendsEveryRecordWithADescriptorBackToHashed() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([
            resource("a", state: .uploaded), resource("b", state: .checked), resource("c"), resource("d"),
            resource("e", state: .notLocal, sha1: ""), resource("f", sha1: ""),
        ])
        try await store.recordAttempt(for: [key("c")])
        try await store.setState(.uploading, for: [key("b")], failureReason: nil)
        try await fail(store, [key("d")], recoverable: false)
        try await fail(store, [key("f")], recoverable: true)
        try await store.resetForNewDeviceSession()
        for asset in ["a", "b", "c", "d"] {
            let record = try await store.record(for: key(asset))
            XCTAssertEqual(record?.resource.state, .hashed, asset)
            XCTAssertEqual(record?.attempts, 0, asset)
            XCTAssertNil(record?.failureReason, asset)
            XCTAssertEqual(record?.resource.sha1, asset)
        }
        let notLocal = try await store.record(for: key("e"))
        XCTAssertEqual(notLocal?.resource.state, .notLocal)
        let unreadable = try await store.record(for: key("f"))
        XCTAssertEqual(unreadable?.resource.state, .failed)
        XCTAssertEqual(unreadable?.failureReason, "why")
        XCTAssertEqual(unreadable?.recoverable, true)
    }

    func testAResetStoreChecksEverythingAgainAndSendsNothingTheServerHolds() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a", state: .uploaded), resource("b", state: .uploaded)])
        try await store.resetForNewDeviceSession()
        let stored = try await store.resources(in: Set(ResourceState.allCases))
        let batches = UploadPlanner.checkBatches(from: stored)
        XCTAssertEqual(batches.flatMap(\.assets).map(\.id), ["a", "b"])
    }

    func testReleasingUploadsPutsOnlyTheUploadingOnesBackToChecked() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([
            resource("a", state: .uploading), resource("b", state: .uploading), resource("c", state: .checked),
            resource("d", state: .uploaded), resource("e", state: .hashed),
        ])
        let released = try await store.releaseUploading()
        XCTAssertEqual(released, 2)
        let counts = try await store.counts()
        XCTAssertEqual(counts[.checked], 3)
        XCTAssertEqual(counts[.uploading] ?? 0, 0)
        XCTAssertEqual(counts[.uploaded], 1)
        XCTAssertEqual(counts[.hashed], 1)
        let again = try await store.releaseUploading()
        XCTAssertEqual(again, 0)
    }
}
