import Foundation
import XCTest
@testable import DarkroomCore

private final class FakeScheduler: ProcessingScheduler, @unchecked Sendable {
    struct Refused: Error {}

    private let lock = NSLock()
    private var requests: [ProcessingRequest] = []
    private var cancelled: [String] = []
    private var refusing = false

    var submitted: [ProcessingRequest] { locked { requests } }
    var cancelledIdentifiers: [String] { locked { cancelled } }
    func refuse() { locked { refusing = true } }

    func submit(_ request: ProcessingRequest) throws {
        try locked {
            if refusing { throw Refused() }
            requests.append(request)
        }
    }

    func cancel(identifier: String) { locked { cancelled.append(identifier) } }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }
}

private final class FakeTask: ProcessingTaskHandle, @unchecked Sendable {
    private let lock = NSLock()
    private let expireAfter: TimeInterval?
    private var results: [Bool] = []

    init(expireAfter: TimeInterval? = nil) { self.expireAfter = expireAfter }

    var completions: [Bool] { lock.lock(); defer { lock.unlock() }; return results }

    func onExpiration(_ handler: @escaping @Sendable () -> Void) {
        guard let expireAfter else { return }
        DispatchQueue.global().asyncAfter(deadline: .now() + expireAfter) { handler() }
    }

    func complete(success: Bool) {
        lock.lock()
        results.append(success)
        lock.unlock()
    }
}

private final class RecordingCheck: BackgroundCheck, @unchecked Sendable {
    private let lock = NSLock()
    private let duration: TimeInterval
    private let outcome: BackgroundOutcome
    private var starts = 0
    private var cancelledAtEnd = false

    init(duration: TimeInterval = 0, outcome: BackgroundOutcome = .uploaded) {
        self.duration = duration
        self.outcome = outcome
    }

    var runs: Int { lock.lock(); defer { lock.unlock() }; return starts }
    var sawCancellation: Bool { lock.lock(); defer { lock.unlock() }; return cancelledAtEnd }

    func run() async -> BackgroundOutcome {
        lock.lock()
        starts += 1
        lock.unlock()
        if duration > 0 { try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000)) }
        let cancelled = Task.isCancelled
        lock.lock()
        cancelledAtEnd = cancelled
        lock.unlock()
        return outcome
    }
}

private var utc: Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    return calendar
}

private func date(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
    utc.date(from: DateComponents(year: 2026, month: 11, day: day, hour: hour, minute: minute))!
}

final class NightlyProcessingTests: XCTestCase {
    private func nightly(
        _ scheduler: FakeScheduler,
        hook: BackgroundCheckHook = BackgroundCheckHook(),
        now: Date = date(6, 12)
    ) -> NightlyProcessing {
        NightlyProcessing(identifier: "app.nightly", scheduler: scheduler, hook: hook, calendar: utc, clock: { now })
    }

    func testTheNextRunIsTheNextTwoInTheMorning() {
        XCTAssertEqual(NightlyProcessing.nextBegin(after: date(6, 1), calendar: utc), date(6, 2))
        XCTAssertEqual(NightlyProcessing.nextBegin(after: date(6, 2), calendar: utc), date(7, 2))
        XCTAssertEqual(NightlyProcessing.nextBegin(after: date(6, 12), calendar: utc), date(7, 2))
    }

    func testSchedulingAsksForNetworkAndNotForPower() {
        let scheduler = FakeScheduler()
        XCTAssertTrue(nightly(scheduler).schedule())
        XCTAssertEqual(
            scheduler.submitted,
            [ProcessingRequest(identifier: "app.nightly", earliestBegin: date(7, 2), requiresNetwork: true, requiresExternalPower: false)]
        )
    }

    func testARefusedRequestIsReported() {
        let scheduler = FakeScheduler()
        scheduler.refuse()
        XCTAssertFalse(nightly(scheduler).schedule())
    }

    func testCancelAsksTheSchedulerToDropTheRequest() {
        let scheduler = FakeScheduler()
        nightly(scheduler).cancel()
        XCTAssertEqual(scheduler.cancelledIdentifiers, ["app.nightly"])
    }

    func testARunReschedulesRunsTheCheckAndCompletes() async {
        let scheduler = FakeScheduler()
        let hook = BackgroundCheckHook()
        let check = RecordingCheck()
        hook.install(check)
        let task = FakeTask()
        await nightly(scheduler, hook: hook).run(task)
        XCTAssertEqual(scheduler.submitted.count, 1)
        XCTAssertEqual(check.runs, 1)
        XCTAssertEqual(task.completions, [true])
    }

    func testARunWithoutACheckStillReschedulesAndCompletes() async {
        let scheduler = FakeScheduler()
        let task = FakeTask()
        await nightly(scheduler).run(task)
        XCTAssertEqual(scheduler.submitted.count, 1)
        XCTAssertEqual(task.completions, [true])
    }

    func testTheRealOutcomeDecidesWhatIosIsTold() async {
        let table: [(BackgroundOutcome, Bool)] = [
            (.uploaded, true), (.nothingNew, true), (.needsSignIn, true), (.waitingForNetwork, false), (.failed, false),
        ]
        for (outcome, success) in table {
            let hook = BackgroundCheckHook()
            hook.install(RecordingCheck(outcome: outcome))
            let task = FakeTask()
            await nightly(FakeScheduler(), hook: hook).run(task)
            XCTAssertEqual(task.completions, [success], "\(outcome)")
        }
    }

    func testExpirationStopsTheCheckAndReportsAnUnfinishedRun() async {
        let scheduler = FakeScheduler()
        let hook = BackgroundCheckHook()
        let check = RecordingCheck(duration: 10)
        hook.install(check)
        let task = FakeTask(expireAfter: 0.05)
        await nightly(scheduler, hook: hook).run(task)
        XCTAssertTrue(check.sawCancellation)
        XCTAssertEqual(task.completions, [false])
        XCTAssertEqual(scheduler.submitted.count, 1)
    }

    func testAnExpirationAfterTheCheckFinishedChangesNothing() async {
        let hook = BackgroundCheckHook()
        let check = RecordingCheck()
        hook.install(check)
        let task = FakeTask(expireAfter: 0.2)
        await nightly(FakeScheduler(), hook: hook).run(task)
        try? await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertFalse(check.sawCancellation)
        XCTAssertEqual(task.completions, [true])
    }
}

final class SilentPushTests: XCTestCase {
    func testOnlyAContentAvailablePushWithoutAnAlertIsSilent() {
        let title: [String: Any] = ["title": "x"]
        let silent: [String: Any] = ["content-available": 1]
        let off: [String: Any] = ["content-available": 0]
        let alert: [String: Any] = ["alert": title, "sound": "default"]
        let both: [String: Any] = ["content-available": 1, "alert": title]
        let plain: [AnyHashable: Any] = ["kind": "google_signin", "alert_id": 4]
        XCTAssertTrue(SilentPush.isSilent(["aps": silent]))
        XCTAssertFalse(SilentPush.isSilent(["aps": off]))
        XCTAssertFalse(SilentPush.isSilent(["aps": alert]))
        XCTAssertFalse(SilentPush.isSilent(["aps": both]))
        XCTAssertFalse(SilentPush.isSilent(plain))
        XCTAssertFalse(SilentPush.isSilent([:]))
    }

    func testWithoutACheckThereIsNoData() async {
        let fetch = await SilentPush(hook: BackgroundCheckHook()).handle()
        XCTAssertEqual(fetch, .noData)
    }

    func testRunsTheCheckOnce() async {
        let hook = BackgroundCheckHook()
        let check = RecordingCheck()
        hook.install(check)
        let fetch = await SilentPush(hook: hook).handle()
        XCTAssertEqual(fetch, .newData)
        XCTAssertEqual(check.runs, 1)
    }

    func testTheRealOutcomeDecidesTheFetchResult() async {
        let table: [(BackgroundOutcome, SilentPush.Fetch)] = [
            (.uploaded, .newData), (.nothingNew, .noData), (.needsSignIn, .noData),
            (.waitingForNetwork, .failed), (.failed, .failed),
        ]
        for (outcome, expected) in table {
            let hook = BackgroundCheckHook()
            hook.install(RecordingCheck(outcome: outcome))
            let fetch = await SilentPush(hook: hook).handle()
            XCTAssertEqual(fetch, expected, "\(outcome)")
        }
    }

    func testStopsACheckThatOutlastsTheTimeLimit() async {
        let hook = BackgroundCheckHook()
        let check = RecordingCheck(duration: 10)
        hook.install(check)
        let fetch = await SilentPush(hook: hook, limit: 0.05).handle()
        XCTAssertEqual(fetch, .failed)
        XCTAssertTrue(check.sawCancellation)
    }
}
