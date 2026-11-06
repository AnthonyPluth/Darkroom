import DarkroomCore
import SwiftUI
import UIKit

enum SignInBanner: Equatable {
    case notice(String)
    case failure(String)
}

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()
    static let deviceLabelKey = "deviceLabel"
    static let resetUploadsKey = "resetUploadsOnSignIn"

    @Published private(set) var restored = false
    @Published private(set) var phase = DeviceSession.Phase.signedOut
    @Published private(set) var savedServer: ServerAddress?
    @Published private(set) var addressProblem: ServerAddress.Problem?
    @Published private(set) var nameMissing = false
    @Published private(set) var banner: SignInBanner?
    @Published private(set) var uploadStorage: UploadStorage?
    @Published private(set) var uploadEngine: UploadEngine?
    @Published private(set) var identity: DeviceMeReply?
    @Published private(set) var signingOut = false
    @Published private(set) var signOutProblem: String?
    @Published private(set) var signInAgainProblem: String?

    private let session: DeviceSession
    private let defaults: UserDefaults
    private var restoring: Task<Void, Never>?
    private var unreadable = false

    var deviceSession: DeviceSession { session }

    init(session: DeviceSession = DeviceSession(storage: KeychainSessionStorage()), defaults: UserDefaults = .standard) {
        self.session = session
        self.defaults = defaults
    }

    /// Reads the saved sign-in. A wake-up in the background (`foreground` false) only tries; a failure is not shown and the next
    /// call tries again, because the Keychain may not be readable yet on a cold launch before the first unlock. The screen's own
    /// call (`foreground` true) shows the unreadable notice if it fails too, and a later call still tries again.
    func restore(foreground: Bool = false) async {
        while let running = restoring { await running.value }
        guard !restored || unreadable else { return }
        let task = Task { await performRestore(foreground: foreground) }
        restoring = task
        await task.value
    }

    private func performRestore(foreground: Bool) async {
        defer { restoring = nil }
        do {
            try await session.restore()
        } catch {
            unreadable = true
            guard foreground else { return }
            banner = .failure(SignInMessages.unreadable)
            await refresh()
            finishRestore()
            return
        }
        if unreadable, banner == .failure(SignInMessages.unreadable) { banner = nil }
        unreadable = false
        if case .signedIn = await session.phase {
            await prepareUploads()
        }
        await refresh()
        finishRestore()
    }

    private func finishRestore() {
        restored = true
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
            identity = nil
            signInAgainProblem = nil
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
        guard case .signedIn = await session.phase else { return }
        if uploadStorage == nil {
            uploadStorage = try? UploadStorage.open()
        }
        guard let storage = uploadStorage else {
            return
        }
        if defaults.bool(forKey: Self.resetUploadsKey) {
            do {
                try await NewDeviceSession.reset(store: storage.store, syncState: storage.syncState)
                defaults.set(false, forKey: Self.resetUploadsKey)
            } catch {
                uploadStorage = nil
                return
            }
        }
        guard case .signedIn(let server) = await session.phase else { return }
        if let current = uploadEngine {
            if current.server == server { return }
            await retire(current)
        }
        install(UploadEngine(server: server, session: session, storage: storage))
    }

    /// Drops the engine and its background check before anything awaits, so a wake-up cannot reach the old server.
    private func retire(_ engine: UploadEngine) async {
        uploadEngine = nil
        await engine.stop()
    }

    private func install(_ engine: UploadEngine) {
        uploadEngine = engine
    }

    var deviceLabel: String? {
        guard let saved = defaults.string(forKey: Self.deviceLabelKey), DeviceSignIn.hasName(saved) else { return nil }
        return saved
    }

    func loadIdentity() async {
        guard case .signedIn(let server) = await session.phase, let token = await session.currentToken else {
            identity = nil
            return
        }
        let client = APIClient(baseURL: server.url, tokenProvider: session.tokenProvider())
        let reply: DeviceMeReply?
        do {
            reply = try await client.me()
        } catch is CancellationError {
            return
        } catch {
            reply = nil
        }
        guard await session.currentToken == token else { return }
        identity = reply
    }

    func signOut() async {
        guard !signingOut else { return }
        signingOut = true
        signOutProblem = nil
        var push: PushTokenAPI?
        if case .signedIn(let server) = await session.phase {
            push = PushClient(baseURL: server.url, tokenProvider: session.tokenProvider())
        }
        let flow = SignOutFlow(
            push: push,
            stopUploads: { [self] in
                if let engine = uploadEngine { await engine.stop() }
            },
            clearSession: { [session] in try await session.signOut() },
            restartUploads: { [self] in
                uploadEngine = nil
                await prepareUploads()
            }
        )
        switch await flow.signOut() {
        case .pushNotForgotten:
            signOutProblem = SettingsMessages.pushNotForgotten
            signingOut = false
            return
        case .signedOut:
            uploadEngine = nil
            identity = nil
            banner = nil
        case .storage:
            signOutProblem = SettingsMessages.storage
        }
        signingOut = false
        await refresh()
    }

    func signInAgain() async {
        signInAgainProblem = nil
        if let engine = uploadEngine { await retire(engine) }
        identity = nil
        if let token = await session.currentToken {
            do {
                try await session.tokenRejected(token)
            } catch {
                signInAgainProblem = SettingsMessages.storage
                await prepareUploads()
                await refresh()
                return
            }
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
