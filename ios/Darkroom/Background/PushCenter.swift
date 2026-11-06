import DarkroomCore
import SwiftUI
import UIKit

/// Registers this iPhone for remote notifications and keeps Darkroom's copy of its token current while a device is signed in.
@MainActor
final class PushCenter {
    static let shared = PushCenter()

    let registrar: PushRegistrar

    private init() {
        let reader = DeviceSession(storage: KeychainSessionStorage())
        registrar = PushRegistrar(api: { () async -> PushTokenAPI? in
            _ = try? await reader.restore()
            guard case .signedIn(let server) = await reader.phase else { return nil }
            return PushClient(baseURL: server.url, tokenProvider: reader.tokenProvider())
        })
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
        case .signedOut, .signingIn:
            break
        }
    }

    func foregrounded(_ phase: DeviceSession.Phase) async {
        guard case .signedIn = phase else { return }
        await activate()
    }

    private func activate() async {
        UIApplication.shared.registerForRemoteNotifications()
        await registrar.sync()
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
