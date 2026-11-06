import DarkroomCore
import SwiftUI

@MainActor
final class UploadsModel: ObservableObject {
    typealias Request = UploadRequest

    static let unreadable = UploadStatus(
        phase: .error(UploadStatusTracker.queueUnreadable), counts: [:], failures: [], lastSync: nil, firstScanFinished: true
    )

    @Published private(set) var status: UploadStatus?
    @Published private(set) var syncing = false

    private let engine: UploadEngine?

    init(engine: UploadEngine?) {
        self.engine = engine
    }

    func load() async {
        guard let engine else {
            show(Self.unreadable)
            return
        }
        show(await engine.status())
    }

    private func show(_ new: UploadStatus) {
        if let text = UploadsPresentation.announcement(from: status, to: new) {
            AccessibilityNotification.Announcement(text).post()
        }
        status = new
    }

    func signInChanged() async {
        await engine?.signInChanged()
        await load()
    }

    func run(_ request: Request) async {
        guard let engine else {
            show(Self.unreadable)
            return
        }
        guard !syncing else { return }
        syncing = true
        let polling = Task { await poll(engine) }
        show(await engine.perform(request))
        polling.cancel()
        syncing = false
    }

    private func poll(_ engine: UploadEngine) async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            if Task.isCancelled { return }
            let latest = await engine.status()
            if Task.isCancelled { return }
            show(latest)
        }
    }
}
