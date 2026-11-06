import XCTest
@testable import DarkroomCore

private final class FakePushAPI: PushTokenAPI, @unchecked Sendable {
    private let lock = NSLock()
    private var forgets = 0
    private var forgetError: Error?

    var forgetCount: Int { locked { forgets } }
    func failForget(with error: Error?) { locked { forgetError = error } }

    func registerPushToken(_ token: String) async throws {}

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

private actor Steps {
    private(set) var order: [String] = []
    func add(_ step: String) { order.append(step) }
}

private struct KeychainFailure: Error {}

final class SignOutFlowTests: XCTestCase {
    private func flow(
        _ api: FakePushAPI?,
        steps: Steps,
        clearing: @escaping @Sendable () async throws -> Void = {}
    ) -> SignOutFlow {
        SignOutFlow(
            push: api,
            stopUploads: { await steps.add("stop") },
            clearSession: {
                await steps.add("clear")
                try await clearing()
            },
            restartUploads: { await steps.add("restart") }
        )
    }

    func testForgetsThePushTokenBeforeStoppingUploadsAndClearingTheSession() async {
        let api = FakePushAPI()
        let steps = Steps()
        let outcome = await flow(api, steps: steps).signOut()
        XCTAssertEqual(outcome, .signedOut)
        XCTAssertEqual(api.forgetCount, 1)
        let order = await steps.order
        XCTAssertEqual(order, ["stop", "clear"])
    }

    func testStaysSignedInAndKeepsUploadingWhenTheTokenCannotBeForgotten() async {
        let failures: [Error] = [
            APIError.transport("-1009"),
            APIError.server(status: 500),
            APIError.rejected(status: 409),
            APIError.decoding,
            APIError.invalidResponse,
            KeychainFailure(),
        ]
        for failure in failures {
            let api = FakePushAPI()
            api.failForget(with: failure)
            let steps = Steps()
            let outcome = await flow(api, steps: steps).signOut()
            XCTAssertEqual(outcome, .pushNotForgotten, "\(failure)")
            XCTAssertEqual(api.forgetCount, 1, "\(failure)")
            let order = await steps.order
            XCTAssertEqual(order, [], "\(failure)")
        }
    }

    func testAnEndedSignInStillSignsOut() async {
        for ended in [APIError.revoked, APIError.unauthorized, APIError.notSignedIn] {
            let api = FakePushAPI()
            api.failForget(with: ended)
            let steps = Steps()
            let outcome = await flow(api, steps: steps).signOut()
            XCTAssertEqual(outcome, .signedOut, "\(ended)")
            let order = await steps.order
            XCTAssertEqual(order, ["stop", "clear"], "\(ended)")
        }
    }

    func testSignsOutWithoutAskingDarkroomWhenThereIsNoSignIn() async {
        let steps = Steps()
        let outcome = await flow(nil, steps: steps).signOut()
        XCTAssertEqual(outcome, .signedOut)
        let order = await steps.order
        XCTAssertEqual(order, ["stop", "clear"])
    }

    func testAStorageFailureIsReportedAndUploadsStartAgain() async {
        let api = FakePushAPI()
        let steps = Steps()
        let outcome = await flow(api, steps: steps, clearing: { throw KeychainFailure() }).signOut()
        XCTAssertEqual(outcome, .storage)
        XCTAssertEqual(api.forgetCount, 1)
        let order = await steps.order
        XCTAssertEqual(order, ["stop", "clear", "restart"])
    }

    func testUploadsAreNotRestartedWhenSigningOutWorked() async {
        let steps = Steps()
        _ = await flow(FakePushAPI(), steps: steps).signOut()
        let order = await steps.order
        XCTAssertFalse(order.contains("restart"))
    }
}
