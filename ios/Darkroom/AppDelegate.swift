import UIKit

final class BackgroundSessionCompletions: @unchecked Sendable {
    static let shared = BackgroundSessionCompletions()

    private let lock = NSLock()
    private var handlers: [String: () -> Void] = [:]

    func store(_ handler: @escaping () -> Void, for identifier: String) {
        lock.lock()
        handlers[identifier] = handler
        lock.unlock()
    }

    func complete(identifier: String) {
        lock.lock()
        let handler = handlers.removeValue(forKey: identifier)
        lock.unlock()
        guard let handler else { return }
        DispatchQueue.main.async(execute: handler)
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        BackgroundSessionCompletions.shared.store(completionHandler, for: identifier)
    }
}
