import UIKit

@MainActor
final class BackgroundSessionCompletions {
    static let shared = BackgroundSessionCompletions()

    private var handlers: [String: () -> Void] = [:]

    func store(_ handler: @escaping () -> Void, for identifier: String) {
        handlers[identifier] = handler
    }

    func complete(identifier: String) {
        handlers.removeValue(forKey: identifier)?()
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        Task { @MainActor in
            BackgroundSessionCompletions.shared.store(completionHandler, for: identifier)
        }
    }
}
