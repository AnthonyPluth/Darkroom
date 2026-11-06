import Foundation

extension ResourceStore {
    @discardableResult
    public func retryFailures(keys: Set<ResourceKey>? = nil) async throws -> Int {
        var retried = 0
        for record in try await records(in: [.failed]) where record.recoverable && record.resource.hasDescriptor {
            if let keys, !keys.contains(record.resource.key) { continue }
            var again = record.resource
            again.state = .hashed
            if try await replace(record.resource, with: again) { retried += 1 }
        }
        return retried
    }

    public func resetForNewDeviceSession() async throws {
        var again: [LocalResource] = []
        for record in try await records(in: Set(ResourceState.allCases)) where record.resource.state != .notLocal && record.resource.hasDescriptor {
            var fresh = record.resource
            fresh.state = .hashed
            again.append(fresh)
        }
        try await upsert(again)
    }
}

extension ResourceStore {
    @discardableResult
    public func releaseUploading() async throws -> Int {
        let interrupted = try await resources(in: [.uploading])
        return try await setState(.checked, ifUnchanged: interrupted, failureReason: nil)
    }
}
