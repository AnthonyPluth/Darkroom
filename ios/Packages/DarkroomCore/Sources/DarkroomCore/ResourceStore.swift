import Foundation

public struct ResourceRecord: Equatable, Sendable {
    public var resource: LocalResource
    public var failureReason: String?
    public var attempts: Int
    public var rehashes: Int

    public init(resource: LocalResource, failureReason: String? = nil, attempts: Int = 0, rehashes: Int = 0) {
        self.resource = resource
        self.failureReason = failureReason
        self.attempts = attempts
        self.rehashes = rehashes
    }
}

public protocol ResourceStore: Sendable {
    func upsert(_ resources: [LocalResource]) async throws
    func record(for key: ResourceKey) async throws -> ResourceRecord?
    func records(in states: Set<ResourceState>) async throws -> [ResourceRecord]
    func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws
    @discardableResult
    func setState(_ state: ResourceState, ifUnchanged resources: [LocalResource], failureReason: String?) async throws -> Int
    func recordAttempt(for keys: [ResourceKey]) async throws
    func recordRehash(for key: ResourceKey) async throws
    func counts() async throws -> [ResourceState: Int]
}

extension ResourceStore {
    @discardableResult
    public func setState(_ state: ResourceState, ifUnchanged resources: [LocalResource], failureReason: String?) async throws -> Int {
        var current: [ResourceKey] = []
        for resource in resources {
            if let record = try await record(for: resource.key), record.resource.hasSameContent(as: resource),
               record.resource.state.canMove(to: state) {
                current.append(resource.key)
            }
        }
        try await setState(state, for: current, failureReason: failureReason)
        return current.count
    }

    public func resources(in states: Set<ResourceState>) async throws -> [LocalResource] {
        try await records(in: states).map(\.resource)
    }
}

public actor InMemoryResourceStore: ResourceStore {
    private var storage: [ResourceKey: ResourceRecord] = [:]
    private var insertionOrder: [ResourceKey] = []

    public init() {}

    public func upsert(_ resources: [LocalResource]) async throws {
        for resource in resources {
            if storage[resource.key] == nil { insertionOrder.append(resource.key) }
            storage[resource.key] = ResourceRecord(resource: resource)
        }
    }

    public func record(for key: ResourceKey) async throws -> ResourceRecord? {
        storage[key]
    }

    public func records(in states: Set<ResourceState>) async throws -> [ResourceRecord] {
        insertionOrder.compactMap { storage[$0] }.filter { states.contains($0.resource.state) }
    }

    public func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        for key in keys {
            guard let current = storage[key]?.resource.state else { continue }
            guard current.canMove(to: state) else {
                throw ResourceStoreError.illegalTransition(key: key, from: current, to: state)
            }
        }
        for key in keys {
            guard var record = storage[key] else { continue }
            record.resource.state = state
            record.failureReason = state == .failed ? failureReason : nil
            storage[key] = record
        }
    }

    @discardableResult
    public func setState(_ state: ResourceState, ifUnchanged resources: [LocalResource], failureReason: String?) async throws -> Int {
        let current = resources.filter {
            guard let stored = storage[$0.key]?.resource else { return false }
            return stored.hasSameContent(as: $0) && stored.state.canMove(to: state)
        }.map(\.key)
        try await setState(state, for: current, failureReason: failureReason)
        return current.count
    }

    public func recordAttempt(for keys: [ResourceKey]) async throws {
        for key in keys { storage[key]?.attempts += 1 }
    }

    public func recordRehash(for key: ResourceKey) async throws {
        storage[key]?.rehashes += 1
    }

    public func counts() async throws -> [ResourceState: Int] {
        storage.values.reduce(into: [:]) { $0[$1.resource.state, default: 0] += 1 }
    }

    public func failureReason(for key: ResourceKey) -> String? {
        storage[key]?.failureReason
    }
}
