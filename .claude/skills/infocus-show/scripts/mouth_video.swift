// usage: mouth_video <in.mov> <start_s> <end_s> <every_n_frames>   > lines
// Apple Vision face landmarks on every n-th frame in [start, end). One line per sampled frame:
//   <seconds> <face count> [<face centre x 0..1> <mouth openness>]...
// Mouth openness is the inner-lip gap over the face height; how much it changes over a speech run says who's talking.
import AVFoundation
import Vision

let args = CommandLine.arguments
guard args.count == 5, let start = Double(args[2]), let end = Double(args[3]), let every = Int(args[4]), every > 0 else {
    FileHandle.standardError.write("usage: mouth_video <in> <start_s> <end_s> <every_n_frames>\n".data(using: .utf8)!)
    exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let track = asset.tracks(withMediaType: .video)[0]
let reader = try AVAssetReader(asset: asset)
reader.timeRange = CMTimeRange(start: CMTime(seconds: start, preferredTimescale: 60000),
                               end: CMTime(seconds: end, preferredTimescale: 60000))
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
reader.add(output)
reader.startReading()

let request = VNDetectFaceLandmarksRequest()
var n = 0
var lines = ""
while let sample = output.copyNextSampleBuffer() {
    defer { n += 1 }
    if n % every != 0 { continue }
    guard let pixels = CMSampleBufferGetImageBuffer(sample) else { continue }
    let t = CMSampleBufferGetPresentationTimeStamp(sample).seconds
    try VNImageRequestHandler(cvPixelBuffer: pixels).perform([request])
    var line = String(format: "%.4f", t)
    let faces = (request.results ?? []).sorted { $0.boundingBox.midX < $1.boundingBox.midX }
    line += " \(faces.count)"
    for face in faces {
        var openness = -1.0
        if let lips = face.landmarks?.innerLips?.normalizedPoints, !lips.isEmpty {
            let ys = lips.map { Double($0.y) }
            openness = (ys.max()! - ys.min()!)              // in face-box units: 0 closed, ~0.1 open
        }
        line += String(format: " %.4f %.4f", Double(face.boundingBox.midX), openness)
    }
    lines += line + "\n"
}
FileHandle.standardOutput.write(lines.data(using: .utf8)!)
