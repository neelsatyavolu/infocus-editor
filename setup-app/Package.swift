// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "InFocusSetup",
    platforms: [.macOS(.v13)],
    targets: [
        // The installer protocol and step state machine: pure, unit-tested.
        .target(name: "SetupCore"),
        .executableTarget(name: "InFocusSetup", dependencies: ["SetupCore"]),
        .testTarget(name: "SetupCoreTests", dependencies: ["SetupCore"], exclude: ["Fixtures"]),
    ]
)
