import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: SetupController?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let controller, MainActor.assumeIsolated({ controller.isRunning }) else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Setup is still running"
        alert.informativeText = "Quitting now stops it. You can open this app again later and press Repair; finished steps are skipped."
        alert.addButton(withTitle: "Keep running")
        alert.addButton(withTitle: "Quit")
        guard alert.runModal() == .alertSecondButtonReturn else { return .terminateCancel }
        MainActor.assumeIsolated { controller.stop() }
        return .terminateNow
    }
}

struct InFocusSetupApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var controller = SetupController()

    init() { Theme.registerFonts() }

    var body: some Scene {
        Window("InFocus Editor Setup", id: "main") {
            RootView(controller: controller)
                .onAppear { delegate.controller = controller }
        }
        .windowResizability(.contentSize)
    }
}

InFocusSetupApp.main()
