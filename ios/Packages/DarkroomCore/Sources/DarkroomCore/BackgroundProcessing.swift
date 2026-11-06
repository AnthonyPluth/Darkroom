import Foundation

/// What a background wake-up does: look for new photos and videos and start their uploads. The upload code installs it in
/// `BackgroundCheckHook`; a wake-up before then does nothing. It stops when its task is cancelled.
public protocol BackgroundCheck: Sendable {
    func run() async -> BackgroundOutcome
}

public enum BackgroundOutcome: Equatable, Sendable {
    case uploaded
    case nothingNew
    case needsSignIn
    case waitingForNetwork
    case failed

    public var completesTask: Bool {
        switch self {
        case .uploaded, .nothingNew, .needsSignIn: return true
        case .waitingForNetwork, .failed: return false
        }
    }

    public var fetchResult: SilentPush.Fetch {
        switch self {
        case .uploaded: return .newData
        case .nothingNew, .needsSignIn: return .noData
        case .waitingForNetwork, .failed: return .failed
        }
    }
}

public final class BackgroundCheckHook: @unchecked Sendable {
    private let lock = NSLock()
    private var check: BackgroundCheck?

    public init() {}

    public func install(_ check: BackgroundCheck?) {
        lock.lock()
        self.check = check
        lock.unlock()
    }

    public var current: BackgroundCheck? {
        lock.lock()
        defer { lock.unlock() }
        return check
    }
}

final class RunGuard: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false
    private var interrupted = false

    func finish() {
        lock.lock()
        finished = true
        lock.unlock()
    }

    func interrupt() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if finished { return false }
        interrupted = true
        return true
    }

    var wasInterrupted: Bool {
        lock.lock()
        defer { lock.unlock() }
        return interrupted
    }
}

public struct ProcessingRequest: Equatable, Sendable {
    public let identifier: String
    public let earliestBegin: Date
    public let requiresNetwork: Bool
    public let requiresExternalPower: Bool

    public init(identifier: String, earliestBegin: Date, requiresNetwork: Bool, requiresExternalPower: Bool) {
        self.identifier = identifier
        self.earliestBegin = earliestBegin
        self.requiresNetwork = requiresNetwork
        self.requiresExternalPower = requiresExternalPower
    }
}

public protocol ProcessingScheduler: Sendable {
    func submit(_ request: ProcessingRequest) throws
    func cancel(identifier: String)
}

public protocol ProcessingTaskHandle: Sendable {
    func onExpiration(_ handler: @escaping @Sendable () -> Void)
    func complete(success: Bool)
}

/// The nightly processing task of #60: scheduled for the small hours with network and external power required, rescheduled
/// at the start of every run, cancelled when iOS says the time is up.
public final class NightlyProcessing: Sendable {
    public static let hour = 2

    private let identifier: String
    private let scheduler: ProcessingScheduler
    private let hook: BackgroundCheckHook
    private let calendar: Calendar
    private let clock: @Sendable () -> Date

    public init(
        identifier: String,
        scheduler: ProcessingScheduler,
        hook: BackgroundCheckHook,
        calendar: Calendar = .current,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.identifier = identifier
        self.scheduler = scheduler
        self.hook = hook
        self.calendar = calendar
        self.clock = clock
    }

    public static func nextBegin(after now: Date, calendar: Calendar) -> Date {
        let components = DateComponents(hour: hour, minute: 0)
        return calendar.nextDate(after: now, matching: components, matchingPolicy: .nextTime)
            ?? now.addingTimeInterval(6 * 3600)
    }

    @discardableResult
    public func schedule() -> Bool {
        let request = ProcessingRequest(
            identifier: identifier,
            earliestBegin: Self.nextBegin(after: clock(), calendar: calendar),
            requiresNetwork: true,
            requiresExternalPower: true
        )
        do {
            try scheduler.submit(request)
            return true
        } catch {
            return false
        }
    }

    public func cancel() {
        scheduler.cancel(identifier: identifier)
    }

    public func run(_ task: ProcessingTaskHandle) async {
        schedule()
        let state = RunGuard()
        let check = hook.current
        let work = Task { () -> BackgroundOutcome in
            let outcome = await check?.run() ?? .nothingNew
            state.finish()
            return outcome
        }
        task.onExpiration {
            if state.interrupt() { work.cancel() }
        }
        let outcome = await work.value
        task.complete(success: !state.wasInterrupted && outcome.completesTask)
    }
}

/// A `content-available` push from the server (`darkroom/providers/apns.py`'s `Wake`): runs the same check, within the
/// time iOS gives a silent push.
public struct SilentPush: Sendable {
    public enum Fetch: Equatable, Sendable {
        case newData
        case noData
        case failed
    }

    public static let timeLimit: TimeInterval = 25

    private let hook: BackgroundCheckHook
    private let limit: TimeInterval

    public init(hook: BackgroundCheckHook, limit: TimeInterval = SilentPush.timeLimit) {
        self.hook = hook
        self.limit = limit
    }

    public static func isSilent(_ userInfo: [AnyHashable: Any]) -> Bool {
        guard let aps = userInfo["aps"] as? [AnyHashable: Any] else { return false }
        guard (aps["content-available"] as? Int) == 1 else { return false }
        return aps["alert"] == nil && aps["sound"] == nil && aps["badge"] == nil
    }

    public func handle() async -> Fetch {
        guard let check = hook.current else { return .noData }
        let state = RunGuard()
        let work = Task { () -> BackgroundOutcome in
            let outcome = await check.run()
            state.finish()
            return outcome
        }
        let nanoseconds = UInt64(max(limit, 0) * 1_000_000_000)
        let timer = Task {
            try? await Task.sleep(nanoseconds: nanoseconds)
            if !Task.isCancelled, state.interrupt() { work.cancel() }
        }
        let outcome = await work.value
        timer.cancel()
        return state.wasInterrupted ? .failed : outcome.fetchResult
    }
}
