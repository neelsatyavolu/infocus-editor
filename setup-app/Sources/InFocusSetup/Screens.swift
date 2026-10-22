import SetupCore
import SwiftUI

struct RootView: View {
    @ObservedObject var controller: SetupController

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(28)
            }
        }
        .frame(width: 640, height: 620)
        .background(Theme.ink)
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Circle().fill(Theme.text).frame(width: 8, height: 8)
            Text("INFOCUS")
                .font(Theme.font(15, .bold))
                .tracking(2.2)
            Text("Editor setup")
                .font(Theme.font(15))
                .opacity(0.85)
            Spacer()
        }
        .foregroundColor(Theme.text)
        .padding(.horizontal, 28)
        .padding(.vertical, 14)
        .background(Theme.green)
    }

    @ViewBuilder private var content: some View {
        switch controller.state.phase {
        case .welcome(let installed):
            if installed { InstalledScreen(controller: controller) } else { WelcomeScreen(controller: controller) }
        case .running:
            ProgressScreen(controller: controller)
        case .linking(let code, let url):
            LinkScreen(controller: controller, code: code, url: url)
        case .finished:
            DoneScreen(controller: controller)
        case .failed(let message):
            FailedScreen(controller: controller, message: message)
        }
    }
}

// MARK: - Welcome

struct WelcomeScreen: View {
    @ObservedObject var controller: SetupController

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Eyebrow(text: "One-time setup")
            Text("Set up this Mac as the InFocus editor")
                .font(Theme.font(28, .semibold))
                .foregroundColor(Theme.text)
            Text("This installs everything the automatic show editor needs and links this Mac to the Portal. It takes about 20–30 minutes; keep the Mac plugged in.")
                .font(Theme.font(15))
                .foregroundColor(Theme.secondary)
                .lineSpacing(4)
            ComputerKindPicker(personal: $controller.personal, isAdminAccount: controller.isAdminAccount)
            Note(icon: "person.badge.key", text: "An admin will need to type their name and password once, in the standard macOS dialog. Nothing after that needs an admin.")
            Note(icon: "link", text: "At the end you'll get a short code to approve in the Portal.")
            Button("Set up") { controller.setUp() }
                .buttonStyle(PrimaryButtonStyle())
                .keyboardShortcut(.defaultAction)
                .padding(.top, 6)
        }
    }
}

struct InstalledScreen: View {
    @ObservedObject var controller: SetupController

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Eyebrow(text: "Already set up")
            Text("This Mac is the InFocus editor")
                .font(Theme.font(28, .semibold))
                .foregroundColor(Theme.text)
            HStack(spacing: 8) {
                Text("Editor version")
                    .font(Theme.font(14))
                    .foregroundColor(Theme.muted)
                Text(controller.installedVersion ?? "unknown")
                    .font(Theme.mono(13, .medium))
                    .foregroundColor(Theme.text)
            }
            HStack(spacing: 8) {
                Text("This computer")
                    .font(Theme.font(14))
                    .foregroundColor(Theme.muted)
                Text(controller.config.installedAsPersonal ? "My own computer (personal)" : "The editing Mac (dedicated)")
                    .font(Theme.font(14, .medium))
                    .foregroundColor(Theme.text)
            }
            Text("Updates install automatically; manage them in the Portal. The AI account, settings and health are there too, under Automatic editing.")
                .font(Theme.font(15))
                .foregroundColor(Theme.secondary)
                .lineSpacing(4)
            Note(icon: "arrow.left.arrow.right", text: "Switch which machine edits in the Portal under Editing machines.")
            Note(icon: "wrench.and.screwdriver", text: "Repair re-runs setup: it reinstalls anything missing and keeps your settings.")
            Note(icon: "link", text: "Link again gives a new code to approve in the Portal, e.g. after the Mac was unlinked.")
            HStack(spacing: 12) {
                Button("Repair") { controller.setUp() }
                    .buttonStyle(PrimaryButtonStyle())
                Button("Link again") { controller.linkAgain() }
                    .buttonStyle(QuietButtonStyle())
            }
            .padding(.top, 6)
        }
    }
}

// MARK: - Progress

struct ProgressScreen: View {
    @ObservedObject var controller: SetupController

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Eyebrow(text: "Setting up")
            Text(currentTitle)
                .font(Theme.font(24, .semibold))
                .foregroundColor(Theme.text)
            ProgressView(value: controller.state.progress)
                .tint(Theme.greenText)
            StepList(steps: controller.state.steps)
            DetailsLog(controller: controller)
        }
    }

    private var currentTitle: String {
        if let running = controller.state.steps.last(where: { $0.state == .running }) {
            return running.message.isEmpty ? running.id.title : running.message
        }
        return "Working…"
    }
}

struct StepList: View {
    let steps: [SetupState.Step]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(steps, id: \.id) { step in
                HStack(spacing: 12) {
                    StepIcon(state: step.state).frame(width: 18)
                    Text(step.id.title)
                        .font(Theme.font(14, step.state == .running ? .medium : .regular))
                        .foregroundColor(step.state == .pending ? Theme.muted : Theme.text)
                    Spacer()
                    if !step.message.isEmpty && step.state != .running {
                        Text(step.message)
                            .font(Theme.font(12))
                            .foregroundColor(step.state == .failed ? Theme.danger : Theme.muted)
                            .lineLimit(1)
                    }
                }
                .padding(.vertical, 7)
                .padding(.horizontal, 12)
                if step.id != steps.last?.id { Divider().overlay(Theme.line) }
            }
        }
        .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius).stroke(Theme.line, lineWidth: 1))
    }
}

struct StepIcon: View {
    let state: StepState

    var body: some View {
        switch state {
        case .pending:
            Image(systemName: "circle").foregroundColor(Theme.line)
        case .running:
            ProgressView().controlSize(.small).scaleEffect(0.8)
        case .done:
            Image(systemName: "checkmark.circle.fill").foregroundColor(Theme.greenText)
        case .skipped:
            Image(systemName: "checkmark.circle").foregroundColor(Theme.muted)
        case .failed:
            Image(systemName: "xmark.octagon.fill").foregroundColor(Theme.danger)
        }
    }
}

struct DetailsLog: View {
    @ObservedObject var controller: SetupController

    var body: some View {
        DisclosureGroup(isExpanded: $controller.showDetails) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(controller.state.log.enumerated()), id: \.offset) { index, line in
                            Text(line.isEmpty ? " " : line)
                                .font(Theme.mono(11))
                                .foregroundColor(Theme.secondary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(index)
                        }
                    }
                    .padding(10)
                }
                .frame(height: 180)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.panel))
                .onChange(of: controller.state.log.count) { count in
                    if count > 0 { proxy.scrollTo(count - 1, anchor: .bottom) }
                }
            }
        } label: {
            Text("Show details")
                .font(Theme.font(13, .medium))
                .foregroundColor(Theme.muted)
        }
        .tint(Theme.muted)
    }
}

// MARK: - Link

struct LinkScreen: View {
    @ObservedObject var controller: SetupController
    let code: String
    let url: URL

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Eyebrow(text: "Link to the Portal")
            Text("Open the Portal and approve this Mac")
                .font(Theme.font(24, .semibold))
                .foregroundColor(Theme.text)
            Text("A producer signs in to the Portal and approves this code. It's good for 15 minutes.")
                .font(Theme.font(15))
                .foregroundColor(Theme.secondary)
            Text(code)
                .font(Theme.mono(44, .medium))
                .tracking(4)
                .foregroundColor(Theme.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 22)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.panel))
                .overlay(RoundedRectangle(cornerRadius: Theme.radius).stroke(Theme.line, lineWidth: 1))
            HStack(spacing: 14) {
                Button("Open Portal") { controller.openPortal(url) }
                    .buttonStyle(PrimaryButtonStyle())
                    .keyboardShortcut(.defaultAction)
                ProgressView().controlSize(.small)
                Text("Waiting for approval…")
                    .font(Theme.font(14))
                    .foregroundColor(Theme.muted)
            }
            Text(url.absoluteString)
                .font(Theme.mono(11))
                .foregroundColor(Theme.muted)
                .textSelection(.enabled)
            DetailsLog(controller: controller)
        }
    }
}

// MARK: - Done / failed

struct DoneScreen: View {
    @ObservedObject var controller: SetupController

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 40))
                .foregroundColor(Theme.greenText)
            Text("This Mac is now the InFocus editor")
                .font(Theme.font(28, .semibold))
                .foregroundColor(Theme.text)
            Text("Everything else — AI account, settings, updates, health — is in the Portal under Automatic editing.")
                .font(Theme.font(15))
                .foregroundColor(Theme.secondary)
                .lineSpacing(4)
            Note(icon: "powerplug", text: "Keep it plugged in with the lid open. A closed lid without an external display puts it to sleep.")
            Note(icon: "person.crop.circle.badge.checkmark", text: "Turn on automatic login for this account so the editor comes back after a restart. It asks for the account password, so do it here: Users & Groups → Automatically log in as.")
            HStack(spacing: 12) {
                Button("Open Users & Groups") { controller.openUsersAndGroups() }
                    .buttonStyle(QuietButtonStyle())
                Button("Close") { NSApp.terminate(nil) }
                    .buttonStyle(PrimaryButtonStyle())
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.top, 6)
        }
    }
}

struct FailedScreen: View {
    @ObservedObject var controller: SetupController
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Eyebrow(text: "Setup stopped")
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill").foregroundColor(Theme.danger)
                Text(message)
                    .font(Theme.font(15, .medium))
                    .foregroundColor(Theme.danger)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.dangerTint))
            Text("Trying again is safe: finished steps are skipped.")
                .font(Theme.font(14))
                .foregroundColor(Theme.secondary)
            Button("Try again") { controller.setUp() }
                .buttonStyle(PrimaryButtonStyle())
                .keyboardShortcut(.defaultAction)
            StepList(steps: controller.state.steps)
            DetailsLog(controller: controller)
        }
    }
}

struct Note: View {
    let icon: String
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .foregroundColor(Theme.greenText)
                .frame(width: 20)
            Text(text)
                .font(Theme.font(14))
                .foregroundColor(Theme.secondary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// "What is this computer?": the dedicated editing Mac, or someone's own computer for now.
struct ComputerKindPicker: View {
    @Binding var personal: Bool
    var isAdminAccount = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Eyebrow(text: "What is this computer?")
            choice(title: "The editing Mac (dedicated)",
                   detail: "Stays plugged in and never sleeps on power. Recommended.", selected: !personal) { personal = false }
            choice(title: "My own computer, for now (personal)",
                   detail: "Keeps your sleep settings and reuses tools you already have.", selected: personal) { personal = true }
            if !personal && isAdminAccount {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "exclamationmark.triangle").foregroundColor(Theme.amber)
                    Text("This is an admin account, which usually means someone's own computer. The dedicated choice turns off sleep while plugged in and sets up tools for an editing-only account. Choose it only on the team's editing Mac.")
                        .font(Theme.font(13))
                        .foregroundColor(Theme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.amber.opacity(0.12)))
            }
            if personal {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "exclamationmark.shield").foregroundColor(Theme.amber)
                    Text("The AI editor runs with your account's access. For the best isolation, create a separate standard macOS account for the editor and run setup there.")
                        .font(Theme.font(13))
                        .foregroundColor(Theme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.amber.opacity(0.12)))
            }
        }
    }

    private func choice(title: String, detail: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundColor(selected ? Theme.greenText : Theme.muted)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(Theme.font(14, .medium)).foregroundColor(Theme.text)
                    Text(detail).font(Theme.font(12)).foregroundColor(Theme.muted)
                }
                Spacer()
            }
            .padding(12)
            .contentShape(Rectangle())
            .background(RoundedRectangle(cornerRadius: Theme.radius).fill(selected ? Theme.green.opacity(0.14) : Theme.panel))
            .overlay(RoundedRectangle(cornerRadius: Theme.radius).stroke(selected ? Theme.green : Theme.line, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}
