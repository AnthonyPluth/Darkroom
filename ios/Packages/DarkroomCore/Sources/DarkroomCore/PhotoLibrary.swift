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

public enum PhotoLibraryError: Error, Equatable, Sendable {
    case accessDenied
}

public protocol PhotoLibrary: Sendable {
    /// Every resource of every asset, oldest first. The PhotoKit adapter implements this.
    func allResources() async throws -> [PhotoResource]
    func resources(ofAsset assetID: String) async throws -> [PhotoResource]
}

extension PhotoLibrary {
    public func resources(ofAsset assetID: String) async throws -> [PhotoResource] {
        try await allResources().filter { $0.assetID == assetID }
    }
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

/// Keeps the versions in an append-only journal file (one JSON line per change) so a relaunch does not re-hash what
/// has not changed, and a save costs one small append however many resources there are.
public actor FileIngestVersions: IngestVersions {
    private struct Entry: Codable {
        let key: String
        let version: String?
    }

    private let url: URL
    private var storage: [String: String] = [:]

    public init(url: URL) {
        self.url = url
        guard let data = try? Data(contentsOf: url) else { return }
        let decoder = JSONDecoder()
        for line in data.split(separator: UInt8(ascii: "\n")) {
            guard let entry = try? decoder.decode(Entry.self, from: Data(line)) else { continue }
            storage[entry.key] = entry.version
        }
    }

    private static func name(_ key: ResourceKey) -> String {
        "\(key.kind.rawValue)|\(key.assetID)"
    }

    public func version(for key: ResourceKey) -> String? {
        storage[Self.name(key)]
    }

    public func setVersion(_ version: String?, for key: ResourceKey) throws {
        let name = Self.name(key)
        var line = Data("\n".utf8)
        line.append(try JSONEncoder().encode(Entry(key: name, version: version)))
        line.append(Data("\n".utf8))
        if !FileManager.default.fileExists(atPath: url.path),
           !FileManager.default.createFile(atPath: url.path, contents: nil) {
            throw CocoaError(.fileWriteUnknown)
        }
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: line)
        try handle.synchronize()
        storage[name] = version
    }
}

public enum UploadFilename {
    public static let maxLength = 200

    public static func isAcceptable(_ name: String) -> Bool {
        let scalars = name.unicodeScalars
        guard !scalars.isEmpty, scalars.count <= maxLength else { return false }
        guard scalars.allSatisfy({ isPrintable($0) }) else { return false }
        if name.contains("/") || name.contains("\\") || name.hasPrefix(".") || name.lowercased().hasSuffix(".xmp") {
            return false
        }
        return true
    }

    private static func isPrintable(_ scalar: Unicode.Scalar) -> Bool {
        if scalar == " " { return true }
        switch scalar.properties.generalCategory {
        case .control, .format, .surrogate, .privateUse, .unassigned, .lineSeparator, .paragraphSeparator, .spaceSeparator:
            return false
        default:
            return true
        }
    }

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
