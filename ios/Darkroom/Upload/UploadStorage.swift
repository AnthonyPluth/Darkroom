import DarkroomCore
import Foundation

struct UploadStorage {
    let store: ResourceStore
    let versions: IngestVersions
    let syncState: SyncStateStore
    let uploadsDirectory: URL

    static func open() throws -> UploadStorage {
        let base = try FileManager.default
            .url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("Darkroom", isDirectory: true)
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return UploadStorage(
            store: try SQLiteResourceStore(path: base.appendingPathComponent("resources.sqlite").path),
            versions: FileIngestVersions(url: base.appendingPathComponent("versions.journal")),
            syncState: FileSyncStateStore(url: base.appendingPathComponent("sync-state.json")),
            uploadsDirectory: FileManager.default.temporaryDirectory.appendingPathComponent("darkroom-uploads", isDirectory: true)
        )
    }
}
