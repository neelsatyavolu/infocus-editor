// usage: personmatte_video <in.mov> <start_s> <end_s>  > matte.gray   (1920x1080 8-bit gray frames on stdout)
// Runs Vision's accurate person segmentation on every frame in [start, end) and writes full-res mattes.
import AVFoundation
import CoreImage
import Vision

let args = CommandLine.arguments
guard args.count == 4, let start = Double(args[2]), let end = Double(args[3]) else {
    FileHandle.standardError.write("usage: personmatte_video <in> <start_s> <end_s>\n".data(using: .utf8)!)
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

let request = VNGeneratePersonSegmentationRequest()
request.qualityLevel = .accurate
request.outputPixelFormat = kCVPixelFormatType_OneComponent8
let sequence = VNSequenceRequestHandler()
let context = CIContext()
let W = 1920, H = 1080
var gray = [UInt8](repeating: 0, count: W * H)
let out = FileHandle.standardOutput
var n = 0
while let sample = output.copyNextSampleBuffer(), let pixels = CMSampleBufferGetImageBuffer(sample) {
    try sequence.perform([request], on: pixels)
    if let mask = request.results?.first?.pixelBuffer {
        var m = CIImage(cvPixelBuffer: mask)
        m = m.transformed(by: CGAffineTransform(scaleX: CGFloat(W) / m.extent.width, y: CGFloat(H) / m.extent.height))
        context.render(m, toBitmap: &gray, rowBytes: W, bounds: CGRect(x: 0, y: 0, width: W, height: H),
                       format: .L8, colorSpace: nil)
    } else {
        gray = [UInt8](repeating: 0, count: W * H)
    }
    out.write(Data(gray))
    n += 1
    if n % 600 == 0 { FileHandle.standardError.write("\(n) frames\n".data(using: .utf8)!) }
}
FileHandle.standardError.write("done \(n) frames\n".data(using: .utf8)!)
