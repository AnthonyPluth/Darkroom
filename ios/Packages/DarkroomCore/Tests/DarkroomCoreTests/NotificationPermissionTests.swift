import XCTest
@testable import DarkroomCore

private final class FakeAuthorizer: NotificationAuthorizer, @unchecked Sendable {
    private let lock = NSLock()
    private var status: NotificationAuthorization
    private let grants: Bool
    private var asked = 0

    init(_ status: NotificationAuthorization, grants: Bool = true) {
        self.status = status
        self.grants = grants
    }

    var requests: Int {
        lock.lock()
        defer { lock.unlock() }
        return asked
    }

    func current() async -> NotificationAuthorization {
        lock.lock()
        defer { lock.unlock() }
        return status
    }

    func request() async -> Bool {
        lock.lock()
        defer { lock.unlock() }
        asked += 1
        status = grants ? .allowed : .denied
        return grants
    }
}

final class NotificationPermissionTests: XCTestCase {
    func testAsksOnceWhenNotAskedAndRecordsAllowed() async {
        let authorizer = FakeAuthorizer(.notAsked)
        let permission = NotificationPermission(authorizer: authorizer)
        let first = await permission.ensureAsked()
        let second = await permission.ensureAsked()
        XCTAssertEqual(first, .allowed)
        XCTAssertEqual(second, .allowed)
        XCTAssertEqual(authorizer.requests, 1)
        let state = await permission.state
        XCTAssertEqual(state, .allowed)
    }

    func testARefusalIsRecordedAndNeverAskedAgain() async {
        let authorizer = FakeAuthorizer(.notAsked, grants: false)
        let permission = NotificationPermission(authorizer: authorizer)
        let first = await permission.ensureAsked()
        let second = await permission.ensureAsked()
        XCTAssertEqual(first, .denied)
        XCTAssertEqual(second, .denied)
        XCTAssertEqual(authorizer.requests, 1)
        let state = await permission.state
        XCTAssertEqual(state, .denied)
    }

    func testAnAnswerGivenEarlierIsNeverAskedAgain() async {
        for status in [NotificationAuthorization.allowed, .denied] {
            let authorizer = FakeAuthorizer(status)
            let permission = NotificationPermission(authorizer: authorizer)
            let result = await permission.ensureAsked()
            XCTAssertEqual(result, status)
            XCTAssertEqual(authorizer.requests, 0)
        }
    }

    func testTheStateStartsNotAsked() async {
        let permission = NotificationPermission(authorizer: FakeAuthorizer(.allowed))
        let state = await permission.state
        XCTAssertEqual(state, .notAsked)
    }
}
