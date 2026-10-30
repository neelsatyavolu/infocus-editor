// usage: arm_poses <in.mov> <first_frame> <count>   > lines
// Apple Vision body pose on frames [first, first + count) by index: one JSON line per frame,
//   [{"ls": [x, y, c], "le": …, "lw": …, "rs": …, "re": …, "rw": …}, …]   one object per person
// shoulders (s), elbows (e) and wrists (w), left and right, in top-left 1920x1080 pixels with Vision's confidence.
// The forearms and hands are what stays in front of the table top when the anchors are re-seated.
import AVFoundation
import Vision

let args = CommandLine.arguments
guard args.count == 4, let first = Int(args[2]), let count = Int(args[3]), first >= 0 else {
    FileHandle.standardError.write("usage: arm_poses <in> <first_frame> <count>\n".data(using: .utf8)!)
    exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let track = asset.tracks(withMediaType: .video)[0]
let frameDur = track.minFrameDuration.isValid && track.minFrameDuration.seconds > 0
    ? track.minFrameDuration.seconds : 1.0 / Double(track.nominalFrameRate)
let origin = track.timeRange.start.seconds
let reader = try AVAssetReader(asset: asset)
reader.timeRange = CMTimeRange(start: CMTime(seconds: origin + max(0, Double(first) * frameDur - 1.0), preferredTimescale: 600000),
                               duration: .positiveInfinity)
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
reader.add(output)
reader.startReading()

let joints: [(String, VNHumanBodyPoseObservation.JointName)] = [
    ("ls", .leftShoulder), ("le", .leftElbow), ("lw", .leftWrist),
    ("rs", .rightShoulder), ("re", .rightElbow), ("rw", .rightWrist)]
let W = 1920.0, H = 1080.0
var written = 0
while written < count, let sample = output.copyNextSampleBuffer() {
    let index = Int(((CMSampleBufferGetPresentationTimeStamp(sample).seconds - origin) / frameDur).rounded())
    guard index >= first, let pixels = CMSampleBufferGetImageBuffer(sample) else { continue }
    let request = VNDetectHumanBodyPoseRequest()
    try VNImageRequestHandler(cvPixelBuffer: pixels).perform([request])
    var people: [[String: [Double]]] = []
    for obs in request.results ?? [] {
        var person: [String: [Double]] = [:]
        for (key, name) in joints {
            if let p = try? obs.recognizedPoint(name), p.confidence > 0 {
                person[key] = [Double(p.location.x) * W, (1 - Double(p.location.y)) * H, Double(p.confidence)]
            }
        }
        people.append(person)
    }
    let line = try JSONSerialization.data(withJSONObject: people)
    print(String(data: line, encoding: .utf8)!)
    written += 1
}
reader.cancelReading()
