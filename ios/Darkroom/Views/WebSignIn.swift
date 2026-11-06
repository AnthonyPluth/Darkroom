import AuthenticationServices
import UIKit

final class WebSignIn: NSObject, ASWebAuthenticationPresentationContextProviding {
    private struct SheetFailure: Error {}

    private var session: ASWebAuthenticationSession?

    /// The session is ephemeral, so the Pocket ID login always shows and Safari's cookies are neither read nor kept. The
    /// cost is that Safari's saved passkeys and passwords are not offered in the sheet.
    @MainActor
    func authenticate(using url: URL, callbackURLScheme: String) async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            let started = ASWebAuthenticationSession(url: url, callbackURLScheme: callbackURLScheme) { callback, error in
                if let callback {
                    continuation.resume(returning: callback)
                } else {
                    continuation.resume(throwing: error ?? SheetFailure())
                }
            }
            started.prefersEphemeralWebBrowserSession = true
            started.presentationContextProvider = self
            session = started
            if !started.start() {
                continuation.resume(throwing: SheetFailure())
            }
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return windows.first(where: \.isKeyWindow) ?? ASPresentationAnchor()
    }
}
