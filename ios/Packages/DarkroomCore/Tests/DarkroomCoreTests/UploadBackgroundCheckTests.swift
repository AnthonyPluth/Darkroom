import XCTest
@testable import DarkroomCore

private func status(_ phase: UploadStatus.Phase, uploaded: Int = 0) -> UploadStatus {
    UploadStatus(phase: phase, counts: [.uploaded: uploaded], failures: [], lastSync: nil, firstScanFinished: true)
}

private final class Calls: @unchecked Sendable {
    private let lock = NSLock()
    private var names: [String] = []

    var order: [String] {
        lock.lock()
        defer { lock.unlock() }
        return names
    }

    func add(_ name: String) {
        lock.lock()
        names.append(name)
        lock.unlock()
    }
}

final class UploadBackgroundCheckTests: XCTestCase {
    func testMoreUploadedFilesThanBeforeIsUploaded() {
        let outcome = UploadBackgroundCheck.outcome(after: status(.idle, uploaded: 5), uploadedBefore: 3)
        XCTAssertEqual(outcome, .uploaded)
    }

    func testNoNewUploadsIsNothingNewEvenWhenFilesFailed() {
        let failed = UploadStatus(
            phase: .idle, counts: [.uploaded: 3, .failed: 2], failures: [], lastSync: nil, firstScanFinished: true
        )
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: failed, uploadedBefore: 3), .nothingNew)
    }

    func testAPassAlreadyRunningIsJudgedByTheCounts() {
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: status(.uploading, uploaded: 3), uploadedBefore: 3), .nothingNew)
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: status(.uploading, uploaded: 4), uploadedBefore: 3), .uploaded)
    }

    func testEachPhaseThatStopsTheCheckHasItsOwnOutcome() {
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: status(.needsSignIn), uploadedBefore: 0), .needsSignIn)
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: status(.waitingForHomeNetwork), uploadedBefore: 0), .waitingForNetwork)
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: status(.photosAccessNeeded), uploadedBefore: 0), .failed)
        XCTAssertEqual(UploadBackgroundCheck.outcome(after: status(.error("x")), uploadedBefore: 0), .failed)
    }

    func testRunReadsTheCountBeforeRefreshingAndReportsTheRealOutcome() async {
        let calls = Calls()
        let check = UploadBackgroundCheck(
            status: { calls.add("status"); return status(.idle, uploaded: 1) },
            refresh: { calls.add("refresh"); return status(.idle, uploaded: 2) }
        )
        let outcome = await check.run()
        XCTAssertEqual(outcome, .uploaded)
        XCTAssertEqual(calls.order, ["status", "refresh"])
    }

    func testOutcomesMapToWhatIosIsTold() {
        let table: [(BackgroundOutcome, Bool, SilentPush.Fetch)] = [
            (.uploaded, true, .newData), (.nothingNew, true, .noData), (.needsSignIn, true, .noData),
            (.waitingForNetwork, false, .failed), (.failed, false, .failed),
        ]
        for (outcome, completes, fetch) in table {
            XCTAssertEqual(outcome.completesTask, completes, "\(outcome)")
            XCTAssertEqual(outcome.fetchResult, fetch, "\(outcome)")
        }
    }
}
