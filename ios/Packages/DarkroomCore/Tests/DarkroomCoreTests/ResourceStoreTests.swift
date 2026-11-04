import XCTest
@testable import DarkroomCore

final class ResourceStoreTests: XCTestCase {
    private func resource(_ asset: String, state: ResourceState = .hashed) -> LocalResource {
        LocalResource(key: ResourceKey(assetID: asset, kind: .photo), sha1: asset, bytes: 1, filename: asset, state: state)
    }

    func testUpsertKeepsInsertionOrderAndReplacesByKey() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a"), resource("b")])
        try await store.upsert([resource("a", state: .checked)])
        let all = try await store.resources(in: Set(ResourceState.allCases))
        XCTAssertEqual(all.map(\.key.assetID), ["a", "b"])
        XCTAssertEqual(all[0].state, .checked)
    }

    func testSetStateRecordsFailureReasonOnlyForFailed() async throws {
        let store = InMemoryResourceStore()
        let key = ResourceKey(assetID: "a", kind: .photo)
        try await store.upsert([resource("a")])
        try await store.setState(.failed, for: [key], failureReason: "hash mismatch")
        let reason = await store.failureReason(for: key)
        XCTAssertEqual(reason, "hash mismatch")
        try await store.setState(.uploaded, for: [key], failureReason: "ignored")
        let cleared = await store.failureReason(for: key)
        XCTAssertNil(cleared)
    }

    func testSetStateIgnoresUnknownKeys() async throws {
        let store = InMemoryResourceStore()
        try await store.setState(.uploaded, for: [ResourceKey(assetID: "x", kind: .photo)], failureReason: nil)
        let counts = try await store.counts()
        XCTAssertTrue(counts.isEmpty)
    }

    func testFilteringAndCounts() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([resource("a"), resource("b", state: .uploaded), resource("c", state: .uploaded)])
        let pending = try await store.resources(in: [.hashed])
        XCTAssertEqual(pending.map(\.key.assetID), ["a"])
        let counts = try await store.counts()
        XCTAssertEqual(counts[.uploaded], 2)
        XCTAssertEqual(counts[.hashed], 1)
    }
}
