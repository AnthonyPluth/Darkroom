import XCTest
@testable import DarkroomCore

final class UploadsPresentationTests: XCTestCase {
    private func status(
        _ phase: UploadStatus.Phase = .idle,
        _ counts: [ResourceState: Int] = [:],
        failures: [FailedResource] = [],
        firstScan: Bool = true
    ) -> UploadStatus {
        UploadStatus(phase: phase, counts: counts, failures: failures, lastSync: nil, firstScanFinished: firstScan)
    }

    private func failure(_ name: String, canRetry: Bool = false) -> FailedResource {
        FailedResource(key: ResourceKey(assetID: name, kind: .photo), filename: name, reason: "why", recoverable: canRetry, canRetry: canRetry)
    }

    func testUpToDateWithAndWithoutICloudOnlyFiles() {
        let plain = UploadsPresentation.card(for: status(.idle, [.uploaded: 5]))
        XCTAssertEqual(plain.chip, "Up to date")
        XCTAssertEqual(plain.tone, .ok)
        XCTAssertEqual(plain.sentence, "All photos stored on this iPhone are on Darkroom.")
        let cloud = UploadsPresentation.card(for: status(.idle, [.uploaded: 5, .notLocal: 2]))
        XCTAssertEqual(cloud.sentence, "Everything stored on this iPhone is on Darkroom.")
        XCTAssertEqual(cloud.detail, "2 files are only in iCloud, so Darkroom can't upload them from this iPhone.")
        let one = UploadsPresentation.card(for: status(.idle, [.notLocal: 1]))
        XCTAssertEqual(one.detail, "1 file is only in iCloud, so Darkroom can't upload it from this iPhone.")
    }

    func testAnEmptyLibraryAfterTheFirstScanIsUpToDateAndBeforeItIsGettingReady() {
        XCTAssertEqual(UploadsPresentation.card(for: status()).title, "Up to date")
        let starting = UploadsPresentation.card(for: status(firstScan: false))
        XCTAssertEqual(starting.title, "Getting ready")
        XCTAssertEqual(starting.chip, "Starting")
    }

    func testWaitingCountsHashedCheckedAndUploading() {
        let card = UploadsPresentation.card(for: status(.idle, [.hashed: 5, .checked: 4, .uploading: 3, .uploaded: 9]))
        XCTAssertEqual(card.chip, "Waiting")
        XCTAssertEqual(card.sentence, "12 files are waiting to upload.")
        XCTAssertEqual(UploadsPresentation.card(for: status(.idle, [.hashed: 1])).sentence, "1 file is waiting to upload.")
    }

    func testSyncingShowsProgressAndTheFilesLeft() {
        let card = UploadsPresentation.card(for: status(.uploading, [.hashed: 12]))
        XCTAssertEqual(card.sentence, "Uploading. 12 files left.")
        XCTAssertTrue(card.showsProgress)
        XCTAssertEqual(card.chip, "Syncing")
    }

    func testWaitingForTheHomeNetworkIsWarnNotDanger() {
        let card = UploadsPresentation.card(for: status(.waitingForHomeNetwork, [.hashed: 12]))
        XCTAssertEqual(card.tone, .warn)
        XCTAssertEqual(card.sentence, "Waiting for your home network. 12 files are waiting.")
        XCTAssertEqual(card.action, .none)
    }

    func testSignInAccessAndStoppedStatesCarryTheirButtons() {
        XCTAssertEqual(UploadsPresentation.card(for: status(.needsSignIn)).action, .signInAgain)
        XCTAssertEqual(UploadsPresentation.card(for: status(.photosAccessNeeded)).action, .openSettings)
        let stopped = UploadsPresentation.card(for: status(.error(UploadStatusTracker.syncStopped)))
        XCTAssertEqual(stopped.action, .tryAgain)
        XCTAssertEqual(stopped.detail, "The last sync stopped unexpectedly. Try again.")
        XCTAssertEqual(stopped.tone, .danger)
    }

    func testNeedsAttentionOnlyWhenNothingIsWaitingAndOffersTryAllOnlyForRetryableFiles() {
        let rows = [failure("a"), failure("b")]
        let plain = UploadsPresentation.card(for: status(.idle, [.failed: 2], failures: rows))
        XCTAssertEqual(plain.title, "2 files need attention")
        XCTAssertEqual(plain.action, .none)
        let retryable = UploadsPresentation.card(for: status(.idle, [.failed: 2], failures: [failure("a", canRetry: true), failure("b")]))
        XCTAssertEqual(retryable.action, .tryAllAgain)
        let busy = UploadsPresentation.card(for: status(.idle, [.failed: 2, .hashed: 1], failures: rows))
        XCTAssertEqual(busy.title, "Waiting to upload")
        XCTAssertEqual(UploadsPresentation.card(for: status(.idle, [.failed: 1], failures: [failure("a")])).sentence, "1 file needs attention.")
    }

    func testTheFourTilesDoNotOverlap() {
        let tiles = UploadsPresentation.tiles(for: status(.idle, [.uploaded: 10, .hashed: 1, .checked: 2, .uploading: 3, .notLocal: 4, .failed: 5]))
        XCTAssertEqual(tiles.map(\.count), [10, 6, 4, 5])
        XCTAssertEqual(tiles.map(\.label), ["On Darkroom", "Waiting to upload", "Only in iCloud", "Needs attention"])
        XCTAssertEqual(tiles[1].note, "3 sending now")
        XCTAssertNil(UploadsPresentation.tiles(for: status())[1].note)
    }

    func testLastSyncedWording() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        func text(_ ago: TimeInterval) -> String {
            UploadsPresentation.lastSynced(now.addingTimeInterval(-ago), now: now, calendar: calendar)
        }
        XCTAssertEqual(UploadsPresentation.lastSynced(nil, now: now, calendar: calendar), "Not yet")
        XCTAssertEqual(text(10), "Just now")
        XCTAssertEqual(text(60), "1 minute ago")
        XCTAssertEqual(text(12 * 60), "12 minutes ago")
        XCTAssertEqual(text(3 * 3600), "3 hours ago")
        XCTAssertEqual(text(30 * 3600), "Yesterday 16:13")
        XCTAssertEqual(text(5 * 86400), "9 Nov 22:13")
    }

    func testRowsAreLimitedOnlyWhenAskedTo() {
        let all = (0..<8).map { failure("f\($0)") }
        XCTAssertEqual(UploadsPresentation.rows(all, limit: 5).count, 5)
        XCTAssertEqual(UploadsPresentation.rows(all, limit: nil).count, 8)
    }

    func testTheServerRowKeepsANonDefaultPort() throws {
        XCTAssertEqual(try ServerAddress(parsing: "darkroom.example.com").authority, "darkroom.example.com")
        XCTAssertEqual(try ServerAddress(parsing: "https://darkroom.example.com:8443/").authority, "darkroom.example.com:8443")
    }

    func testTheFirstScanShowsGettingReadyEvenWhileTheSyncRuns() {
        let card = UploadsPresentation.card(for: status(.uploading, [:], firstScan: false))
        XCTAssertEqual(card.title, "Getting ready")
        XCTAssertTrue(card.showsProgress)
        XCTAssertEqual(UploadsPresentation.card(for: status(.uploading, [.hashed: 3], firstScan: true)).title, "Syncing")
        XCTAssertEqual(UploadsPresentation.card(for: status(.needsSignIn, [:], firstScan: false)).title, "Sign-in needed")
        XCTAssertEqual(UploadsPresentation.card(for: status(.photosAccessNeeded, [:], firstScan: false)).title, "Photos access needed")
    }

    func testYesterdayFollowsTheCalendarDay() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: 1_700_000_000 + 3 * 3600)
        let yesterday = Date(timeIntervalSince1970: 1_700_000_000 - 22 * 3600)
        XCTAssertEqual(UploadsPresentation.lastSynced(yesterday, now: now, calendar: calendar), "Yesterday 00:13")
    }

    func testOnlyAChangeOfStateIsAnnounced() {
        let syncing = status(.uploading, [.hashed: 12])
        let fewer = status(.uploading, [.hashed: 5])
        let done = status(.idle, [.uploaded: 12])
        XCTAssertNil(UploadsPresentation.announcement(from: nil, to: syncing))
        XCTAssertNil(UploadsPresentation.announcement(from: syncing, to: fewer))
        XCTAssertEqual(UploadsPresentation.announcement(from: fewer, to: done), "Up to date")
        XCTAssertEqual(UploadsPresentation.announcement(from: done, to: status(.needsSignIn)), "Sign-in needed")
        XCTAssertEqual(UploadsPresentation.announcement(from: done, to: status(.waitingForHomeNetwork, [.hashed: 2])), "Waiting for home network")
        let one = status(.idle, [.failed: 1], failures: [failure("a")])
        let two = status(.idle, [.failed: 2], failures: [failure("a"), failure("b")])
        XCTAssertNil(UploadsPresentation.announcement(from: one, to: two))
    }
}
