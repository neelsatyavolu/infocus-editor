// usage: face_marks <image>...   One JSON line per image:
//   {"path", "faces": [{"box": [x, y, w, h], "eyes": e, "mouth": m, "yaw": y}, ...]}   (box in top-left pixels)
// eyes: mean over both eyes of eye height / eye width (about 0.3 open, under 0.1 shut).
// mouth: the inner-lip gap in face-box units, as mouth_video measures it (0 closed, about 0.1 open).
// yaw: how far the face turns from the camera, radians (0 when Vision doesn't say).
import Foundation
import Vision

func openness(_ points: [CGPoint]?) -> Double? {
    guard let p = points, p.count > 2 else { return nil }
    let xs = p.map { Double($0.x) }, ys = p.map { Double($0.y) }
    let w = xs.max()! - xs.min()!
    return w > 0 ? (ys.max()! - ys.min()!) / w : nil
}

for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { continue }
    let W = Double(img.width), H = Double(img.height)
    let req = VNDetectFaceLandmarksRequest()
    try VNImageRequestHandler(cgImage: img).perform([req])
    let faces = (req.results ?? []).sorted { $0.boundingBox.midX < $1.boundingBox.midX }.map { f -> [String: Any] in
        let b = f.boundingBox
        let eyes = [openness(f.landmarks?.leftEye?.normalizedPoints), openness(f.landmarks?.rightEye?.normalizedPoints)]
            .compactMap { $0 }
        var mouth = -1.0
        if let lips = f.landmarks?.innerLips?.normalizedPoints, !lips.isEmpty {
            let ys = lips.map { Double($0.y) }
            mouth = ys.max()! - ys.min()!
        }
        return ["box": [Int(b.minX * W), Int((1 - b.maxY) * H), Int(b.width * W), Int(b.height * H)],
                "eyes": eyes.isEmpty ? -1.0 : eyes.reduce(0, +) / Double(eyes.count),
                "mouth": mouth, "yaw": f.yaw?.doubleValue ?? 0.0]
    }
    let line = try JSONSerialization.data(withJSONObject: ["path": path, "faces": faces])
    print(String(data: line, encoding: .utf8)!)
}
