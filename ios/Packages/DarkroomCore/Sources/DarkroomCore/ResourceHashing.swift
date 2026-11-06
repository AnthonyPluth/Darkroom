import Foundation

public struct ResourceHashing: Sendable {
    private let exporter: ResourceExporter
    private let temporaryDirectory: URL

    public init(exporter: ResourceExporter, temporaryDirectory: URL = FileManager.default.temporaryDirectory) {
        self.exporter = exporter
        self.temporaryDirectory = temporaryDirectory
    }

    public func hash(_ resource: PhotoResource, filename: String) async throws -> LocalResource {
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

public struct LibraryRehasher: ResourceRehasher {
    private let library: PhotoLibrary
    private let hashing: ResourceHashing

    public init(library: PhotoLibrary, exporter: ResourceExporter, temporaryDirectory: URL = FileManager.default.temporaryDirectory) {
        self.library = library
        self.hashing = ResourceHashing(exporter: exporter, temporaryDirectory: temporaryDirectory)
    }

    public func rehash(_ key: ResourceKey) async throws -> LocalResource? {
        let siblings = try await library.resources(ofAsset: key.assetID)
        guard let resource = siblings.first(where: { $0.key == key }) else { return nil }
        let filename = UploadFilename.make(for: resource, assetResources: siblings)
        guard UploadFilename.isAcceptable(filename) else { return nil }
        return try await hashing.hash(resource, filename: filename)
    }
}

extension APIError {
    public static func forUpload(status: Int) -> APIError? {
        switch status {
        case 200..<300: return nil
        case 401: return .unauthorized
        case 400..<500: return .rejected(status: status)
        case 500..<600: return .server(status: status)
        default: return .invalidResponse
        }
    }
}
