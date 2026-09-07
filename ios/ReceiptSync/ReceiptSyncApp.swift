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
                .task(id: scenePhase) {
                    guard scenePhase == .active else { return }
                    repeat {
                        await store.syncAll()
                        do { try await Task.sleep(for: .seconds(30)) }
                        catch { return }
                    } while !Task.isCancelled
                }
        }
    }
}
