import BackgroundTasks
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    static let refreshIdentifier = "\(Bundle.main.bundleIdentifier ?? "com.local.receiptsync").refresh"

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: Self.refreshIdentifier, using: nil) { task in
            guard let refreshTask = task as? BGAppRefreshTask else { return }
            Self.handle(refreshTask)
        }
        return true
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        Self.scheduleBackgroundRefresh()
    }

    static func scheduleBackgroundRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: refreshIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private static func handle(_ task: BGAppRefreshTask) {
        scheduleBackgroundRefresh()
        let work = Task {
            await ReceiptStore.shared.syncAll()
            task.setTaskCompleted(success: !Task.isCancelled)
        }
        task.expirationHandler = { work.cancel() }
    }
}
