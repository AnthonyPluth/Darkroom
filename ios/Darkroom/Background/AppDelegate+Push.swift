import DarkroomCore
import UIKit

extension AppDelegate {
    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { await PushCenter.shared.tokenReceived(deviceToken) }
    }

    func application(
        _ application: UIApplication,
        didReceiveRemoteNotification userInfo: [AnyHashable: Any],
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        guard SilentPush.isSilent(userInfo) else {
            completionHandler(.noData)
            return
        }
        Task {
            await AppModel.shared.restore()
            switch await BackgroundWork.silentPush.handle() {
            case .newData: completionHandler(.newData)
            case .noData: completionHandler(.noData)
            case .failed: completionHandler(.failed)
            }
        }
    }
}
