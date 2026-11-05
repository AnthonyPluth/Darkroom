import Foundation

/// One file the photo library holds for an asset (the original photo, the Live Photo's video, the edited render).
public struct PhotoResource: Equatable, Sendable {
    public let assetID: String
    public let kind: ResourceKind
    /// The name the library reports (`IMG_0001.HEIC`, `IMG_0001.MOV`, `FullSizeRender.jpg`).
    public let originalFilename: String
    /// Changes whenever the file's content can have changed (a modification date plus size, say). Equal means skip.
    public let version: String

    public init(assetID: String, kind: ResourceKind, originalFilename: String, version: String) {
        self.assetID = assetID
        self.kind = kind
        self.originalFilename = originalFilename
        self.version = version
    }

    public var key: ResourceKey {
        ResourceKey(assetID: assetID, kind: kind)
    }
}

public protocol PhotoLibrary: Sendable {
    /// Every resource of every asset, oldest first. The PhotoKit adapter implements this.
    func allResources() async throws -> [PhotoResource]
}

public enum ExportOutcome: Equatable, Sendable {
    case exported
    /// The bytes live only in iCloud; the exporter must not have used the network to fetch them.
    case notLocal
}

public protocol ResourceExporter: Sendable {
    /// Writes the resource's bytes to `destination` without any network access.
    func export(_ resource: PhotoResource, to destination: URL) async throws -> ExportOutcome
}

public protocol IngestVersions: Sendable {
    func version(for key: ResourceKey) async -> String?
    /// Durable when it returns: it throws if the version could not be saved, and then the old version stands.
    /// Nil forgets the version.
    func setVersion(_ version: String?, for key: ResourceKey) async throws
}

public actor InMemoryIngestVersions: IngestVersions {
    private var storage: [ResourceKey: String] = [:]

    public init() {}

    public func version(for key: ResourceKey) -> String? {
        storage[key]
    }

    public func setVersion(_ version: String?, for key: ResourceKey) {
        storage[key] = version
    }
}

/// Keeps the versions in a JSON file so a relaunch does not re-hash what has not changed.
public actor FileIngestVersions: IngestVersions {
    private let url: URL
    private var storage: [String: String]

    public init(url: URL) {
        self.url = url
        if let data = try? Data(contentsOf: url),
           let decoded = try? JSONDecoder().decode([String: String].self, from: data) {
            storage = decoded
        } else {
            storage = [:]
        }
    }

    private static func name(_ key: ResourceKey) -> String {
        "\(key.kind.rawValue)|\(key.assetID)"
    }

    public func version(for key: ResourceKey) -> String? {
        storage[Self.name(key)]
    }

    public func setVersion(_ version: String?, for key: ResourceKey) throws {
        var updated = storage
        updated[Self.name(key)] = version
        let data = try JSONEncoder().encode(updated)
        try data.write(to: url, options: .atomic)
        // Only a version that reached the disk is accepted, so a relaunch agrees with this run.
        storage = updated
    }
}

public enum UploadFilename {
    /// `IMG_0001.HEIC` stays, its video is `IMG_0001.MOV`, its edited render is `IMG_0001_adjusted.<ext>`.
    public static func make(for resource: PhotoResource, assetResources: [PhotoResource]) -> String {
        let primary = assetResources.first { $0.kind == .photo } ?? resource
        let stem = (primary.originalFilename as NSString).deletingPathExtension
        switch resource.kind {
        case .photo:
            return resource.originalFilename
        case .liveVideo:
            return stem + ".MOV"
        case .adjusted:
            let ext = (resource.originalFilename as NSString).pathExtension
            return stem + "_adjusted." + (ext.isEmpty ? "jpg" : ext)
        }
    }
}
