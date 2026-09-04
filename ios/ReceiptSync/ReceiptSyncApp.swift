import SwiftUI

@main
struct ReceiptSyncApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = ReceiptStore.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .task { await store.syncAll() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await store.syncAll() } }
        }
    }
}

