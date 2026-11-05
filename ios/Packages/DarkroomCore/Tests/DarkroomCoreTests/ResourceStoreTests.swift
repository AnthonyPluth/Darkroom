import XCTest
@testable import DarkroomCore

final class ResourceStoreTests: XCTestCase {
    private func make() -> ResourceStore { InMemoryResourceStore() }

    func testUpsertKeepsInsertionOrderAndReplacesByKey() async throws {
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

    func testSetStateRecordsFailureReasonOnlyForFailed() async throws {
        try await ResourceStoreContract.failureKeepsItsReasonUntilTheResourceMovesOn(make())
    }

    func testSameStateMoveIsAllowedAndUpdatesTheReason() async throws {
        try await ResourceStoreContract.sameStateMoveIsAllowedAndUpdatesTheReason(make())
    }

    func testConditionalSetStateOnlyTouchesRowsWithTheSameContent() async throws {
        try await ResourceStoreContract.conditionalSetStateOnlyTouchesRowsWithTheSameContent(make())
    }

    func testSetStateIgnoresUnknownKeys() async throws {
        try await ResourceStoreContract.setStateIgnoresKeysThatAreGone(make())
    }

    func testAttemptsAndRehashesCountUpAndResetOnUpsert() async throws {
        try await ResourceStoreContract.attemptsAndRehashesCountUpAndResetOnUpsert(make())
    }

    func testConditionalAttemptCountsOnlyAnUnchangedRow() async throws {
        try await ResourceStoreContract.conditionalAttemptCountsOnlyAnUnchangedRow(make())
    }

    func testFilteringAndCounts() async throws {
        try await ResourceStoreContract.countsAndFilteringByState(make())
    }

    func testFailureReasonAccessor() async throws {
        let store = InMemoryResourceStore()
        try await store.upsert([ResourceStoreContract.resource("a")])
        try await store.setState(.failed, for: [ResourceStoreContract.key("a")], failureReason: "why")
        let reason = await store.failureReason(for: ResourceStoreContract.key("a"))
        XCTAssertEqual(reason, "why")
    }
}
