import Foundation

/// The installer's steps, in the order the app lists them (docs/setup-app.md).
public enum StepID: String, CaseIterable, Sendable {
    case admin, clt, homebrew, tools, ai, model, app, package, link, service, health

    public var title: String {
        switch self {
        case .admin: return "Admin approval"
        case .clt: return "Apple command-line tools"
        case .homebrew: return "Homebrew"
        case .tools: return "Video tools (ffmpeg, node, whisper)"
        case .ai: return "AI tools (Codex, Grok)"
        case .model: return "Speech model"
        case .app: return "Editor program"
        case .package: return "Graphics package"
        case .link: return "Link to the Portal"
        case .service: return "Background service"
        case .health: return "Health check"
        }
    }
}

public enum StepState: String, Sendable {
    case pending, running, done, skipped, failed
}

/// One line of installer output, parsed.
public enum InstallerEvent: Equatable, Sendable {
    case step(StepID, StepState, String)
    case link(code: String, url: URL)
    case linked
    case done
    case error(String)
    case log(String)
}

/// Parses one stdout line. Lines that start with `@@` are protocol lines; anything
/// else (or a malformed protocol line) is plain log output.
public func parseInstallerLine(_ raw: String) -> InstallerEvent {
    let line = raw.trimmingCharacters(in: CharacterSet(charactersIn: "\r\n"))
    guard line.hasPrefix("@@") else { return .log(line) }
    let parts = line.dropFirst(2).split(separator: " ", maxSplits: 3, omittingEmptySubsequences: true)
        .map(String.init)
    guard let verb = parts.first else { return .log(line) }

    switch verb {
    case "step":
        guard parts.count >= 3, let id = StepID(rawValue: parts[1]),
              let state = StepState(rawValue: parts[2]), state != .pending
        else { return .log(line) }
        return .step(id, state, parts.count == 4 ? parts[3] : "")
    case "link":
        guard parts.count >= 3, let url = URL(string: parts[2]),
              let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http"
        else { return .log(line) }
        return .link(code: parts[1], url: url)
    case "linked":
        return parts.count == 1 ? .linked : .log(line)
    case "done":
        return parts.count == 1 ? .done : .log(line)
    case "error":
        let message = parts.dropFirst().joined(separator: " ")
        return .error(message.isEmpty ? "The installer reported an error." : message)
    default:
        return .log(line)
    }
}

/// Splits a byte stream into lines, keeping any partial last line for the next chunk.
public struct LineSplitter: Sendable {
    private var pending = Data()

    public init() {}

    public mutating func feed(_ chunk: Data) -> [String] {
        pending.append(chunk)
        var lines: [String] = []
        while let newline = pending.firstIndex(of: 0x0A) {
            let lineData = pending[pending.startIndex..<newline]
            lines.append(String(decoding: lineData, as: UTF8.self))
            pending.removeSubrange(pending.startIndex...newline)
        }
        return lines
    }

    public mutating func flush() -> [String] {
        guard !pending.isEmpty else { return [] }
        defer { pending = Data() }
        return [String(decoding: pending, as: UTF8.self)]
    }
}
