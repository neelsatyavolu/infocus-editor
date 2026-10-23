import AppKit

// loginwindow asks GUI applications to quit during a normal logout/restart/shutdown.
// A sleep assertion alone cannot cancel that request.
final class GuardDelegate: NSObject, NSApplicationDelegate {
    let parent: pid_t

    init(parent: pid_t) {
        self.parent = parent
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if CommandLine.arguments.contains("--check") {
            print("ready")
            exit(0)
        }
        ProcessInfo.processInfo.disableSuddenTermination()
        ProcessInfo.processInfo.disableAutomaticTermination("Keeping the editing Mac logged in")
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [self] _ in
            // Also releases protection after SIGKILL/crashes, when Python cannot clean up.
            if getppid() != parent { exit(0) }
        }
        print("ready")
        fflush(stdout)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard getppid() == parent else { return .terminateNow }
        NSLog("InFocus Editor blocked logout, restart or shutdown. Stop the editor service for maintenance.")
        return .terminateCancel
    }
}

let checking = CommandLine.arguments.dropFirst() == ["--check"]
let parent = checking ? getppid() : (CommandLine.arguments.count == 2 ? Int32(CommandLine.arguments[1]) : nil)
// The worker supplies its PID so dying before this helper initializes cannot leave
// an orphan that mistakes launchd (PID 1) for the editor it should protect.
guard let parent, parent > 1, getppid() == parent else { exit(0) }
let app = NSApplication.shared
let delegate = GuardDelegate(parent: parent)
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
