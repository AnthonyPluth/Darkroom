import DarkroomCore
import Foundation

enum UploadRequest {
    case refresh
    case syncNow
    case retry([ResourceKey])
}

final class UploadEngine: @unchecked Sendable {
    let server: ServerAddress
    private let tracker: UploadStatusTracker
    private let transport: BackgroundUploadTransport
    private let store: ResourceStore
    private let runs = RunRegistry()

    init(server: ServerAddress, session: DeviceSession, storage: UploadStorage) {
        let client = APIClient(baseURL: server.url, tokenProvider: session.tokenProvider())
        let library = PhotoKitLibrary()
        let exporter = PhotoKitExporter()
        let identifier = (Bundle.main.bundleIdentifier ?? "darkroom") + ".upload"
        let transport = BackgroundUploadTransport(
            client: client, library: library, exporter: exporter, identifier: identifier, directory: storage.uploadsDirectory
        )
        let coordinator = UploadCoordinator(
            api: client,
            transport: transport,
            rehasher: LibraryRehasher(library: library, exporter: exporter),
            store: storage.store,
            probe: ServerProbe(client: client),
            appVersion: BuildVersion.current.marketing
        )
        let ingester = ResourceIngester(library: library, exporter: exporter, store: storage.store, versions: storage.versions)
        self.server = server
        self.transport = transport
        self.store = storage.store
        tracker = UploadStatusTracker(runner: coordinator, store: storage.store, scanner: ingester, syncState: storage.syncState)
    }

    func status() async -> UploadStatus {
        await tracker.status()
    }

    func signInChanged() async {
        await tracker.signInChanged()
    }

    func perform(_ request: UploadRequest) async -> UploadStatus {
        let tracker = self.tracker
        let status = await runs.run { () async -> UploadStatus in
            switch request {
            case .refresh: return await tracker.refresh()
            case .syncNow: return await tracker.syncNow()
            case .retry(let keys): return await tracker.retry(keys)
            }
        }
        if let status { return status }
        return await tracker.status()
    }

    func stop() async {
        await runs.stop()
        transport.cancelAll()
        _ = try? await store.releaseUploading()
    }
}
