import XCTest
@testable import DarkroomCore

final class ReachabilityTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_000_000)

    func testProbesBeforeAnyResult() {
        XCTAssertTrue(ReachabilityPolicy().shouldProbe(at: start))
    }

    func testBackoffDoublesUpToTheCap() {
        let policy = ReachabilityPolicy(baseDelay: 30, maxDelay: 900)
        XCTAssertEqual(policy.delay(afterFailures: 1), 30)
        XCTAssertEqual(policy.delay(afterFailures: 2), 60)
        XCTAssertEqual(policy.delay(afterFailures: 5), 480)
        XCTAssertEqual(policy.delay(afterFailures: 6), 900)
        XCTAssertEqual(policy.delay(afterFailures: 500), 900)
        XCTAssertEqual(policy.delay(afterFailures: 0), 0)
    }

    func testFailureWaitsThenAllowsProbeAfterTheDelay() {
        var policy = ReachabilityPolicy(baseDelay: 30, maxDelay: 900)
        policy.recordProbe(succeeded: false, at: start)
        XCTAssertEqual(policy.status, .waiting(failures: 1))
        XCTAssertFalse(policy.shouldProbe(at: start.addingTimeInterval(29)))
        XCTAssertTrue(policy.shouldProbe(at: start.addingTimeInterval(30)))
    }

    func testRepeatedFailuresBackOffFurther() {
        var policy = ReachabilityPolicy(baseDelay: 30, maxDelay: 900)
        policy.recordProbe(succeeded: false, at: start)
        policy.recordProbe(succeeded: false, at: start.addingTimeInterval(30))
        XCTAssertEqual(policy.status, .waiting(failures: 2))
        XCTAssertFalse(policy.shouldProbe(at: start.addingTimeInterval(30 + 59)))
        XCTAssertTrue(policy.shouldProbe(at: start.addingTimeInterval(30 + 60)))
    }

    func testSuccessResetsTheBackoff() {
        var policy = ReachabilityPolicy()
        policy.recordProbe(succeeded: false, at: start)
        policy.recordProbe(succeeded: true, at: start.addingTimeInterval(40))
        XCTAssertEqual(policy.status, .reachable)
        XCTAssertTrue(policy.shouldProbe(at: start.addingTimeInterval(40)))
        policy.recordProbe(succeeded: false, at: start.addingTimeInterval(50))
        XCTAssertEqual(policy.status, .waiting(failures: 1))
    }

    func testServerProbeReportsClosureResult() async {
        let up = await ServerProbe { true }()
        let down = await ServerProbe { false }()
        XCTAssertTrue(up)
        XCTAssertFalse(down)
    }
}
