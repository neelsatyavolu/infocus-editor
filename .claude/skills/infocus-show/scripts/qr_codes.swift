// QR codes for announcement cards (compiled on first use by announcement_media.py).
//   qr_codes read <image>         -> JSON [{"payload": "...", "box": [x0, y0, x1, y1]}] in image pixels, top-left origin
//   qr_codes make <text> <out.png> -> a clean black-on-white QR, 1200 px, with a quiet zone
import AppKit
import CoreImage
import Foundation
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func read(_ path: String) {
    guard let image = NSImage(contentsOfFile: path),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fail("can't read \(path)") }
    let request = VNDetectBarcodesRequest()
    request.symbologies = [.qr]
    do { try VNImageRequestHandler(cgImage: cg).perform([request]) } catch { fail("vision: \(error)") }
    let w = Double(cg.width), h = Double(cg.height)
    var seen = Set<String>()
    var codes: [[String: Any]] = []
    for result in request.results ?? [] {
        guard let payload = result.payloadStringValue, !payload.isEmpty, seen.insert(payload).inserted else { continue }
        let b = result.boundingBox  // normalized, bottom-left origin
        let box = [b.minX * w, (1 - b.maxY) * h, b.maxX * w, (1 - b.minY) * h].map { Int($0.rounded()) }
        codes.append(["payload": payload, "box": box])
    }
    let data = try! JSONSerialization.data(withJSONObject: codes, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

func make(_ text: String, _ out: String) {
    guard let filter = CIFilter(name: "CIQRCodeGenerator") else { fail("no QR generator") }
    filter.setValue(text.data(using: .utf8), forKey: "inputMessage")
    filter.setValue("M", forKey: "inputCorrectionLevel")
    guard let code = filter.outputImage else { fail("QR generation failed") }
    // CIQRCodeGenerator includes a 1-module quiet zone; pad to 4 modules and scale with hard edges.
    let modules = code.extent.width + 6
    let scale = (1200 / modules).rounded(.down)
    let padded = code.transformed(by: CGAffineTransform(translationX: 3, y: 3))
        .composited(over: CIImage(color: .white).cropped(to: CGRect(x: 0, y: 0, width: modules, height: modules)))
        .samplingNearest()
        .transformed(by: CGAffineTransform(scaleX: scale, y: scale))
    let context = CIContext()
    guard let cg = context.createCGImage(padded, from: padded.extent) else { fail("QR render failed") }
    let rep = NSBitmapImageRep(cgImage: cg)
    guard let png = rep.representation(using: .png, properties: [:]) else { fail("PNG encode failed") }
    do { try png.write(to: URL(fileURLWithPath: out)) } catch { fail("can't write \(out): \(error)") }
    print(out)
}

let args = CommandLine.arguments
switch (args.count, args.count > 1 ? args[1] : "") {
case (3, "read"): read(args[2])
case (4, "make"): make(args[2], args[3])
default: fail("usage: qr_codes read <image> | qr_codes make <text> <out.png>")
}
