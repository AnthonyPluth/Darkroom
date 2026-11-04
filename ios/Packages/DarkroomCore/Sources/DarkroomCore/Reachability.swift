import Foundation

public struct ReachabilityPolicy: Equatable, Sendable {
    public enum Status: Equatable, Sendable {
        case unknown
        case reachable
        case waiting(failures: Int)
    }

    public let baseDelay: TimeInterval
    public let maxDelay: TimeInterval
    public private(set) var status: Status = .unknown
    private var nextProbeAt: Date?

    public init(baseDelay: TimeInterval = 30, maxDelay: TimeInterval = 900) {
        self.baseDelay = baseDelay
        self.maxDelay = maxDelay
    }

    public func shouldProbe(at now: Date) -> Bool {
        guard let nextProbeAt else { return true }
        return now >= nextProbeAt
    }

    public mutating func recordProbe(succeeded: Bool, at now: Date) {
        if succeeded {
            status = .reachable
            nextProbeAt = nil
            return
        }
        let failures: Int
        if case .waiting(let previous) = status { failures = previous + 1 } else { failures = 1 }
        status = .waiting(failures: failures)
        nextProbeAt = now.addingTimeInterval(delay(afterFailures: failures))
    }

    public func delay(afterFailures failures: Int) -> TimeInterval {
        guard failures > 0 else { return 0 }
        let exponent = min(failures - 1, 30)
        return min(baseDelay * pow(2, Double(exponent)), maxDelay)
    }
}

public struct ServerProbe: Sendable {
    private let run: @Sendable () async -> Bool

    public init(_ run: @escaping @Sendable () async -> Bool) {
        self.run = run
    }

    public init(client: APIClient) {
        self.run = {
            do {
                try await client.probe()
                return true
            } catch {
                return false
            }
        }
    }

    public func callAsFunction() async -> Bool {
        await run()
    }
}
