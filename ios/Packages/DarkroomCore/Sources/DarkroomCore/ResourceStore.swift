import Foundation

public struct ResourceRecord: Equatable, Sendable {
    public var resource: LocalResource
    public var failureReason: String?

    public init(resource: LocalResource, failureReason: String? = nil) {
        self.resource = resource
        self.failureReason = failureReason
    }
}

public protocol ResourceStore: Sendable {
    func upsert(_ resources: [LocalResource]) async throws
    func resources(in states: Set<ResourceState>) async throws -> [LocalResource]
    func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws
    func counts() async throws -> [ResourceState: Int]
}

public actor InMemoryResourceStore: ResourceStore {
    private var records: [ResourceKey: ResourceRecord] = [:]
    private var insertionOrder: [ResourceKey] = []

    public init() {}

    public func upsert(_ resources: [LocalResource]) async throws {
        for resource in resources {
            if records[resource.key] == nil { insertionOrder.append(resource.key) }
            records[resource.key] = ResourceRecord(resource: resource)
        }
    }

    public func resources(in states: Set<ResourceState>) async throws -> [LocalResource] {
        insertionOrder.compactMap { records[$0]?.resource }.filter { states.contains($0.state) }
    }

    public func setState(_ state: ResourceState, for keys: [ResourceKey], failureReason: String?) async throws {
        for key in keys {
            guard var record = records[key] else { continue }
            record.resource.state = state
            record.failureReason = state == .failed ? failureReason : nil
            records[key] = record
        }
    }

    public func counts() async throws -> [ResourceState: Int] {
        records.values.reduce(into: [:]) { $0[$1.resource.state, default: 0] += 1 }
    }

    public func failureReason(for key: ResourceKey) -> String? {
        records[key]?.failureReason
    }
}
