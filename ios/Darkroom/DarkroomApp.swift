import SwiftUI
import UIKit

@main
struct DarkroomApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel.shared

    init() {
        BackgroundWork.register()
        Task { await AppModel.shared.restore() }
        let bar = UITabBarAppearance()
        bar.configureWithOpaqueBackground()
        bar.backgroundColor = UIColor(Color.rail)
        bar.shadowColor = UIColor(Color.line)
        UITabBar.appearance().standardAppearance = bar
        UITabBar.appearance().scrollEdgeAppearance = bar
    }

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
        }
    }
}
