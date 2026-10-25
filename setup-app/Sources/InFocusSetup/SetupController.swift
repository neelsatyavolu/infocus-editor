import AppKit
import Foundation
import SetupCore

/// Paths and settings the app reads from its bundle and the editor's data dir.
struct AppConfig {
    let portalURL: String
    let resources: URL
    let dataDir: URL
    /// Testing only: run this script instead of the bundled installer.
    let installerOverride: String?

    static func load(bundle: Bundle = .main, env: [String: String] = ProcessInfo.processInfo.environment) -> AppConfig {
        let portal = (bundle.object(forInfoDictionaryKey: "InFocusPortalURL") as? String) ?? "https://infocusportal.com"
        return AppConfig(
            portalURL: portal,
            resources: bundle.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath),
            dataDir: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("InFocusEditor"),
            installerOverride: env["INFOCUS_SETUP_INSTALLER"].flatMap { $0.isEmpty ? nil : $0 }
        )
    }

    var appCheckout: URL { dataDir.appendingPathComponent("app") }

    /// An admin account is almost always someone's own computer; the dedicated editing Mac uses a
    /// standard account. Used to preselect "personal" and to warn before "dedicated" changes power settings.
    static var currentUserIsAdmin: Bool {
        let id = Process()
        id.executableURL = URL(fileURLWithPath: "/usr/bin/id")
        id.arguments = ["-Gn"]
        let out = Pipe()
        id.standardOutput = out
        id.standardError = FileHandle.nullDevice
        guard (try? id.run()) != nil else { return false }
        id.waitUntilExit()
        let groups = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return groups.split(separator: " ").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.contains("admin")
    }

    var isInstalled: Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: appCheckout.appendingPathComponent(".git").path)
            && fm.fileExists(atPath: dataDir.appendingPathComponent("config.toml").path)
    }

    /// True when an earlier setup marked this Mac as someone's own computer (config.toml `machine_kind = "personal"`).
    var installedAsPersonal: Bool {
        guard let text = try? String(contentsOf: dataDir.appendingPathComponent("config.toml"), encoding: .utf8)
        else { return false }
        return text.range(of: #"(?m)^\s*machine_kind\s*=\s*"personal""#, options: .regularExpression) != nil
    }

    /// The full installer, exactly as docs/setup-app.md specifies.
    func installerCommand(personal: Bool) -> [String] {
        let script = installerOverride ?? resources.appendingPathComponent("infocus-editor/install/install.sh").path
        return [script, "--unattended", "--progress",
                "--repo", resources.appendingPathComponent("infocus-editor.bundle").path,
                "--package-src", resources.appendingPathComponent("package").path,
                "--portal", portalURL] + (personal ? ["--personal"] : [])
    }

    /// Link only, with the installed editor; falls back to the full installer.
    var linkCommand: (executable: String, arguments: [String]) {
        let cli = appCheckout.appendingPathComponent(".venv/bin/infocus-editor").path
        if installerOverride == nil, FileManager.default.isExecutableFile(atPath: cli) {
            return (cli, ["link", "--progress"])
        }
        return ("/bin/bash", installerCommand(personal: installedAsPersonal))
    }
}

@MainActor
final class SetupController: ObservableObject {
    @Published private(set) var state: SetupState
    @Published private(set) var installedVersion: String?
    @Published var showDetails = false
    /// Welcome-screen answer to "What is this computer?" (personal by default on an admin account).
    @Published var personal = AppConfig.currentUserIsAdmin
    let isAdminAccount = AppConfig.currentUserIsAdmin

    let config: AppConfig
    private var process: Process?
    private let ioQueue = DispatchQueue(label: "org.infocus.editor-setup.io")

    init(config: AppConfig = .load()) {
        self.config = config
        self.state = SetupState(alreadyInstalled: config.isInstalled)
        refreshInstalledVersion()
    }

    var isRunning: Bool { process?.isRunning ?? false }

    /// First setup uses the welcome-screen answer; Repair keeps what the Mac was set up as.
    func setUp() {
        let personal = config.isInstalled ? config.installedAsPersonal : self.personal
        run("/bin/bash", config.installerCommand(personal: personal))
    }

    func linkAgain() {
        let command = config.linkCommand
        run(command.executable, command.arguments)
    }

    func openPortal(_ url: URL) { NSWorkspace.shared.open(url) }

    func openUsersAndGroups() {
        let settings = URL(string: "x-apple.systempreferences:com.apple.Users-Groups-Settings.extension")!
        NSWorkspace.shared.open(settings)
    }

    func stop() {
        guard let process, process.isRunning else { return }
        process.interrupt()
    }

    // MARK: - running the installer

    private func run(_ executable: String, _ arguments: [String]) {
        guard !isRunning else { return }
        state = state.started()
        showDetails = false

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = arguments
        proc.environment = Self.environment()
        proc.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe

        let reader = pipe.fileHandleForReading
        var splitter = LineSplitter()
        let queue = ioQueue
        reader.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            queue.sync {
                let lines = splitter.feed(chunk)
                DispatchQueue.main.async { self?.consume(lines) }
            }
        }
        proc.terminationHandler = { [weak self] finished in
            queue.async {
                reader.readabilityHandler = nil
                let rest = reader.readDataToEndOfFile()
                let lines = splitter.feed(rest) + splitter.flush()
                let status = finished.terminationStatus
                DispatchQueue.main.async {
                    self?.consume(lines)
                    self?.finish(status: status)
                }
            }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            reader.readabilityHandler = nil
            state = state.applying(.error("Couldn't start the installer: \(error.localizedDescription)"))
        }
    }

    private func consume(_ lines: [String]) {
        state = lines.reduce(state) { $0.applying(parseInstallerLine($1)) }
    }

    private func finish(status: Int32) {
        process = nil
        state = state.exited(status: status)
        if case .failed = state.phase { showDetails = true }
        refreshInstalledVersion()
    }

    private func refreshInstalledVersion() {
        guard config.isInstalled else { installedVersion = nil; return }
        installedVersion = Self.gitDescribe(config.appCheckout)
    }

    private static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:\(home)/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["INFOCUS_SETUP_APP"] = "1"
        return env
    }

    /// `git describe --tags` of the installed checkout, or nil.
    private static func gitDescribe(_ checkout: URL) -> String? {
        let git = "/usr/bin/git"
        // /usr/bin/git is a stub that opens an install prompt until the command-line tools exist.
        guard FileManager.default.fileExists(atPath: "/Library/Developer/CommandLineTools/usr/bin/git")
                || FileManager.default.fileExists(atPath: "/Applications/Xcode.app") else { return nil }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: git)
        proc.arguments = ["-C", checkout.path, "describe", "--tags", "--always"]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return nil }
        proc.waitUntilExit()
        guard proc.terminationStatus == 0 else { return nil }
        let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}
