import DarkroomCore
import SwiftUI

@MainActor
final class UploadsModel: ObservableObject {
    enum Request {
        case refresh
        case syncNow
        case retry([ResourceKey])
    }

    static let unreadable = UploadStatus(
        phase: .error(UploadStatusTracker.queueUnreadable), counts: [:], failures: [], lastSync: nil, firstScanFinished: true
    )

    @Published private(set) var status: UploadStatus?
    @Published private(set) var syncing = false

    private let tracker: UploadStatusTracker?

    init(server: ServerAddress, session: DeviceSession, storage: UploadStorage?) {
        guard let storage else {
            tracker = nil
            return
        }
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
        tracker = UploadStatusTracker(runner: coordinator, store: storage.store, scanner: ingester, syncState: storage.syncState)
    }

    func load() async {
        guard let tracker else {
            status = Self.unreadable
            return
        }
        status = await tracker.status()
    }

    func signInChanged() async {
        await tracker?.signInChanged()
        await load()
    }

    func run(_ request: Request) async {
        guard let tracker else {
            status = Self.unreadable
            return
        }
        guard !syncing else { return }
        syncing = true
        let polling = Task { await poll(tracker) }
        switch request {
        case .refresh: status = await tracker.refresh()
        case .syncNow: status = await tracker.syncNow()
        case .retry(let keys): status = await tracker.retry(keys)
        }
        polling.cancel()
        syncing = false
    }

    private func poll(_ tracker: UploadStatusTracker) async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            if Task.isCancelled { return }
            let latest = await tracker.status()
            if Task.isCancelled { return }
            status = latest
        }
    }
}
