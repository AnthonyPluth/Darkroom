import Foundation

public protocol DeviceAPI: Sendable {
    func check(_ request: CheckRequest) async throws -> CheckReply
    func checkin(_ request: CheckinRequest) async throws
}

extension APIClient: DeviceAPI {}

public protocol UploadTransport: Sendable {
    /// Sends the resource's bytes. Throws `APIError` for what the server or the network did.
    func upload(_ resource: LocalResource) async throws
}

public protocol ResourceRehasher: Sendable {
    /// Exports and hashes the resource again. Nil when it can no longer be read.
    func rehash(_ key: ResourceKey) async throws -> LocalResource?
}

/// Runs one pass of the upload: recover, check what the server needs, upload it, report. All state lives in the store.
public actor UploadCoordinator {
    public enum Outcome: Equatable, Sendable {
        case finished
        case needsSignIn
        case waitingForNetwork
    }

    public struct Result: Equatable, Sendable {
        public var uploaded = 0
        public var alreadyOnServer = 0
        public var failed = 0
        public var outcome = Outcome.finished

        public init() {}
    }

    private enum Disposition {
        case signIn
        case network
        case retry(String)
        case rejected(Int)
    }

    private static let refusedChecks: Set<Int> = [400, 413]

    private let api: DeviceAPI
    private let transport: UploadTransport
    private let rehasher: ResourceRehasher
    private let store: ResourceStore
    private let probe: ServerProbe
    private let appVersion: String
    private let maxAttempts: Int
    private let clock: @Sendable () -> Date
    private var policy: ReachabilityPolicy
    private var inFlight: Task<Result, Error>?

    public init(
        api: DeviceAPI,
        transport: UploadTransport,
        rehasher: ResourceRehasher,
        store: ResourceStore,
        probe: ServerProbe,
        appVersion: String,
        maxAttempts: Int = 3,
        policy: ReachabilityPolicy = ReachabilityPolicy(),
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.api = api
        self.transport = transport
        self.rehasher = rehasher
        self.store = store
        self.probe = probe
        self.appVersion = appVersion
        self.maxAttempts = maxAttempts
        self.policy = policy
        self.clock = clock
    }

    public var reachability: ReachabilityPolicy.Status { policy.status }

    public func run() async throws -> Result {
        while true {
            if let running = inFlight {
                do {
                    return try await Self.join(running)
                } catch is CancellationError {
                    try Task.checkCancellation()
                    if inFlight == running { inFlight = nil }
                    continue
                }
            }
            let pass = Task { try await self.runPass() }
            inFlight = pass
            defer { if inFlight == pass { inFlight = nil } }
            return try await withTaskCancellationHandler {
                try await pass.value
            } onCancel: {
                pass.cancel()
            }
        }
    }

    /// Waits for a pass another caller started. This caller's cancellation ends its own wait, never the pass.
    private static func join(_ pass: Task<Result, Error>) async throws -> Result {
        let waiter = JoinedPass()
        Task { waiter.finish(await pass.result) }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { waiter.wait(with: $0) }
        } onCancel: {
            waiter.finish(.failure(CancellationError()))
        }
    }

    private static func isCancellation(_ error: Error) -> Bool {
        error is CancellationError || Task.isCancelled
    }

    private func runPass() async throws -> Result {
        var result = Result()
        // An upload cut short by a kill is `uploading` on disk; the server was never told it finished.
        let interrupted = try await store.resources(in: [.uploading])
        try await store.setState(.checked, ifUnchanged: interrupted, failureReason: nil)

        guard try await networkReady() else {
            result.outcome = .waitingForNetwork
            return result
        }
        let afterCheck = try await checkPhase(&result)
        if afterCheck != .finished {
            result.outcome = afterCheck
            return result
        }
        let afterUpload = try await uploadPhase(&result)
        if afterUpload != .finished {
            result.outcome = afterUpload
            return result
        }
        result.outcome = try await checkin()
        return result
    }

    private func networkReady() async throws -> Bool {
        guard policy.shouldProbe(at: clock()) else { return false }
        let reachable = await probe()
        try Task.checkCancellation()
        policy.recordProbe(succeeded: reachable, at: clock())
        return reachable
    }

    private func disposition(of error: Error) -> Disposition {
        guard let api = error as? APIError else { return .network }
        switch api {
        case .unauthorized, .revoked, .notSignedIn: return .signIn
        case .transport: return .network
        case .server(let status): return .retry("server error \(status)")
        case .rejected(let status): return status == 408 ? .retry("the upload timed out") : .rejected(status)
        case .decoding, .invalidResponse: return .retry("unexpected reply")
        }
    }

    private func lostNetwork() -> Outcome {
        policy.recordProbe(succeeded: false, at: clock())
        return .waitingForNetwork
    }

    private func checkPhase(_ result: inout Result) async throws -> Outcome {
        let everything = Set(ResourceState.allCases)
        let snapshot = try await store.resources(in: everything)
        var batches = Array(UploadPlanner.checkBatches(from: snapshot).reversed())
        while let batch = batches.popLast() {
            try Task.checkCancellation()
            let reply: CheckReply
            do {
                reply = try await api.check(batch)
            } catch {
                if Self.isCancellation(error) { throw CancellationError() }
                switch disposition(of: error) {
                case .signIn: return .needsSignIn
                case .network: return lostNetwork()
                case .rejected(let status) where Self.refusedChecks.contains(status):
                    // The server refuses the whole batch for one bad asset: halve it until that asset is alone, then fail it.
                    if batch.assets.count > 1 {
                        let middle = batch.assets.count / 2
                        batches.append(CheckRequest(assets: Array(batch.assets[middle...])))
                        batches.append(CheckRequest(assets: Array(batch.assets[..<middle])))
                    } else {
                        let current = try await store.resources(in: everything)
                        let refused = UploadPlanner.declared(in: batch, among: current)
                        result.failed += try await store.setState(.failed, ifUnchanged: refused, failureReason: "the server refused to check it (\(status))")
                    }
                    continue
                case .retry, .rejected: continue
                }
            }
            let current = try await store.resources(in: everything)
            let plan = UploadPlanner.plan(resources: current, checked: batch, reply: reply)
            let rows = Dictionary(uniqueKeysWithValues: current.map { ($0.key, $0) })
            let done = plan.markUploaded.compactMap { rows[$0] }
            let queued = plan.queue.compactMap { rows[$0] }
            result.alreadyOnServer += try await store.setState(.uploaded, ifUnchanged: done, failureReason: nil)
            try await store.setState(.checked, ifUnchanged: queued, failureReason: nil)
        }
        return .finished
    }

    private func uploadPhase(_ result: inout Result) async throws -> Outcome {
        for record in try await store.records(in: [.checked]) {
            try Task.checkCancellation()
            let sent = record.resource
            guard try await store.setState(.uploading, ifUnchanged: [sent], failureReason: nil) == 1 else { continue }
            do {
                try await transport.upload(sent)
                result.uploaded += try await store.setState(.uploaded, ifUnchanged: [sent], failureReason: nil)
            } catch let error where Self.isCancellation(error) {
                try await store.setState(.checked, ifUnchanged: [sent], failureReason: nil)
                throw CancellationError()
            } catch {
                switch disposition(of: error) {
                case .signIn:
                    try await store.setState(.checked, ifUnchanged: [sent], failureReason: nil)
                    return .needsSignIn
                case .network:
                    try await store.setState(.checked, ifUnchanged: [sent], failureReason: nil)
                    return lostNetwork()
                case .retry(let why):
                    // Only these failures count toward the limit; a sign-in or network stop leaves the count alone.
                    guard let attempts = try await store.recordAttempt(ifUnchanged: sent) else { continue }
                    if attempts >= maxAttempts {
                        result.failed += try await store.setFailed(
                            ifUnchanged: [sent], reason: "gave up after \(attempts) attempts: \(why)", recoverable: true
                        )
                    } else {
                        try await store.setState(.checked, ifUnchanged: [sent], failureReason: nil)
                    }
                case .rejected(let status):
                    switch status {
                    case 422:
                        try await handleMismatch(record.resource, &result)
                    case 404, 409:
                        // Not checked, or the asset changed since: check it again.
                        try await store.setState(.hashed, ifUnchanged: [sent], failureReason: nil)
                    default:
                        result.failed += try await store.setState(.failed, ifUnchanged: [sent], failureReason: "the server refused it (\(status))")
                    }
                }
            }
        }
        return .finished
    }

    /// The server saw other bytes than the hash promised: hash once more. A changed file starts over, a same hash fails.
    private func handleMismatch(_ old: LocalResource, _ result: inout Result) async throws {
        let fresh: LocalResource?
        do {
            fresh = try await rehasher.rehash(old.key)
        } catch let error where Self.isCancellation(error) {
            try await store.setState(.checked, ifUnchanged: [old], failureReason: nil)
            throw CancellationError()
        } catch {
            fresh = nil
        }
        if let fresh, fresh.sha1 != old.sha1 || fresh.state != .hashed {
            try await store.replace(old, with: fresh)
            return
        }
        let reason = fresh == nil
            ? "the file could not be read again after the server saw different bytes"
            : "the server received different bytes than the hash promised"
        result.failed += try await store.setState(.failed, ifUnchanged: [old], failureReason: reason)
    }

    private func checkin() async throws -> Outcome {
        guard let counts = try? await store.counts() else { return .finished }
        let pending = [ResourceState.hashed, .checked, .uploading].reduce(0) { $0 + (counts[$1] ?? 0) }
        do {
            try await api.checkin(CheckinRequest(pendingUploads: pending, appVersion: appVersion))
        } catch {
            if Self.isCancellation(error) { throw CancellationError() }
            switch disposition(of: error) {
            case .signIn: return .needsSignIn
            case .network: return lostNetwork()
            case .retry, .rejected: return .finished
            }
        }
        return .finished
    }
}

/// One joined caller's wait: resumed once, by the pass finishing or by the caller being cancelled, whichever is first.
private final class JoinedPass: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<UploadCoordinator.Result, Error>?
    private var outcome: Swift.Result<UploadCoordinator.Result, Error>?

    func wait(with continuation: CheckedContinuation<UploadCoordinator.Result, Error>) {
        lock.lock()
        guard let outcome else {
            self.continuation = continuation
            lock.unlock()
            return
        }
        lock.unlock()
        continuation.resume(with: outcome)
    }

    func finish(_ outcome: Swift.Result<UploadCoordinator.Result, Error>) {
        lock.lock()
        guard self.outcome == nil else {
            lock.unlock()
            return
        }
        self.outcome = outcome
        let waiting = continuation
        continuation = nil
        lock.unlock()
        waiting?.resume(with: outcome)
    }
}
