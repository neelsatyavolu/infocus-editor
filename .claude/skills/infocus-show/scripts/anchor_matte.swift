// usage: anchor_matte <in.mov> <first_frame> <count> <keep>  > matte.gray   (1920x1080 8-bit gray frames on stdout)
// Vision person instance masks on frames [first, first + count) by index (fewer at the end of the file): keeps only
// the <keep> largest people (the anchors), so a neighbour's shoulder or arm at the frame edge drops out. Soft matte
// at full resolution.
import AVFoundation
import CoreImage
import Vision

let args = CommandLine.arguments
guard args.count == 5, let first = Int(args[2]), let count = Int(args[3]), let keep = Int(args[4]), first >= 0 else {
    FileHandle.standardError.write("usage: anchor_matte <in> <first_frame> <count> <keep>\n".data(using: .utf8)!)
    exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let track = asset.tracks(withMediaType: .video)[0]
let frameDur = track.minFrameDuration.isValid && track.minFrameDuration.seconds > 0
    ? track.minFrameDuration.seconds : 1.0 / Double(track.nominalFrameRate)
let origin = track.timeRange.start.seconds
let reader = try AVAssetReader(asset: asset)
// Start a second early and skip to the frame by its index, so chunks read by different processes line up exactly.
let startS = max(0, Double(first) * frameDur - 1.0)
reader.timeRange = CMTimeRange(start: CMTime(seconds: origin + startS, preferredTimescale: 600000),
                               duration: .positiveInfinity)
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
reader.add(output)
reader.startReading()

let context = CIContext()
let W = 1920, H = 1080
var gray = [UInt8](repeating: 0, count: W * H)
let out = FileHandle.standardOutput
var written = 0

/// Pixel count of each instance label in the observation's low-res label map.
func areas(_ obs: VNInstanceMaskObservation) -> [Int: Int] {
    let labels = obs.instanceMask
    CVPixelBufferLockBaseAddress(labels, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(labels, .readOnly) }
    let w = CVPixelBufferGetWidth(labels), h = CVPixelBufferGetHeight(labels)
    let row = CVPixelBufferGetBytesPerRow(labels)
    let base = CVPixelBufferGetBaseAddress(labels)!.assumingMemoryBound(to: UInt8.self)
    var count = [Int: Int]()
    for y in 0..<h { for x in 0..<w { let v = Int(base[y * row + x]); if v > 0 { count[v, default: 0] += 1 } } }
    return count
}

while written < count, let sample = output.copyNextSampleBuffer() {
    let index = Int(((CMSampleBufferGetPresentationTimeStamp(sample).seconds - origin) / frameDur).rounded())
    guard index >= first, let pixels = CMSampleBufferGetImageBuffer(sample) else { continue }
    let handler = VNImageRequestHandler(cvPixelBuffer: pixels)
    let request = VNGeneratePersonInstanceMaskRequest()
    try handler.perform([request])
    gray = [UInt8](repeating: 0, count: W * H)
    if let obs = request.results?.first {
        let chosen = areas(obs).sorted { $0.value > $1.value }.prefix(keep).map { $0.key }
        if !chosen.isEmpty, let mask = try? obs.generateScaledMaskForImage(forInstances: IndexSet(chosen), from: handler) {
            var m = CIImage(cvPixelBuffer: mask)
            m = m.transformed(by: CGAffineTransform(scaleX: CGFloat(W) / m.extent.width, y: CGFloat(H) / m.extent.height))
            context.render(m, toBitmap: &gray, rowBytes: W, bounds: CGRect(x: 0, y: 0, width: W, height: H),
                           format: .L8, colorSpace: nil)
        }
    }
    out.write(Data(gray))
    written += 1
}
reader.cancelReading()
