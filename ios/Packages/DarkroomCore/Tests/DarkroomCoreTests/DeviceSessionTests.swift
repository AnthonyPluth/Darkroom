import XCTest
@testable import DarkroomCore

private final class FakeStorage: SessionStorage, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [SessionItem: String] = [:]
    private var failing: Set<String> = []

    func fail(_ operation: String, on item: SessionItem) { locked { _ = failing.insert(operation + item.rawValue) } }
    func value(_ item: SessionItem) -> String? { locked { items[item] } }
    func seed(_ value: String, as item: SessionItem) { locked { items[item] = value } }

    func load(_ item: SessionItem) throws -> String? { try locked { try check("load", item); return items[item] } }
    func save(_ value: String, as item: SessionItem) throws { try locked { try check("save", item); items[item] = value } }
    func delete(_ item: SessionItem) throws { try locked { try check("delete", item); items[item] = nil } }

    private func check(_ operation: String, _ item: SessionItem) throws {
        if failing.contains(operation + item.rawValue) { throw KeychainFailure() }
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }
}

private struct KeychainFailure: Error {}

private final class StateSequence: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    func next() -> String {
        lock.lock()
        defer { lock.unlock() }
        count += 1
        return "state-\(count)"
    }
}

private final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var moment = Date(timeIntervalSince1970: 3_000_000)

    var now: Date {
        lock.lock()
        defer { lock.unlock() }
        return moment
    }

    func advance(_ seconds: TimeInterval) {
        lock.lock()
        moment = moment.addingTimeInterval(seconds)
        lock.unlock()
    }
}

final class DeviceSessionTests: XCTestCase {
    private let host = "https://darkroom.example.net"
    private let address = try! ServerAddress(parsing: "https://darkroom.example.net")
    private let callback = URL(string: "darkroom://signed-in#token=abc123&state=state-1")!
    private let clock = TestClock()

    private func makeSession(_ storage: FakeStorage) -> DeviceSession {
        let states = StateSequence()
        let clock = clock
        return DeviceSession(storage: storage, makeState: { states.next() }, clock: { clock.now })
    }

    private func signedInSession(_ storage: FakeStorage) async throws -> DeviceSession {
        let session = makeSession(storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        try await session.complete(callback: callback)
        return session
    }

    func testABadServerAddressDoesNotStartASignIn() async throws {
        let session = makeSession(FakeStorage())
        do {
            _ = try await session.begin(serverText: "http://darkroom.example.net", deviceName: "Phone")
            XCTFail("expected a refusal")
        } catch {
            XCTAssertEqual(error as? SignInError, .server(.notHTTPS))
        }
        let phase = await session.phase
        XCTAssertEqual(phase, .signedOut)
    }

    func testCompletingStoresTheTokenAndTheServer() async throws {
        let storage = FakeStorage()
        let session = try await signedInSession(storage)
        let phase = await session.phase
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedIn(address))
        XCTAssertEqual(token, "abc123")
        XCTAssertEqual(storage.value(.deviceToken), "abc123")
        XCTAssertEqual(storage.value(.serverURL), host)
    }

    func testTheTokenProviderReadsTheCurrentToken() async throws {
        let session = try await signedInSession(FakeStorage())
        let provider = session.tokenProvider()
        let before = await provider()
        XCTAssertEqual(before, "abc123")
        try await session.signOut()
        let after = await provider()
        XCTAssertNil(after)
    }

    func testACallbackOutsideASignInIsIgnored() async throws {
        let storage = FakeStorage()
        let session = makeSession(storage)
        do {
            try await session.complete(callback: callback)
            XCTFail("expected a refusal")
        } catch {
            XCTAssertEqual(error as? SignInError, .notSigningIn)
        }
        let phase = await session.phase
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedOut)
        XCTAssertNil(token)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testACallbackCannotReplaceTheTokenOfASignedInDevice() async throws {
        let storage = FakeStorage()
        let session = try await signedInSession(storage)
        do {
            try await session.complete(callback: URL(string: "darkroom://signed-in#token=forged&state=state-1")!)
            XCTFail("expected a refusal")
        } catch {
            XCTAssertEqual(error as? SignInError, .notSigningIn)
        }
        let token = await session.currentToken
        XCTAssertEqual(token, "abc123")
        XCTAssertEqual(storage.value(.deviceToken), "abc123")
    }

    func testASignedInDeviceCannotBeginAgain() async throws {
        let session = try await signedInSession(FakeStorage())
        do {
            _ = try await session.begin(serverText: host, deviceName: "Phone")
            XCTFail("expected a refusal")
        } catch {
            XCTAssertEqual(error as? SignInError, .alreadySignedIn)
        }
        let token = await session.currentToken
        XCTAssertEqual(token, "abc123")
    }

    func testCancellingEndsTheAttemptAndKeepsTheServerForTheNextTry() async throws {
        let session = makeSession(FakeStorage())
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        await session.cancel()
        let phase = await session.phase
        let saved = await session.savedServer
        XCTAssertEqual(phase, .signedOut)
        XCTAssertEqual(saved, address)
    }

    func testAFailedSaveLeavesTheDeviceSignedOutWithoutAToken() async throws {
        let storage = FakeStorage()
        storage.fail("save", on: .deviceToken)
        let session = makeSession(storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        do {
            try await session.complete(callback: callback)
            XCTFail("expected a failure")
        } catch {
            XCTAssertEqual(error as? SignInError, .storage)
        }
        let phase = await session.phase
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedOut)
        XCTAssertNil(token)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testRestoringBringsBackTheSignedInDevice() async throws {
        let storage = FakeStorage()
        _ = try await signedInSession(storage)
        let relaunched = makeSession(storage)
        let phase = try await relaunched.restore()
        let token = await relaunched.currentToken
        XCTAssertEqual(phase, .signedIn(address))
        XCTAssertEqual(token, "abc123")
    }

    func testSigningOutClearsTheTokenAndKeepsTheServer() async throws {
        let storage = FakeStorage()
        let session = try await signedInSession(storage)
        try await session.signOut()
        let phase = await session.phase
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedOut)
        XCTAssertNil(token)
        XCTAssertNil(storage.value(.deviceToken))
        XCTAssertEqual(storage.value(.serverURL), host)
    }

    func testAFailedSignOutKeepsTheDeviceSignedIn() async throws {
        let storage = FakeStorage()
        let session = try await signedInSession(storage)
        storage.fail("delete", on: .deviceToken)
        do {
            try await session.signOut()
            XCTFail("expected a failure")
        } catch {
            XCTAssertEqual(error as? SignInError, .storage)
        }
        let phase = await session.phase
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedIn(address))
        XCTAssertEqual(token, "abc123")
        XCTAssertEqual(storage.value(.deviceToken), "abc123")
    }

    func testARejectedTokenSignsTheDeviceOut() async throws {
        let storage = FakeStorage()
        let session = try await signedInSession(storage)
        try await session.tokenRejected("abc123")
        let phase = await session.phase
        XCTAssertEqual(phase, .signedOut)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testAnOldTokenBeingRejectedDoesNotSignOutANewSignIn() async throws {
        let storage = FakeStorage()
        let session = try await signedInSession(storage)
        try await session.tokenRejected("an-older-token")
        let phase = await session.phase
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedIn(address))
        XCTAssertEqual(token, "abc123")
        XCTAssertEqual(storage.value(.deviceToken), "abc123")
    }

    func testTheStartURLCarriesTheAttemptsState() async throws {
        let session = makeSession(FakeStorage())
        let start = try await session.begin(serverText: host, deviceName: "Phone")
        XCTAssertEqual(start.query, "name=Phone&state=state-1")
    }

    private func assertRefused(_ session: DeviceSession, _ text: String, as expected: SignInError, line: UInt = #line) async {
        do {
            try await session.complete(callback: URL(string: text)!)
            XCTFail("expected a refusal", line: line)
        } catch {
            XCTAssertEqual(error as? SignInError, expected, line: line)
        }
    }

    func testAWrongOrMissingStateInstallsNothingAndEndsTheAttempt() async throws {
        for text in ["darkroom://signed-in#token=abc123&state=state-2", "darkroom://signed-in#token=abc123&state=state-", "darkroom://signed-in#token=abc123"] {
            let storage = FakeStorage()
            let session = makeSession(storage)
            _ = try await session.begin(serverText: host, deviceName: "Phone")
            await assertRefused(session, text, as: text.hasSuffix("abc123") ? .noToken : .stateMismatch)
            let phase = await session.phase
            let token = await session.currentToken
            XCTAssertEqual(phase, .signedOut, text)
            XCTAssertNil(token, text)
            XCTAssertNil(storage.value(.deviceToken), text)
            await assertRefused(session, callback.absoluteString, as: .notSigningIn)
        }
    }

    func testTheStateWorksOnlyOnce() async throws {
        let storage = FakeStorage()
        let session = makeSession(storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        try await session.complete(callback: callback)
        try await session.signOut()
        await assertRefused(session, callback.absoluteString, as: .notSigningIn)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testACancelledAttemptsStateIsDead() async throws {
        let session = makeSession(FakeStorage())
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        await session.cancel()
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        await assertRefused(session, callback.absoluteString, as: .stateMismatch)
    }

    func testAnEarlierAttemptsCallbackIsRefusedByALaterOne() async throws {
        let storage = FakeStorage()
        let session = makeSession(storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        await assertRefused(session, callback.absoluteString, as: .stateMismatch)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testACallbackAfterTheAttemptTimedOutIsRefused() async throws {
        let storage = FakeStorage()
        let session = makeSession(storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        clock.advance(DeviceSession.attemptLifetime + 1)
        await assertRefused(session, callback.absoluteString, as: .expired)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testARefusalWithTheAttemptsStateNamesTheReasonAndInstallsNothing() async throws {
        let cases: [(String, SignInError)] = [
            ("darkroom://signed-in#error=no_person&state=state-1", .noPerson),
            ("darkroom://signed-in#error=not_allowed&state=state-1", .notAllowed),
        ]
        for (text, expected) in cases {
            let storage = FakeStorage()
            let session = makeSession(storage)
            _ = try await session.begin(serverText: host, deviceName: "Phone")
            await assertRefused(session, text, as: expected)
            let phase = await session.phase
            let token = await session.currentToken
            XCTAssertEqual(phase, .signedOut, text)
            XCTAssertNil(token, text)
            XCTAssertNil(storage.value(.deviceToken), text)
            await assertRefused(session, text, as: .notSigningIn)
        }
    }

    func testARefusalWithAnotherStateIsIgnoredAsAMismatch() async throws {
        let session = makeSession(FakeStorage())
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        await assertRefused(session, "darkroom://signed-in#error=no_person&state=state-2", as: .stateMismatch)
        let phase = await session.phase
        XCTAssertEqual(phase, .signedOut)
    }

    func testARefusalAfterTheAttemptTimedOutIsExpired() async throws {
        let session = makeSession(FakeStorage())
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        clock.advance(DeviceSession.attemptLifetime + 1)
        await assertRefused(session, "darkroom://signed-in#error=not_allowed&state=state-1", as: .expired)
    }
}
