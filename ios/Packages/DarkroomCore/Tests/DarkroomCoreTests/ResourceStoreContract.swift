import XCTest
@testable import DarkroomCore

enum ResourceStoreContract {
    static func resource(_ asset: String, _ kind: ResourceKind = .photo, state: ResourceState = .hashed, sha1: String? = nil) -> LocalResource {
        LocalResource(key: ResourceKey(assetID: asset, kind: kind), sha1: sha1 ?? asset, bytes: 7, filename: "\(asset).bin", state: state)
    }

    static func key(_ asset: String, _ kind: ResourceKind = .photo) -> ResourceKey {
        ResourceKey(assetID: asset, kind: kind)
    }

    static func upsertReplacesByKeyAndKeepsInsertionOrder(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b"), resource("a", .liveVideo)])
        try await store.upsert([resource("a", state: .checked, sha1: "new")])
        let all = try await store.resources(in: Set(ResourceState.allCases))
        XCTAssertEqual(all.map { "\($0.key.assetID).\($0.key.kind.rawValue)" }, ["a.photo", "b.photo", "a.live_video"])
        XCTAssertEqual(all[0].sha1, "new")
        XCTAssertEqual(all[0].state, .checked)
    }

    static func upsertStoresEveryField(_ store: ResourceStore) async throws {
        let original = LocalResource(key: key("a", .adjusted), sha1: "abc", bytes: 5_000_000_000, filename: "IMG 1_adjusted.HEIC", state: .hashed)
        try await store.upsert([original])
        let record = try await store.record(for: original.key)
        XCTAssertEqual(record, ResourceRecord(resource: original))
    }

    static func conditionalSetStateOnlyTouchesRowsWithTheSameContent(_ store: ResourceStore) async throws {
        let old = resource("a", state: .checked, sha1: "old")
        try await store.upsert([old, resource("b", state: .uploading)])
        try await store.upsert([resource("a", sha1: "new")])
        let applied = try await store.setState(.uploaded, ifUnchanged: [old, resource("b", state: .uploading), resource("gone")], failureReason: nil)
        XCTAssertEqual(applied, 1)
        let a = try await store.record(for: key("a"))
        let b = try await store.record(for: key("b"))
        XCTAssertEqual(a?.resource.state, .hashed)
        XCTAssertEqual(a?.resource.sha1, "new")
        XCTAssertEqual(b?.resource.state, .uploaded)
    }

    static func unknownKeyHasNoRecord(_ store: ResourceStore) async throws {
        let record = try await store.record(for: key("nope"))
        XCTAssertNil(record)
    }

    static func walksTheUploadPath(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a")])
        for state in [ResourceState.checked, .uploading, .uploaded] {
            try await store.setState(state, for: [key("a")], failureReason: nil)
            let record = try await store.record(for: key("a"))
            XCTAssertEqual(record?.resource.state, state)
        }
    }

    static func illegalTransitionThrowsAndChangesNothing(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b", state: .checked)])
        do {
            try await store.setState(.uploading, for: [key("b"), key("a")], failureReason: nil)
            XCTFail("hashed cannot move to uploading")
        } catch let error as ResourceStoreError {
            XCTAssertEqual(error, .illegalTransition(key: key("a"), from: .hashed, to: .uploading))
        }
        let states = try await store.resources(in: Set(ResourceState.allCases)).map(\.state)
        XCTAssertEqual(states, [.hashed, .checked])
    }

    static func uploadedAndNotLocalAreTerminal(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a", state: .uploaded), resource("b", state: .notLocal)])
        for asset in ["a", "b"] {
            do {
                try await store.setState(.hashed, for: [key(asset)], failureReason: nil)
                XCTFail("\(asset) must stay put")
            } catch is ResourceStoreError {}
        }
    }

    static func failureKeepsItsReasonUntilTheResourceMovesOn(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a")])
        try await store.setState(.failed, for: [key("a")], failureReason: "the server refused the file")
        var record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.failureReason, "the server refused the file")
        try await store.setState(.hashed, for: [key("a")], failureReason: "ignored")
        record = try await store.record(for: key("a"))
        XCTAssertNil(record?.failureReason)
        XCTAssertEqual(record?.resource.state, .hashed)
    }

    static func sameStateMoveIsAllowedAndUpdatesTheReason(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a")])
        try await store.setState(.failed, for: [key("a")], failureReason: "first")
        try await store.setState(.failed, for: [key("a")], failureReason: "second")
        let record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.failureReason, "second")
    }

    static func setStateIgnoresKeysThatAreGone(_ store: ResourceStore) async throws {
        try await store.setState(.uploaded, for: [key("x")], failureReason: nil)
        let counts = try await store.counts()
        XCTAssertTrue(counts.isEmpty)
    }

    static func removeDeletesOnlyTheGivenKeysAndLaterInsertsGoLast(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b"), resource("a", .liveVideo), resource("c", state: .uploaded)])
        try await store.remove([key("b"), key("a", .liveVideo), key("never-stored")])
        var all = try await store.resources(in: Set(ResourceState.allCases))
        XCTAssertEqual(all.map(\.key), [key("a"), key("c")])
        let gone = try await store.record(for: key("b"))
        XCTAssertNil(gone)
        let counts = try await store.counts()
        XCTAssertEqual(counts, [.hashed: 1, .uploaded: 1])
        try await store.upsert([resource("b")])
        all = try await store.resources(in: Set(ResourceState.allCases))
        XCTAssertEqual(all.map(\.key), [key("a"), key("c"), key("b")])
        try await store.remove([])
        let unchanged = try await store.resources(in: Set(ResourceState.allCases))
        XCTAssertEqual(unchanged.count, 3)
    }

    static func attemptsAndRehashesCountUpAndResetOnUpsert(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b")])
        try await store.recordAttempt(for: [key("a"), key("b")])
        try await store.recordAttempt(for: [key("a")])
        try await store.recordRehash(for: key("a"))
        var record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.attempts, 2)
        XCTAssertEqual(record?.rehashes, 1)
        let other = try await store.record(for: key("b"))
        XCTAssertEqual(other?.attempts, 1)
        XCTAssertEqual(other?.rehashes, 0)
        try await store.upsert([resource("a", sha1: "changed")])
        record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.attempts, 0)
        XCTAssertEqual(record?.rehashes, 0)
    }

    static func conditionalAttemptCountsOnlyAnUnchangedRow(_ store: ResourceStore) async throws {
        let old = resource("a", state: .uploading, sha1: "old")
        try await store.upsert([old])
        let first = try await store.recordAttempt(ifUnchanged: old)
        let second = try await store.recordAttempt(ifUnchanged: old)
        XCTAssertEqual(first, 1)
        XCTAssertEqual(second, 2)
        try await store.upsert([resource("a", sha1: "new")])
        let stale = try await store.recordAttempt(ifUnchanged: old)
        let gone = try await store.recordAttempt(ifUnchanged: resource("gone"))
        XCTAssertNil(stale)
        XCTAssertNil(gone)
        let record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.attempts, 0)
    }

    static func countsAndFilteringByState(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b", state: .uploaded), resource("c", state: .uploaded), resource("d", state: .notLocal)])
        let counts = try await store.counts()
        XCTAssertEqual(counts, [.hashed: 1, .uploaded: 2, .notLocal: 1])
        let pending = try await store.resources(in: [.hashed, .notLocal])
        XCTAssertEqual(pending.map(\.key.assetID), ["a", "d"])
        let none = try await store.resources(in: [])
        XCTAssertTrue(none.isEmpty)
    }

    static func recoverableMarksFailedRowsOnlyAndIsClearedWhenTheRowMovesOnOrIsReplaced(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b"), resource("c")])
        try await store.setRecoverable(true, for: [key("a")])
        var record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.recoverable, false)
        try await store.setState(.failed, for: [key("a"), key("b"), key("c")], failureReason: "why")
        try await store.setRecoverable(true, for: [key("a"), key("b"), key("never-stored")])
        record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.recoverable, true)
        XCTAssertEqual(record?.failureReason, "why")
        let untouched = try await store.record(for: key("c"))
        XCTAssertEqual(untouched?.recoverable, false)
        try await store.setState(.hashed, for: [key("a")], failureReason: nil)
        record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.recoverable, false)
        try await store.upsert([resource("b", sha1: "new")])
        record = try await store.record(for: key("b"))
        XCTAssertEqual(record?.recoverable, false)
        try await store.setRecoverable(false, for: [])
    }

    static func setFailedWritesTheStateTheReasonAndTheRecoverableFlagTogether(_ store: ResourceStore) async throws {
        try await store.upsert([resource("a"), resource("b"), resource("c")])
        try await store.setFailed(for: [key("a"), key("never-stored")], reason: "why", recoverable: true)
        var record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.resource.state, .failed)
        XCTAssertEqual(record?.failureReason, "why")
        XCTAssertEqual(record?.recoverable, true)
        try await store.setFailed(for: [key("b")], reason: "permanent", recoverable: false)
        record = try await store.record(for: key("b"))
        XCTAssertEqual(record?.resource.state, .failed)
        XCTAssertEqual(record?.recoverable, false)
        let moved = try await store.setFailed(ifUnchanged: [resource("c"), resource("a", sha1: "other")], reason: "gave up", recoverable: true)
        XCTAssertEqual(moved, 1)
        record = try await store.record(for: key("c"))
        XCTAssertEqual(record?.resource.state, .failed)
        XCTAssertEqual(record?.recoverable, true)
        record = try await store.record(for: key("a"))
        XCTAssertEqual(record?.failureReason, "why")
        try await store.setState(.hashed, for: [key("c")], failureReason: nil)
        record = try await store.record(for: key("c"))
        XCTAssertEqual(record?.recoverable, false)
    }
}
