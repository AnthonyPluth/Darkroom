import DarkroomCore
import SwiftUI
import UIKit
import UserNotifications

struct SystemNotificationAuthorizer: NotificationAuthorizer {
    func current() async -> NotificationAuthorization {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .notDetermined: return .notAsked
        case .authorized, .provisional, .ephemeral: return .allowed
        case .denied: return .denied
        @unknown default: return .denied
        }
    }

    func request() async -> Bool {
        let granted = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
        return granted ?? false
    }
}

/// Registers this iPhone for remote notifications and keeps Darkroom's copy of its token current while a device is signed in.
@MainActor
final class PushCenter {
    static let shared = PushCenter()

    let registrar: PushRegistrar
    let notifications = NotificationPermission(authorizer: SystemNotificationAuthorizer())

    var signInEnded: (@MainActor (String) async -> Void)?

    private init() {
        let reader = DeviceSession(storage: KeychainSessionStorage())
        registrar = PushRegistrar(
            api: { () async throws -> PushTokenAPI? in
                try await reader.restore()
                guard case .signedIn(let server) = await reader.phase else { return nil }
                return PushClient(baseURL: server.url, tokenProvider: reader.tokenProvider())
            },
            signInEnded: {
                guard let rejected = await reader.currentToken else { return }
                await PushCenter.shared.signInEnded?(rejected)
            }
        )
    }

    func tokenReceived(_ data: Data) async {
        await registrar.tokenReceived(PushToken.hex(data))
        await registrar.sync()
    }

    func phaseChanged(_ phase: DeviceSession.Phase) async {
        switch phase {
        case .signedIn:
            await registrar.deviceSessionChanged()
            await activate()
        case .signedOut:
            BackgroundWork.nightly.cancel()
        case .signingIn:
            break
        }
    }

    func foregrounded(_ phase: DeviceSession.Phase) async {
        guard case .signedIn = phase else { return }
        await activate()
    }

    private func activate() async {
        UIApplication.shared.registerForRemoteNotifications()
        BackgroundWork.nightly.schedule()
        await registrar.sync()
        await notifications.ensureAsked()
    }
}

private struct PushLifecycle: ViewModifier {
    let phase: DeviceSession.Phase?
    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content
            .task(id: phase) {
                guard let phase else { return }
                await PushCenter.shared.phaseChanged(phase)
            }
            .onChange(of: scenePhase) { _, next in
                guard next == .active, let phase else { return }
                Task { await PushCenter.shared.foregrounded(phase) }
            }
    }
}

extension View {
    /// Pass nil until the saved sign-in has been read, so the first frame's "signed out" is not taken for a sign-out.
    func pushLifecycle(phase: DeviceSession.Phase?) -> some View {
        modifier(PushLifecycle(phase: phase))
    }
}
