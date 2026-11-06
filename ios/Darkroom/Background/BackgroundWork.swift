import BackgroundTasks
import DarkroomCore
import Foundation

struct SystemProcessingScheduler: ProcessingScheduler {
    func submit(_ request: ProcessingRequest) throws {
        let task = BGProcessingTaskRequest(identifier: request.identifier)
        task.earliestBeginDate = request.earliestBegin
        task.requiresNetworkConnectivity = request.requiresNetwork
        task.requiresExternalPower = request.requiresExternalPower
        try BGTaskScheduler.shared.submit(task)
    }

    func cancel(identifier: String) {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier)
    }
}

final class SystemProcessingTask: ProcessingTaskHandle, @unchecked Sendable {
    private let task: BGProcessingTask

    init(_ task: BGProcessingTask) {
        self.task = task
    }

    func onExpiration(_ handler: @escaping @Sendable () -> Void) {
        task.expirationHandler = handler
    }

    func complete(success: Bool) {
        task.setTaskCompleted(success: success)
    }
}

/// The wake-ups that run the upload check without the app being opened. The upload code sets `hook` to the check.
enum BackgroundWork {
    static let hook = BackgroundCheckHook()
    static let nightlyIdentifier = (Bundle.main.bundleIdentifier ?? "") + ".nightly"
    static let nightly = NightlyProcessing(
        identifier: nightlyIdentifier,
        scheduler: SystemProcessingScheduler(),
        hook: hook
    )
    static let silentPush = SilentPush(hook: hook)

    static func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: nightlyIdentifier, using: nil) { task in
            guard let task = task as? BGProcessingTask else {
                task.setTaskCompleted(success: false)
                return
            }
            let handle = SystemProcessingTask(task)
            Task { await BackgroundWork.nightly.run(handle) }
        }
    }
}
