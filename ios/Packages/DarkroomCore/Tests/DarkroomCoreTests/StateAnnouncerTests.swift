import XCTest
@testable import DarkroomCore

final class StateAnnouncerTests: XCTestCase {
    private func card(_ phase: UploadStatus.Phase, _ counts: [ResourceState: Int] = [:]) -> UploadsCard {
        UploadsPresentation.card(
            for: UploadStatus(phase: phase, counts: counts, failures: [], lastSync: nil, firstScanFinished: true)
        )
    }

    func testTheFirstCardIsNotAnnounced() {
        var announcer = StateAnnouncer()
        XCTAssertNil(announcer.announcement(for: card(.idle, [.uploaded: 1])))
    }

    func testAChangeOfStateAnnouncesTheNewCardsTitle() {
        var announcer = StateAnnouncer()
        _ = announcer.announcement(for: card(.uploading, [.uploading: 1]))
        XCTAssertEqual(announcer.announcement(for: card(.idle, [.uploaded: 1])), "Up to date")
        XCTAssertEqual(announcer.announcement(for: card(.waitingForHomeNetwork, [.hashed: 2])), "Waiting for home network")
        XCTAssertEqual(announcer.announcement(for: card(.needsSignIn)), "Sign-in needed")
    }

    func testCountsChangingWithinTheSameStateAreNotAnnounced() {
        var announcer = StateAnnouncer()
        _ = announcer.announcement(for: card(.uploading, [.hashed: 12]))
        XCTAssertNil(announcer.announcement(for: card(.uploading, [.hashed: 9])))
        XCTAssertNil(announcer.announcement(for: card(.uploading, [.hashed: 3])))
    }

    func testTheSameStateTwiceInARowIsAnnouncedOnlyOnce() {
        var announcer = StateAnnouncer()
        _ = announcer.announcement(for: card(.uploading, [.hashed: 1]))
        XCTAssertEqual(announcer.announcement(for: card(.idle, [.uploaded: 1])), "Up to date")
        XCTAssertNil(announcer.announcement(for: card(.idle, [.uploaded: 2])))
    }
}
