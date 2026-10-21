import XCTest
@testable import SetupCore

final class InstallerProtocolTests: XCTestCase {
    func testPlainLinesAreLog() {
        XCTAssertEqual(parseInstallerLine("==> Installing ffmpeg"), .log("==> Installing ffmpeg"))
    }

    func testStepLine() {
        XCTAssertEqual(parseInstallerLine("@@step tools running Installing ffmpeg and node\n"),
                       .step(.tools, .running, "Installing ffmpeg and node"))
        XCTAssertEqual(parseInstallerLine("@@step admin done"), .step(.admin, .done, ""))
    }

    func testUnknownOrMalformedStepIsLog() {
        XCTAssertEqual(parseInstallerLine("@@step nope running x"), .log("@@step nope running x"))
        XCTAssertEqual(parseInstallerLine("@@step tools sideways"), .log("@@step tools sideways"))
        XCTAssertEqual(parseInstallerLine("@@step tools pending"), .log("@@step tools pending"))
        XCTAssertEqual(parseInstallerLine("@@"), .log("@@"))
    }

    func testLinkLine() {
        let url = URL(string: "https://portal.example.org/show-roles?link=K7Q2-M9XD")!
        XCTAssertEqual(parseInstallerLine("@@link K7Q2-M9XD \(url.absoluteString)"),
                       .link(code: "K7Q2-M9XD", url: url))
    }

    func testLinkRejectsNonWebURL() {
        XCTAssertEqual(parseInstallerLine("@@link K7Q2 file:///etc/passwd"), .log("@@link K7Q2 file:///etc/passwd"))
    }

    func testTerminalLines() {
        XCTAssertEqual(parseInstallerLine("@@linked"), .linked)
        XCTAssertEqual(parseInstallerLine("@@done"), .done)
        XCTAssertEqual(parseInstallerLine("@@error Homebrew couldn't be installed: no network"),
                       .error("Homebrew couldn't be installed: no network"))
        XCTAssertEqual(parseInstallerLine("@@error"), .error("The installer reported an error."))
    }

    func testLineSplitterKeepsPartialLines() {
        var splitter = LineSplitter()
        XCTAssertEqual(splitter.feed(Data("@@step adm".utf8)), [])
        XCTAssertEqual(splitter.feed(Data("in running Waiting\nhello\npar".utf8)),
                       ["@@step admin running Waiting", "hello"])
        XCTAssertEqual(splitter.flush(), ["par"])
        XCTAssertEqual(splitter.flush(), [])
    }
}

final class SetupStateTests: XCTestCase {
    private func run(_ lines: [String], from state: SetupState = SetupState(alreadyInstalled: false).started())
        -> SetupState {
        lines.reduce(state) { $0.applying(parseInstallerLine($1)) }
    }

    func testStartsOnWelcome() {
        XCTAssertEqual(SetupState(alreadyInstalled: true).phase, .welcome(alreadyInstalled: true))
        XCTAssertTrue(SetupState(alreadyInstalled: false).steps.allSatisfy { $0.state == .pending })
    }

    func testHappyPath() {
        let lines = StepID.allCases.filter { $0 != .link }.flatMap { ["@@step \($0.rawValue) running", "@@step \($0.rawValue) done ok"] }
        var state = run(Array(lines.prefix(16)))
        state = run(["@@link ABCD-EFGH https://portal.example.org/show-roles?link=ABCD-EFGH"], from: state)
        guard case .linking(let code, _) = state.phase else { return XCTFail("expected linking, got \(state.phase)") }
        XCTAssertEqual(code, "ABCD-EFGH")
        state = run(["@@linked"] + Array(lines.dropFirst(16)) + ["@@done"], from: state)
        XCTAssertEqual(state.phase, .finished)
        XCTAssertTrue(state.linked)
        XCTAssertEqual(state.exited(status: 0).phase, .finished)
        XCTAssertEqual(state.progress, 1.0, accuracy: 0.001)
    }

    func testStepFailureFailsTheRun() {
        let state = run(["@@step homebrew running", "@@step homebrew failed No network connection"])
        XCTAssertEqual(state.phase, .failed("No network connection"))
        XCTAssertEqual(state.steps.first { $0.id == .homebrew }?.state, .failed)
    }

    func testErrorLineFailsTheRun() {
        XCTAssertEqual(run(["@@error The admin dialog was cancelled"]).phase, .failed("The admin dialog was cancelled"))
    }

    func testExitWithoutDoneIsAFailure() {
        XCTAssertEqual(run(["@@step admin done"]).exited(status: 0).phase,
                       .failed("The installer ended without finishing. Try again."))
        XCTAssertEqual(run(["@@step admin done"]).exited(status: 3).phase,
                       .failed("The installer stopped with an error (code 3)."))
    }

    func testDoneThenNonZeroExitIsAFailure() {
        XCTAssertEqual(run(["@@done"]).exited(status: 1).phase,
                       .failed("The installer stopped with an error (code 1)."))
    }

    func testEventsAfterFailureOnlyLog() {
        let state = run(["@@error boom", "@@done", "more output"])
        XCTAssertEqual(state.phase, .failed("boom"))
        XCTAssertEqual(state.log.last, "more output")
    }

    func testStartedResetsEverything() {
        let failed = run(["@@step admin done", "log line", "@@error boom"])
        let fresh = failed.started()
        XCTAssertEqual(fresh.phase, .running)
        XCTAssertTrue(fresh.log.isEmpty)
        XCTAssertTrue(fresh.steps.allSatisfy { $0.state == .pending })
    }

    func testLogIsCapped() {
        let lines = (0..<(SetupState.maxLogLines + 10)).map { "line \($0)" }
        let state = run(lines)
        XCTAssertEqual(state.log.count, SetupState.maxLogLines)
        XCTAssertEqual(state.log.last, "line \(SetupState.maxLogLines + 9)")
    }

    func testLaterStepLeavesLinkingScreen() {
        let state = run(["@@link ABCD-EFGH https://portal.example.org/x", "@@step service running"])
        XCTAssertEqual(state.phase, .running)
    }
}
