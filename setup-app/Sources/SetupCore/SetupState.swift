import Foundation

/// Where the setup is, as one value the UI renders. Every change returns a new state.
public struct SetupState: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
        case welcome(alreadyInstalled: Bool)
        case running
        case linking(code: String, url: URL)
        case finished
        case failed(String)
    }

    public struct Step: Equatable, Sendable {
        public let id: StepID
        public let state: StepState
        public let message: String
    }

    public static let maxLogLines = 5000

    public let phase: Phase
    public let steps: [Step]
    public let log: [String]
    public let linked: Bool

    public init(alreadyInstalled: Bool) {
        self.init(phase: .welcome(alreadyInstalled: alreadyInstalled),
                  steps: StepID.allCases.map { Step(id: $0, state: .pending, message: "") },
                  log: [], linked: false)
    }

    init(phase: Phase, steps: [Step], log: [String], linked: Bool) {
        self.phase = phase
        self.steps = steps
        self.log = log
        self.linked = linked
    }

    /// A fresh run (Set up, Try again, Repair, Link again) keeps nothing from the last one.
    public func started() -> SetupState {
        SetupState(phase: .running,
                   steps: StepID.allCases.map { Step(id: $0, state: .pending, message: "") },
                   log: [], linked: false)
    }

    public func applying(_ event: InstallerEvent) -> SetupState {
        if case .finished = phase { return appendingLog(event) }
        if case .failed = phase { return appendingLog(event) }

        switch event {
        case .log(let line):
            return withLog(line)
        case .step(let id, let state, let message):
            let next = with(steps: steps.map { $0.id == id ? Step(id: id, state: state, message: message) : $0 })
            if state == .failed {
                return next.stoppingSpinners().with(phase: .failed(message.isEmpty ? "\(id.title) didn't finish." : message))
            }
            if case .linking = phase, id != .link { return next.with(phase: .running) }
            return next
        case .link(let code, let url):
            return with(steps: mark(.link, .running, "Waiting for approval in the Portal"))
                .with(phase: .linking(code: code, url: url))
        case .linked:
            return SetupState(phase: .running, steps: mark(.link, .done, "Linked"), log: log, linked: true)
        case .done:
            return with(phase: .finished)
        case .error(let message):
            return stoppingSpinners().with(phase: .failed(message))
        }
    }

    /// The installer process exited. Only a clean `@@done` before exit 0 counts as success.
    public func exited(status: Int32) -> SetupState {
        switch phase {
        case .finished:
            return status == 0 ? self
                : with(phase: .failed("The installer stopped with an error (code \(status))."))
        case .failed:
            return self
        default:
            return stoppingSpinners().with(phase: .failed(status == 0
                ? "The installer ended without finishing. Try again."
                : "The installer stopped with an error (code \(status))."))
        }
    }

    public var progress: Double {
        let total = Double(steps.count)
        let finished = Double(steps.filter { $0.state == .done || $0.state == .skipped }.count)
        return total == 0 ? 0 : finished / total
    }

    // MARK: - helpers

    /// After a failure nothing is still running: a step left "running" goes back to not started.
    private func stoppingSpinners() -> SetupState {
        with(steps: steps.map { $0.state == .running ? Step(id: $0.id, state: .pending, message: "") : $0 })
    }

    private func mark(_ id: StepID, _ state: StepState, _ message: String) -> [Step] {
        steps.map { $0.id == id ? Step(id: id, state: state, message: message) : $0 }
    }

    private func with(phase: Phase) -> SetupState {
        SetupState(phase: phase, steps: steps, log: log, linked: linked)
    }

    private func with(steps: [Step]) -> SetupState {
        SetupState(phase: phase, steps: steps, log: log, linked: linked)
    }

    private func withLog(_ line: String) -> SetupState {
        var lines = log
        lines.append(line)
        if lines.count > Self.maxLogLines { lines.removeFirst(lines.count - Self.maxLogLines) }
        return SetupState(phase: phase, steps: steps, log: lines, linked: linked)
    }

    private func appendingLog(_ event: InstallerEvent) -> SetupState {
        if case .log(let line) = event { return withLog(line) }
        return self
    }
}
