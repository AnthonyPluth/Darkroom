#if canImport(SQLite3)
import Foundation
import SQLite3
import XCTest
@testable import DarkroomCore

final class SQLiteResourceStoreTests: XCTestCase {
    private func make() throws -> ResourceStore {
        try SQLiteResourceStore(path: ":memory:")
    }

    private func databaseURL() -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("resources-\(UUID().uuidString).sqlite")
        addTeardownBlock {
            for suffix in ["", "-wal", "-shm"] {
                try? FileManager.default.removeItem(atPath: url.path + suffix)
            }
        }
        return url
    }

    func testUpsertReplacesByKeyAndKeepsInsertionOrder() async throws {
        try await ResourceStoreContract.upsertReplacesByKeyAndKeepsInsertionOrder(make())
    }

    func testUpsertStoresEveryField() async throws {
        try await ResourceStoreContract.upsertStoresEveryField(make())
    }

    func testUnknownKeyHasNoRecord() async throws {
        try await ResourceStoreContract.unknownKeyHasNoRecord(make())
    }

    func testWalksTheUploadPath() async throws {
        try await ResourceStoreContract.walksTheUploadPath(make())
    }

    func testIllegalTransitionThrowsAndChangesNothing() async throws {
        try await ResourceStoreContract.illegalTransitionThrowsAndChangesNothing(make())
    }

    func testUploadedAndNotLocalAreTerminal() async throws {
        try await ResourceStoreContract.uploadedAndNotLocalAreTerminal(make())
    }

    func testFailureKeepsItsReasonUntilTheResourceMovesOn() async throws {
        try await ResourceStoreContract.failureKeepsItsReasonUntilTheResourceMovesOn(make())
    }

    func testSameStateMoveIsAllowedAndUpdatesTheReason() async throws {
        try await ResourceStoreContract.sameStateMoveIsAllowedAndUpdatesTheReason(make())
    }

    func testConditionalSetStateOnlyTouchesRowsWithTheSameContent() async throws {
        try await ResourceStoreContract.conditionalSetStateOnlyTouchesRowsWithTheSameContent(make())
    }

    func testSetStateIgnoresKeysThatAreGone() async throws {
        try await ResourceStoreContract.setStateIgnoresKeysThatAreGone(make())
    }

    func testRemoveDeletesOnlyTheGivenKeys() async throws {
        try await ResourceStoreContract.removeDeletesOnlyTheGivenKeysAndLaterInsertsGoLast(make())
    }

    func testAttemptsAndRehashesCountUpAndResetOnUpsert() async throws {
        try await ResourceStoreContract.attemptsAndRehashesCountUpAndResetOnUpsert(make())
    }

    func testConditionalAttemptCountsOnlyAnUnchangedRow() async throws {
        try await ResourceStoreContract.conditionalAttemptCountsOnlyAnUnchangedRow(make())
    }

    func testCountsAndFilteringByState() async throws {
        try await ResourceStoreContract.countsAndFilteringByState(make())
    }

    func testRecoverableMarksFailedRowsOnly() async throws {
        try await ResourceStoreContract.recoverableMarksFailedRowsOnlyAndIsClearedWhenTheRowMovesOnOrIsReplaced(make())
    }

    func testRecoverableSurvivesReopeningTheDatabase() async throws {
        let url = databaseURL()
        let key = ResourceStoreContract.key("a")
        do {
            let store = try SQLiteResourceStore(path: url.path)
            try await store.upsert([ResourceStoreContract.resource("a")])
            try await store.setState(.failed, for: [key], failureReason: "gave up")
            try await store.setRecoverable(true, for: [key])
        }
        let reopened = try SQLiteResourceStore(path: url.path)
        let record = try await reopened.record(for: key)
        XCTAssertEqual(record?.resource.state, .failed)
        XCTAssertEqual(record?.recoverable, true)
    }

    func testADatabaseFromTheFirstSchemaIsUpgradedAndKeepsItsRows() async throws {
        let url = databaseURL()
        var handle: OpaquePointer?
        XCTAssertEqual(sqlite3_open(url.path, &handle), SQLITE_OK)
        let created = """
        CREATE TABLE resources (
            asset_id TEXT NOT NULL, kind TEXT NOT NULL, sha1 TEXT NOT NULL, bytes INTEGER NOT NULL,
            filename TEXT NOT NULL, state TEXT NOT NULL, failure_reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, rehashes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (asset_id, kind)
        );
        INSERT INTO resources (asset_id, kind, sha1, bytes, filename, state, failure_reason, attempts)
        VALUES ('a', 'photo', 'abc', 7, 'a.HEIC', 'failed', 'gave up', 3);
        PRAGMA user_version = 1;
        """
        XCTAssertEqual(sqlite3_exec(handle, created, nil, nil, nil), SQLITE_OK)
        sqlite3_close(handle)
        let store = try SQLiteResourceStore(path: url.path)
        let key = ResourceStoreContract.key("a")
        let record = try await store.record(for: key)
        XCTAssertEqual(record?.resource.sha1, "abc")
        XCTAssertEqual(record?.failureReason, "gave up")
        XCTAssertEqual(record?.attempts, 3)
        XCTAssertEqual(record?.recoverable, false)
        try await store.setRecoverable(true, for: [key])
        let marked = try await store.record(for: key)
        XCTAssertEqual(marked?.recoverable, true)
        _ = try SQLiteResourceStore(path: url.path)
    }

    func testStateSurvivesReopeningTheDatabase() async throws {
        let url = databaseURL()
        let key = ResourceStoreContract.key("a", .liveVideo)
        do {
            let store = try SQLiteResourceStore(path: url.path)
            try await store.upsert([ResourceStoreContract.resource("a", .liveVideo), ResourceStoreContract.resource("b")])
            try await store.setState(.checked, for: [key], failureReason: nil)
            try await store.setState(.uploading, for: [key], failureReason: nil)
            try await store.recordAttempt(for: [key])
            try await store.setState(.failed, for: [ResourceStoreContract.key("b")], failureReason: "refused")
        }
        let reopened = try SQLiteResourceStore(path: url.path)
        let interrupted = try await reopened.record(for: key)
        XCTAssertEqual(interrupted?.resource.state, .uploading)
        XCTAssertEqual(interrupted?.attempts, 1)
        let failed = try await reopened.record(for: ResourceStoreContract.key("b"))
        XCTAssertEqual(failed?.failureReason, "refused")
        let order = try await reopened.resources(in: Set(ResourceState.allCases)).map(\.key.assetID)
        XCTAssertEqual(order, ["a", "b"])
    }

    func testInterruptedUploadResumesAsCheckedAfterReopening() async throws {
        let url = databaseURL()
        let key = ResourceStoreContract.key("a")
        do {
            let store = try SQLiteResourceStore(path: url.path)
            try await store.upsert([ResourceStoreContract.resource("a")])
            try await store.setState(.checked, for: [key], failureReason: nil)
            try await store.setState(.uploading, for: [key], failureReason: nil)
        }
        let reopened = try SQLiteResourceStore(path: url.path)
        let stuck = try await reopened.resources(in: [.uploading])
        try await reopened.setState(.checked, for: stuck.map(\.key), failureReason: nil)
        let counts = try await reopened.counts()
        XCTAssertEqual(counts, [.checked: 1])
    }

    func testValuesWithQuotesAndUnicodeAreStoredAsGiven() async throws {
        let store = try SQLiteResourceStore(path: ":memory:")
        let awkward = LocalResource(
            key: ResourceKey(assetID: "AB/12'; DROP TABLE resources;--/L0/001", kind: .photo),
            sha1: "0",
            bytes: 0,
            filename: "Sch\u{F6}ne \"Aussicht\" \u{1F305}.HEIC",
            state: .hashed
        )
        try await store.upsert([awkward])
        try await store.setState(.failed, for: [awkward.key], failureReason: "can't read \"it\"")
        let record = try await store.record(for: awkward.key)
        XCTAssertEqual(record?.resource.filename, awkward.filename)
        XCTAssertEqual(record?.failureReason, "can't read \"it\"")
    }

    func testDatabaseFromANewerAppIsRefused() throws {
        let url = databaseURL()
        var handle: OpaquePointer?
        XCTAssertEqual(sqlite3_open(url.path, &handle), SQLITE_OK)
        XCTAssertEqual(sqlite3_exec(handle, "PRAGMA user_version = 99", nil, nil, nil), SQLITE_OK)
        sqlite3_close(handle)
        XCTAssertThrowsError(try SQLiteResourceStore(path: url.path)) { error in
            guard case ResourceStoreError.database = error else { return XCTFail("\(error)") }
        }
    }

    func testUnopenablePathThrows() {
        XCTAssertThrowsError(try SQLiteResourceStore(path: "/nonexistent-directory/resources.sqlite"))
    }
}
#endif
