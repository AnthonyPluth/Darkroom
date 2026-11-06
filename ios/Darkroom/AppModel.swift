import DarkroomCore
import SwiftUI
import UIKit

enum SignInBanner: Equatable {
    case notice(String)
    case failure(String)
}

@MainActor
final class AppModel: ObservableObject {
    static let deviceLabelKey = "deviceLabel"
    static let resetUploadsKey = "resetUploadsOnSignIn"

    @Published private(set) var restored = false
    @Published private(set) var phase = DeviceSession.Phase.signedOut
    @Published private(set) var savedServer: ServerAddress?
    @Published private(set) var addressProblem: ServerAddress.Problem?
    @Published private(set) var nameMissing = false
    @Published private(set) var banner: SignInBanner?
    @Published private(set) var uploadStorage: UploadStorage?

    private let session: DeviceSession
    private let defaults: UserDefaults

    var deviceSession: DeviceSession { session }

    init(session: DeviceSession = DeviceSession(storage: KeychainSessionStorage()), defaults: UserDefaults = .standard) {
        self.session = session
        self.defaults = defaults
    }

    func restore() async {
        guard !restored else { return }
        do {
            try await session.restore()
        } catch {
            banner = .failure(SignInMessages.unreadable)
        }
        if case .signedIn = await session.phase {
            await prepareUploads()
        }
        await refresh()
        restored = true
        PushCenter.shared.signInEnded = { [weak self] token in await self?.signInEnded(rejected: token) }
    }

    func signInEnded(rejected: String) async {
        do {
            try await session.tokenRejected(rejected)
        } catch {
            return
        }
        await refresh()
        if case .signedOut = phase {
            banner = .notice(SignInMessages.ended)
        }
    }

    /// The name to put in the "Name of this iPhone" field: the last one used here, else the device's own name unless iOS
    /// hides it behind the generic "iPhone", else empty.
    func suggestedDeviceName() -> String {
        if let saved = defaults.string(forKey: Self.deviceLabelKey), DeviceSignIn.hasName(saved) {
            return saved
        }
        let name = UIDevice.current.name
        return name == DeviceSignIn.fallbackDeviceName ? "" : name
    }

    /// Checks both fields and, if they are fine, starts an attempt and returns the page to open.
    func begin(addressText: String, deviceName: String) async -> URL? {
        banner = nil
        addressProblem = nil
        nameMissing = !DeviceSignIn.hasName(deviceName)
        do {
            _ = try ServerAddress(parsing: addressText)
        } catch let problem as ServerAddress.Problem {
            addressProblem = problem
        } catch {
            addressProblem = .invalid
        }
        guard !nameMissing, addressProblem == nil else { return nil }
        defaults.set(DeviceSignIn.cleanedDeviceName(deviceName), forKey: Self.deviceLabelKey)
        do {
            let start = try await session.begin(serverText: addressText, deviceName: deviceName)
            defaults.set(true, forKey: Self.resetUploadsKey)
            await refresh()
            return start
        } catch {
            show(error)
            await refresh()
            return nil
        }
    }

    func finish(callback: URL) async {
        do {
            try await session.complete(callback: callback)
            await prepareUploads()
        } catch {
            defaults.set(false, forKey: Self.resetUploadsKey)
            show(error)
        }
        await refresh()
    }

    func cancelled() async {
        defaults.set(false, forKey: Self.resetUploadsKey)
        await session.cancel()
        banner = .notice(SignInMessages.cancelled)
        await refresh()
    }

    func sheetFailed() async {
        defaults.set(false, forKey: Self.resetUploadsKey)
        await session.cancel()
        banner = .failure(SignInMessages.sheetFailed)
        await refresh()
    }

    func prepareUploads() async {
        if uploadStorage == nil {
            uploadStorage = try? UploadStorage.open()
        }
        guard let storage = uploadStorage, defaults.bool(forKey: Self.resetUploadsKey) else { return }
        do {
            try await NewDeviceSession.reset(store: storage.store, syncState: storage.syncState)
            defaults.set(false, forKey: Self.resetUploadsKey)
        } catch {
            uploadStorage = nil
        }
    }

    func signInAgain() async {
        if let token = await session.currentToken {
            try? await session.tokenRejected(token)
        }
        banner = .notice(SignInMessages.ended)
        await refresh()
    }

    private func show(_ error: Error) {
        guard let failure = error as? SignInError, let text = SignInMessages.failure(failure) else { return }
        banner = .failure(text)
    }

    private func refresh() async {
        phase = await session.phase
        savedServer = await session.savedServer
    }
}
