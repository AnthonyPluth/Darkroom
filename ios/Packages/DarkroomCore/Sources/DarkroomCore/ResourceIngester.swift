import Foundation

public struct IngestSummary: Equatable, Sendable {
    public var hashed = 0
    public var notLocal = 0
    public var skipped = 0
    public var failed = 0
    public var removed = 0

    public init(hashed: Int = 0, notLocal: Int = 0, skipped: Int = 0, failed: Int = 0, removed: Int = 0) {
        self.hashed = hashed
        self.notLocal = notLocal
        self.skipped = skipped
        self.failed = failed
        self.removed = removed
    }
}

/// Walks the library, exports each new or changed resource to a temporary file, streams its SHA-1 and records it.
public struct ResourceIngester: Sendable {
    private let library: PhotoLibrary
    private let exporter: ResourceExporter
    private let store: ResourceStore
    private let versions: IngestVersions
    private let temporaryDirectory: URL

    public init(
        library: PhotoLibrary,
        exporter: ResourceExporter,
        store: ResourceStore,
        versions: IngestVersions,
        temporaryDirectory: URL = FileManager.default.temporaryDirectory
    ) {
        self.library = library
        self.exporter = exporter
        self.store = store
        self.versions = versions
        self.temporaryDirectory = temporaryDirectory
    }

    public func run() async throws -> IngestSummary {
        var summary = IngestSummary()
        let all = try await library.allResources()
        let byAsset = Dictionary(grouping: all, by: \.assetID)
        summary.removed = try await forgetResourcesGone(from: Set(all.map(\.key)))
        for resource in all {
            try Task.checkCancellation()
            let known = try await store.record(for: resource.key)
            let previous = await versions.version(for: resource.key)
            if let known, known.resource.state != .notLocal, previous == resource.version {
                try Task.checkCancellation()
                summary.skipped += 1
                continue
            }
            let filename = UploadFilename.make(for: resource, assetResources: byAsset[resource.assetID] ?? [resource])
            guard UploadFilename.isAcceptable(filename) else {
                summary.failed += 1
                continue
            }
            do {
                let local = try await ingest(resource, filename: filename)
                if local.state == .notLocal, let known, known.resource.state == .notLocal, previous == resource.version {
                    try Task.checkCancellation()
                    summary.notLocal += 1
                    continue
                }
                try Task.checkCancellation()
                if previous != nil {
                    try await versions.setVersion(nil, for: resource.key)
                    try Task.checkCancellation()
                }
                do {
                    try await store.upsert([local])
                } catch {
                    _ = try? await versions.setVersion(previous, for: resource.key)
                    throw error
                }
                try Task.checkCancellation()
                try await versions.setVersion(resource.version, for: resource.key)
                if local.state == .notLocal { summary.notLocal += 1 } else { summary.hashed += 1 }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                // Not recorded, so the next run tries this resource again.
                summary.failed += 1
            }
        }
        return summary
    }

    private func forgetResourcesGone(from present: Set<ResourceKey>) async throws -> Int {
        let gone = try await store.records(in: Set(ResourceState.allCases)).map(\.resource.key).filter { !present.contains($0) }
        try Task.checkCancellation()
        guard !gone.isEmpty else { return 0 }
        for key in gone {
            try Task.checkCancellation()
            try await versions.setVersion(nil, for: key)
        }
        try Task.checkCancellation()
        try await store.remove(gone)
        try Task.checkCancellation()
        return gone.count
    }

    private func ingest(_ resource: PhotoResource, filename: String) async throws -> LocalResource {
        let temp = temporaryDirectory.appendingPathComponent("darkroom-ingest-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: temp) }
        switch try await exporter.export(resource, to: temp) {
        case .notLocal:
            return LocalResource(key: resource.key, sha1: "", bytes: 0, filename: filename, state: .notLocal)
        case .exported:
            let sha1 = try SHA1Hasher.hash(ofFileAt: temp, checkingCancellation: true)
            let size = (try FileManager.default.attributesOfItem(atPath: temp.path)[.size] as? NSNumber)?.int64Value ?? 0
            return LocalResource(key: resource.key, sha1: sha1, bytes: size, filename: filename, state: .hashed)
        }
    }
}
