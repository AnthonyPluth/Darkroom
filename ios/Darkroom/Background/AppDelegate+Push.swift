import UIKit

extension AppDelegate {
    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { await PushCenter.shared.tokenReceived(deviceToken) }
    }
}
