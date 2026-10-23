import AppKit

// Send Quit only to the test child, never to loginwindow or the computer itself.
let pid = pid_t(CommandLine.arguments[1])!
let event = NSAppleEventDescriptor(
    eventClass: AEEventClass(0x61657674), // aevt
    eventID: AEEventID(0x71756974), // quit
    targetDescriptor: NSAppleEventDescriptor(processIdentifier: pid),
    returnID: AEReturnID(kAutoGenerateReturnID),
    transactionID: AETransactionID(kAnyTransactionID)
)
do {
    let reply = try event.sendEvent(options: [.waitForReply], timeout: 5)
    let error = reply.paramDescriptor(forKeyword: AEKeyword(0x6572726e))?.int32Value ?? 0 // errn
    guard error == -128 else {
        fputs("Expected userCanceledErr (-128), got \(error)\n", stderr)
        exit(1)
    }
} catch {
    guard (error as NSError).code == -128 else {
        fputs("Quit event failed: \(error)\n", stderr)
        exit(1)
    }
}
