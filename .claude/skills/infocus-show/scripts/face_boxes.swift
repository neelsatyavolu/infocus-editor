// usage: face_boxes <image>...   One JSON line per image: {"path", "faces": [[x, y, w, h], ...]} in top-left pixels.
import Foundation
import Vision

for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { continue }
    let W = Double(img.width), H = Double(img.height)
    let req = VNDetectFaceRectanglesRequest()
    try VNImageRequestHandler(cgImage: img).perform([req])
    let faces = (req.results ?? []).map { f -> [Int] in
        let b = f.boundingBox
        return [Int(b.minX * W), Int((1 - b.maxY) * H), Int(b.width * W), Int(b.height * H)]
    }
    let line = try JSONSerialization.data(withJSONObject: ["path": path, "faces": faces])
    print(String(data: line, encoding: .utf8)!)
}
