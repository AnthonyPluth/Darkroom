import XCTest
@testable import DarkroomCore

private final class FakeStorage: SessionStorage, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [SessionItem: String] = [:]
    private var failing: [String: Set<SessionItem>] = [:]

    func fail(_ operation: String, on item: SessionItem) {
        lock.lock()
        failing[operation, default: []].insert(item)
        lock.unlock()
    }

    func value(_ item: SessionItem) -> String? {
        lock.lock()
        defer { lock.unlock() }
        return items[item]
    }

    func seed(_ value: String, as item: SessionItem) {
        lock.lock()
        items[item] = value
        lock.unlock()
    }

    func load(_ item: SessionItem) throws -> String? {
        lock.lock()
        defer { lock.unlock() }
        if failing["load"]?.contains(item) == true { throw KeychainFailure() }
        return items[item]
    }

    func save(_ value: String, as item: SessionItem) throws {
        lock.lock()
        defer { lock.unlock() }
        if failing["save"]?.contains(item) == true { throw KeychainFailure() }
        items[item] = value
    }

    func delete(_ item: SessionItem) throws {
        lock.lock()
        defer { lock.unlock() }
        if failing["delete"]?.contains(item) == true { throw KeychainFailure() }
        items[item] = nil
    }
}

private struct KeychainFailure: Error {}

final class DeviceSessionTests: XCTestCase {
    private let host = "https://darkroom.example.net"
    private let address = try! ServerAddress(parsing: "https://darkroom.example.net")
    private let callback = URL(string: "darkroom://signed-in#token=abc123")!

    private func signedInSession(_ storage: FakeStorage) async throws -> DeviceSession {
        let session = DeviceSession(storage: storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        try await session.complete(callback: callback)
        return session
    }

    func testAFreshInstallIsSignedOut() async throws {
        let session = DeviceSession(storage: FakeStorage())
        let phase = try await session.restore()
        XCTAssertEqual(phase, .signedOut)
        let token = await session.currentToken
        XCTAssertNil(token)
    }

    func testBeginningReturnsTheStartURLAndRemembersTheServer() async throws {
        let session = DeviceSession(storage: FakeStorage())
        let start = try await session.begin(serverText: "darkroom.example.net", deviceName: "Sam's iPhone")
        XCTAssertEqual(start.absoluteString, "https://darkroom.example.net/auth/device/start?name=Sam%27s%20iPhone")
        let phase = await session.phase
        let saved = await session.savedServer
        XCTAssertEqual(phase, .signingIn(address))
        XCTAssertEqual(saved, address)
    }

    func testABadServerAddressDoesNotStartASignIn() async throws {
        let session = DeviceSession(storage: FakeStorage())
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
        let session = DeviceSession(storage: storage)
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
            try await session.complete(callback: URL(string: "darkroom://signed-in#token=forged")!)
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

    func testACallbackWithoutATokenEndsTheAttemptAndStoresNothing() async throws {
        let storage = FakeStorage()
        let session = DeviceSession(storage: storage)
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        do {
            try await session.complete(callback: URL(string: "darkroom://signed-in")!)
            XCTFail("expected a refusal")
        } catch {
            XCTAssertEqual(error as? SignInError, .noToken)
        }
        let phase = await session.phase
        XCTAssertEqual(phase, .signedOut)
        XCTAssertNil(storage.value(.deviceToken))
    }

    func testCancellingEndsTheAttemptAndKeepsTheServerForTheNextTry() async throws {
        let session = DeviceSession(storage: FakeStorage())
        _ = try await session.begin(serverText: host, deviceName: "Phone")
        await session.cancel()
        let phase = await session.phase
        let saved = await session.savedServer
        XCTAssertEqual(phase, .signedOut)
        XCTAssertEqual(saved, address)
    }

    func testCancellingDoesNotSignOutASignedInDevice() async throws {
        let session = try await signedInSession(FakeStorage())
        await session.cancel()
        let phase = await session.phase
        XCTAssertEqual(phase, .signedIn(address))
    }

    func testAFailedSaveLeavesTheDeviceSignedOutWithoutAToken() async throws {
        let storage = FakeStorage()
        storage.fail("save", on: .deviceToken)
        let session = DeviceSession(storage: storage)
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
        let relaunched = DeviceSession(storage: storage)
        let phase = try await relaunched.restore()
        let token = await relaunched.currentToken
        XCTAssertEqual(phase, .signedIn(address))
        XCTAssertEqual(token, "abc123")
    }

    func testRestoringWithoutATokenKeepsTheServerPrefilled() async throws {
        let storage = FakeStorage()
        storage.seed(host, as: .serverURL)
        let session = DeviceSession(storage: storage)
        let phase = try await session.restore()
        let saved = await session.savedServer
        XCTAssertEqual(phase, .signedOut)
        XCTAssertEqual(saved, address)
    }

    func testRestoringWithATokenButNoUsableServerIsSignedOut() async throws {
        let storage = FakeStorage()
        storage.seed("abc123", as: .deviceToken)
        storage.seed("http://darkroom.example.net", as: .serverURL)
        let session = DeviceSession(storage: storage)
        let phase = try await session.restore()
        let token = await session.currentToken
        XCTAssertEqual(phase, .signedOut)
        XCTAssertNil(token)
    }

    func testAnUnreadableKeychainIsReportedNotTreatedAsSignedOut() async throws {
        let storage = FakeStorage()
        storage.fail("load", on: .deviceToken)
        let session = DeviceSession(storage: storage)
        do {
            try await session.restore()
            XCTFail("expected a failure")
        } catch {
            XCTAssertEqual(error as? SignInError, .storage)
        }
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
}
