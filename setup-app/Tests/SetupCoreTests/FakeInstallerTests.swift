import XCTest
@testable import SetupCore

/// Runs a fake installer as a real child process, the way the app does, and feeds its
/// combined output through the splitter and the state machine.
final class FakeInstallerTests: XCTestCase {
    private func runFake(env extra: [String: String] = [:]) throws -> SetupState {
        let script = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Fixtures/fake-installer.sh")
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [script.path, "--unattended", "--progress", "--personal"]
        proc.environment = ProcessInfo.processInfo.environment.merging(extra) { $1 }
        proc.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        try proc.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()

        var splitter = LineSplitter()
        // Feed in odd-sized chunks to exercise partial lines.
        var lines: [String] = []
        var index = data.startIndex
        while index < data.endIndex {
            let end = data.index(index, offsetBy: 7, limitedBy: data.endIndex) ?? data.endIndex
            lines += splitter.feed(data[index..<end])
            index = end
        }
        lines += splitter.flush()
        let state = lines.reduce(SetupState(alreadyInstalled: false).started()) { $0.applying(parseInstallerLine($1)) }
        return state.exited(status: proc.terminationStatus)
    }

    func testFakeInstallerFinishes() throws {
        let state = try runFake()
        XCTAssertEqual(state.phase, .finished)
        XCTAssertTrue(state.linked)
        XCTAssertTrue(state.steps.allSatisfy { $0.state == .done })
        XCTAssertTrue(state.log.contains("arg: --personal"))
        XCTAssertTrue(state.log.contains("some output for tools"))
    }

    func testFakeInstallerFailure() throws {
        let state = try runFake(env: ["FAKE_FAIL_AT": "homebrew"])
        XCTAssertEqual(state.phase, .failed("homebrew broke"))
        XCTAssertEqual(state.steps.first { $0.id == .homebrew }?.state, .failed)
        XCTAssertEqual(state.steps.first { $0.id == .tools }?.state, .pending)
    }
}
