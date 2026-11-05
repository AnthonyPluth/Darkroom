import XCTest
@testable import DarkroomCore

final class ResourceStateMachineTests: XCTestCase {
    func testTransitionTable() {
        let expected: [ResourceState: Set<ResourceState>] = [
            .hashed: [.checked, .uploaded, .failed],
            .checked: [.uploading, .hashed, .failed],
            .uploading: [.uploaded, .checked, .hashed, .failed],
            .uploaded: [],
            .failed: [.hashed],
            .notLocal: [],
        ]
        for state in ResourceState.allCases {
            XCTAssertEqual(state.next, expected[state], "\(state)")
        }
    }

    func testEveryStateMayStayWhereItIs() {
        for state in ResourceState.allCases {
            XCTAssertTrue(state.canMove(to: state))
        }
    }

    func testNothingReachesNotLocalOrSkipsTheCheck() {
        for state in ResourceState.allCases where state != .notLocal {
            XCTAssertFalse(state.canMove(to: .notLocal), "\(state)")
        }
        XCTAssertFalse(ResourceState.hashed.canMove(to: .uploading))
        XCTAssertFalse(ResourceState.failed.canMove(to: .uploaded))
    }

    func testInterruptedUploadCanBeResumedOrRehashed() {
        XCTAssertTrue(ResourceState.uploading.canMove(to: .checked))
        XCTAssertTrue(ResourceState.uploading.canMove(to: .hashed))
    }
}
