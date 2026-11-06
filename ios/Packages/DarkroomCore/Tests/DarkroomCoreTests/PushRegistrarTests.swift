import XCTest
@testable import DarkroomCore

private final class FakePushAPI: PushTokenAPI, @unchecked Sendable {
    private let lock = NSLock()
    private var sent: [String] = []
    private var forgets = 0
    private var registerError: Error?
    private var forgetError: Error?

    var registered: [String] { locked { sent } }
    var forgetCount: Int { locked { forgets } }
    func failRegister(with error: Error?) { locked { registerError = error } }
    func failForget(with error: Error?) { locked { forgetError = error } }

    func registerPushToken(_ token: String) async throws {
        try locked {
            sent.append(token)
            if let registerError { throw registerError }
        }
    }

    func forgetPushToken() async throws {
        try locked {
            forgets += 1
            if let forgetError { throw forgetError }
        }
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }
}

private final class GatedPushAPI: PushTokenAPI, @unchecked Sendable {
    private let lock = NSLock()
    private var events: [String] = []
    private var release: CheckedContinuation<Void, Never>?
    private var released = false

    var log: [String] { locked { events } }

    func open() {
        let waiting: CheckedContinuation<Void, Never>? = locked {
            released = true
            let held = release
            release = nil
            return held
        }
        waiting?.resume()
    }

    func registerPushToken(_ token: String) async throws {
        locked { events.append("register started") }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let proceed: Bool = locked {
                if released { return true }
                release = continuation
                return false
            }
            if proceed { continuation.resume() }
        }
        locked { events.append("register finished") }
    }

    func forgetPushToken() async throws {
        locked { events.append("forget") }
    }

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }
}

private func waitUntil(_ condition: () -> Bool) async {
    for _ in 0..<10_000 {
        if condition() { return }
        await Task.yield()
    }
}

private final class EndedCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    var value: Int {
        lock.lock()
        defer { lock.unlock() }
        return count
    }

    func hit() {
        lock.lock()
        count += 1
        lock.unlock()
    }
}

private actor Gate {
    private var isOpen = false
    private var waiting: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if isOpen { return }
        await withCheckedContinuation { waiting.append($0) }
    }

    func open() {
        isOpen = true
        let released = waiting
        waiting = []
        for continuation in released { continuation.resume() }
    }
}

private actor Log {
    private(set) var events: [String] = []
    func add(_ event: String) { events.append(event) }

    func waitFor(_ event: String) async {
        while !events.contains(event) { await Task.yield() }
    }
}

private struct SlowPushAPI: PushTokenAPI {
    let log: Log
    let gate: Gate

    func registerPushToken(_ token: String) async throws {
        await log.add("put started")
        await gate.wait()
        await log.add("put finished")
    }

    func forgetPushToken() async throws {
        await log.add("delete")
    }
}

private let tokenA = String(repeating: "ab", count: 32)
private let tokenB = String(repeating: "cd", count: 32)

final class PushRegistrarTests: XCTestCase {
    func testHexIsLowercaseAndPadded() {
        XCTAssertEqual(PushToken.hex(Data([0x00, 0x0f, 0xa0, 0xff])), "000fa0ff")
    }

    func testOnlyHexOfTheServersLengthIsValid() {
        XCTAssertTrue(PushToken.isValid(tokenA))
        XCTAssertTrue(PushToken.isValid(tokenA.uppercased()))
        XCTAssertFalse(PushToken.isValid(String(repeating: "a", count: 63)))
        XCTAssertFalse(PushToken.isValid(String(repeating: "a", count: 201)))
        XCTAssertFalse(PushToken.isValid(String(repeating: "g", count: 64)))
        XCTAssertFalse(PushToken.isValid(String(repeating: "\u{FF21}", count: 64)))
    }

    func testWaitsWithoutATokenOrWithoutASignedInDevice() async {
        let api = FakePushAPI()
        let signedIn = PushRegistrar(api: { api })
        let first = await signedIn.sync()
        XCTAssertEqual(first, .waiting)
        let signedOut = PushRegistrar(api: { nil })
        await signedOut.tokenReceived(tokenA)
        let second = await signedOut.sync()
        XCTAssertEqual(second, .waiting)
        XCTAssertEqual(api.registered, [])
    }

    func testIgnoresATokenThatIsNotHex() async {
        let api = FakePushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived("not a token")
        let current = await registrar.currentToken
        XCTAssertNil(current)
        let outcome = await registrar.sync()
        XCTAssertEqual(outcome, .waiting)
    }

    func testSendsALowercaseTokenOnceAndAgainWhenItChanges() async {
        let api = FakePushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA.uppercased())
        let first = await registrar.sync()
        let again = await registrar.sync()
        XCTAssertEqual(first, .registered)
        XCTAssertEqual(again, .registered)
        XCTAssertEqual(api.registered, [tokenA])
        await registrar.tokenReceived(tokenB)
        await registrar.sync()
        XCTAssertEqual(api.registered, [tokenA, tokenB])
    }

    func testANewDeviceSessionSendsTheTokenAgain() async {
        let api = FakePushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        await registrar.sync()
        await registrar.deviceSessionChanged()
        await registrar.sync()
        XCTAssertEqual(api.registered, [tokenA, tokenA])
    }

    func testRefusalsAreOutcomesAndAFailureIsRetried() async {
        let cases: [(APIError, PushRegistrar.Outcome)] = [
            (.unauthorized, .needsSignIn),
            (.revoked, .needsSignIn),
            (.notSignedIn, .needsSignIn),
            (.rejected(status: 409), .heldByAnother),
            (.rejected(status: 400), .rejected),
            (.server(status: 503), .retryLater),
            (.transport("-1009"), .retryLater),
            (.decoding, .retryLater),
        ]
        for (error, expected) in cases {
            let api = FakePushAPI()
            api.failRegister(with: error)
            let registrar = PushRegistrar(api: { api })
            await registrar.tokenReceived(tokenA)
            let failed = await registrar.sync()
            XCTAssertEqual(failed, expected)
            api.failRegister(with: nil)
            let retried = await registrar.sync()
            XCTAssertEqual(retried, .registered)
            XCTAssertEqual(api.registered.count, 2)
        }
    }

    func testOnlyAnEndedSignInIsReportedToTheSession() async {
        let cases: [(APIError, Int)] = [
            (.unauthorized, 1),
            (.revoked, 1),
            (.notSignedIn, 1),
            (.rejected(status: 409), 0),
            (.rejected(status: 400), 0),
            (.server(status: 503), 0),
            (.transport("-1009"), 0),
        ]
        for (error, expected) in cases {
            let api = FakePushAPI()
            api.failRegister(with: error)
            let ended = EndedCounter()
            let registrar = PushRegistrar(api: { api }, signInEnded: { ended.hit() })
            await registrar.tokenReceived(tokenA)
            await registrar.sync()
            XCTAssertEqual(ended.value, expected, "\(error)")
        }
    }

    func testASyncThatSucceedsOrWaitsReportsNothing() async {
        let api = FakePushAPI()
        let ended = EndedCounter()
        let signedOut = PushRegistrar(api: { nil }, signInEnded: { ended.hit() })
        await signedOut.tokenReceived(tokenA)
        await signedOut.sync()
        let registrar = PushRegistrar(api: { api }, signInEnded: { ended.hit() })
        await registrar.sync()
        await registrar.tokenReceived(tokenA)
        await registrar.sync()
        XCTAssertEqual(ended.value, 0)
    }

    func testAnUnknownErrorIsRetriedLater() async {
        struct Odd: Error {}
        let api = FakePushAPI()
        api.failRegister(with: Odd())
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        let outcome = await registrar.sync()
        XCTAssertEqual(outcome, .retryLater)
    }

    func testForgetAsksDarkroomAndTheTokenIsSentAgainAfterwards() async {
        let api = FakePushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        await registrar.sync()
        let outcome = await registrar.forget()
        XCTAssertEqual(outcome, .forgotten)
        XCTAssertEqual(api.forgetCount, 1)
        await registrar.deviceSessionChanged()
        await registrar.sync()
        XCTAssertEqual(api.registered, [tokenA, tokenA])
    }

    func testForgetWaitsForARegistrationThatIsStillInFlight() async {
        let api = GatedPushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        let syncing = Task { await registrar.sync() }
        await waitUntil { api.log == ["register started"] }
        let forgetting = Task { await registrar.forget() }
        for _ in 0..<200 { await Task.yield() }
        XCTAssertEqual(api.log, ["register started"])
        api.open()
        _ = await syncing.value
        let outcome = await forgetting.value
        XCTAssertEqual(outcome, .forgotten)
        XCTAssertEqual(api.log, ["register started", "register finished", "forget"])
    }

    func testNothingIsRegisteredAfterForgetUntilANewDeviceSession() async {
        let api = FakePushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        _ = await registrar.forget()
        let waiting = await registrar.sync()
        XCTAssertEqual(waiting, .waiting)
        XCTAssertEqual(api.registered, [])
        await registrar.deviceSessionChanged()
        let registered = await registrar.sync()
        XCTAssertEqual(registered, .registered)
        XCTAssertEqual(api.registered, [tokenA])
    }

    func testAFailedForgetLeavesRegistrationRunning() async {
        let api = FakePushAPI()
        api.failForget(with: APIError.transport("-1009"))
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        let outcome = await registrar.forget()
        XCTAssertEqual(outcome, .failed)
        await registrar.sync()
        XCTAssertEqual(api.registered, [tokenA])
    }

    func testTwoSyncsAtOnceSendTheTokenOnce() async {
        let api = GatedPushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        let first = Task { await registrar.sync() }
        await waitUntil { api.log == ["register started"] }
        let second = Task { await registrar.sync() }
        for _ in 0..<200 { await Task.yield() }
        api.open()
        let outcomes = [await first.value, await second.value]
        XCTAssertEqual(outcomes, [.registered, .registered])
        XCTAssertEqual(api.log, ["register started", "register finished"])
    }

    func testForgetNeedsNothingWhenTheSignInHasAlreadyEnded() async {
        for error in [APIError.unauthorized, .revoked, .notSignedIn] {
            let api = FakePushAPI()
            api.failForget(with: error)
            let registrar = PushRegistrar(api: { api })
            let outcome = await registrar.forget()
            XCTAssertEqual(outcome, .alreadyEnded)
        }
        let signedOut = PushRegistrar(api: { nil })
        let outcome = await signedOut.forget()
        XCTAssertEqual(outcome, .alreadyEnded)
    }

    func testForgetFailsWhenTheSignInCannotBeReadAndIsNotTakenForEnded() async {
        struct Unreadable: Error {}
        let registrar = PushRegistrar(api: { throw Unreadable() })
        let outcome = await registrar.forget()
        XCTAssertEqual(outcome, .failed)
        await registrar.tokenReceived(tokenA)
        let synced = await registrar.sync()
        XCTAssertEqual(synced, .retryLater)
    }

    func testForgetFailsWhenDarkroomCannotBeReached() async {
        for error in [APIError.transport("-1009"), .server(status: 500), .rejected(status: 400)] {
            let api = FakePushAPI()
            api.failForget(with: error)
            let registrar = PushRegistrar(api: { api })
            let outcome = await registrar.forget()
            XCTAssertEqual(outcome, .failed)
        }
    }

    func testForgetWaitsForARegistrationThatIsAlreadyBeingSent() async {
        let log = Log()
        let gate = Gate()
        let api = SlowPushAPI(log: log, gate: gate)
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        let syncing = Task { await registrar.sync() }
        await log.waitFor("put started")
        let forgetting = Task { await registrar.forget() }
        for _ in 0..<50 { await Task.yield() }
        let beforeRelease = await log.events
        XCTAssertEqual(beforeRelease, ["put started"])
        await gate.open()
        _ = await syncing.value
        let outcome = await forgetting.value
        XCTAssertEqual(outcome, .forgotten)
        let events = await log.events
        XCTAssertEqual(events, ["put started", "put finished", "delete"])
    }

    func testARegistrationNotYetSentWhenForgetStartsIsNeverSent() async {
        let log = Log()
        let gate = Gate()
        let opened = Log()
        let api = SlowPushAPI(log: log, gate: Gate())
        let registrar = PushRegistrar(api: {
            await opened.add("asked")
            await gate.wait()
            return api
        })
        await registrar.tokenReceived(tokenA)
        let syncing = Task { await registrar.sync() }
        await opened.waitFor("asked")
        let forgetting = Task { await registrar.forget() }
        for _ in 0..<50 { await Task.yield() }
        await gate.open()
        _ = await syncing.value
        _ = await forgetting.value
        let events = await log.events
        XCTAssertEqual(events, ["delete"])
    }

    func testASyncAfterForgetStartsWaitsInsteadOfSendingTheToken() async {
        let log = Log()
        let gate = Gate()
        let api = SlowPushAPI(log: log, gate: Gate())
        let registrar = PushRegistrar(api: {
            await gate.wait()
            return api
        })
        await registrar.tokenReceived(tokenA)
        let forgetting = Task { await registrar.forget() }
        for _ in 0..<50 { await Task.yield() }
        let outcome = await registrar.sync()
        XCTAssertEqual(outcome, .waiting)
        await gate.open()
        _ = await forgetting.value
        let events = await log.events
        XCTAssertEqual(events, ["delete"])
    }
}

private actor Steps {
    private(set) var order: [String] = []
    func add(_ step: String) { order.append(step) }
}

final class SignOutFlowTests: XCTestCase {
    private func flow(_ api: FakePushAPI, steps: Steps, clearing: @escaping @Sendable () async throws -> Void = {}) -> SignOutFlow {
        SignOutFlow(
            push: PushRegistrar(api: { api }),
            stopUploads: { await steps.add("stop") },
            clearSession: {
                await steps.add("clear")
                try await clearing()
            }
        )
    }

    func testForgetsThePushTokenBeforeStoppingUploadsAndClearingTheSession() async {
        let api = FakePushAPI()
        let steps = Steps()
        let outcome = await flow(api, steps: steps).signOut()
        XCTAssertEqual(outcome, .signedOut(pushForgotten: true))
        XCTAssertEqual(api.forgetCount, 1)
        let order = await steps.order
        XCTAssertEqual(order, ["stop", "clear"])
    }

    func testStaysSignedInWhenTheTokenCannotBeForgotten() async {
        let api = FakePushAPI()
        api.failForget(with: APIError.transport("-1009"))
        let steps = Steps()
        let outcome = await flow(api, steps: steps).signOut()
        XCTAssertEqual(outcome, .pushNotForgotten)
        let order = await steps.order
        XCTAssertEqual(order, [])
    }

    func testAnUnreadableSignInKeepsThePersonSignedInAndSendsNoDelete() async {
        struct Unreadable: Error {}
        let steps = Steps()
        let flow = SignOutFlow(
            push: PushRegistrar(api: { throw Unreadable() }),
            stopUploads: { await steps.add("stop") },
            clearSession: { await steps.add("clear") }
        )
        let outcome = await flow.signOut()
        XCTAssertEqual(outcome, .pushNotForgotten)
        let order = await steps.order
        XCTAssertEqual(order, [])
    }

    func testForcingSignsOutAndSaysTheTokenWasNotForgotten() async {
        let api = FakePushAPI()
        api.failForget(with: APIError.server(status: 500))
        let steps = Steps()
        let outcome = await flow(api, steps: steps).signOut(forcing: true)
        XCTAssertEqual(outcome, .signedOut(pushForgotten: false))
        let order = await steps.order
        XCTAssertEqual(order, ["stop", "clear"])
    }

    func testAnEndedSignInStillSignsOut() async {
        let api = FakePushAPI()
        api.failForget(with: APIError.revoked)
        let steps = Steps()
        let outcome = await flow(api, steps: steps).signOut()
        XCTAssertEqual(outcome, .signedOut(pushForgotten: true))
    }

    func testAStorageFailureIsReportedAndTheTokenIsSentAgain() async {
        struct Keychain: Error {}
        let api = FakePushAPI()
        let registrar = PushRegistrar(api: { api })
        await registrar.tokenReceived(tokenA)
        await registrar.sync()
        let steps = Steps()
        let flow = SignOutFlow(push: registrar, stopUploads: { await steps.add("stop") }, clearSession: { throw Keychain() })
        let outcome = await flow.signOut()
        XCTAssertEqual(outcome, .storage)
        await registrar.sync()
        XCTAssertEqual(api.registered, [tokenA, tokenA])
    }
}
