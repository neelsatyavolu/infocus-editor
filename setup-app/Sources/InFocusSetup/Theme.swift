import AppKit
import CoreText
import SwiftUI

/// InFocus 2026 design system, dark (Ink) surfaces: Portal DESIGN.md §10.
enum Theme {
    static let ink = Color(hex: 0x0F110F)
    static let panel = Color(hex: 0x1A1D1A)
    static let raised = Color(hex: 0x252925)
    static let line = Color(hex: 0x2B302B)
    static let text = Color(hex: 0xECEFEA)
    static let secondary = Color(hex: 0xDCE2DE)
    static let muted = Color(hex: 0xA3ABA6)
    static let green = Color(hex: 0x0B6E3E)
    static let greenText = Color(hex: 0x2BB36E)
    static let amber = Color(hex: 0xF2A516)
    static let danger = Color(hex: 0xFF7A8A)
    static let dangerTint = Color(hex: 0x3A1218)
    static let radius: CGFloat = 6

    private static var lexendLoaded = false

    /// Registers the package's Lexend fonts (bundled in Resources/Fonts) for this process.
    static func registerFonts(bundle: Bundle = .main) {
        guard let dir = bundle.resourceURL?.appendingPathComponent("Fonts"),
              let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
        else { return }
        for file in files where file.pathExtension.lowercased() == "ttf" {
            CTFontManagerRegisterFontsForURL(file as CFURL, .process, nil)
        }
        lexendLoaded = NSFont(name: "Lexend-Regular", size: 12) != nil
    }

    static func font(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        guard lexendLoaded else { return .system(size: size, weight: weight) }
        let name: String
        switch weight {
        case .bold, .heavy, .black: name = "Lexend-Bold"
        case .semibold: name = "Lexend-SemiBold"
        case .medium: name = "Lexend-Medium"
        default: name = "Lexend-Regular"
        }
        return .custom(name, size: size)
    }

    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}

/// Solid InFocus Green with Soft White text: the one main action on a screen.
struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.font(15, .medium))
            .foregroundColor(Theme.text)
            .padding(.horizontal, 22)
            .padding(.vertical, 10)
            .background(RoundedRectangle(cornerRadius: Theme.radius)
                .fill(Theme.green.opacity(configuration.isPressed ? 0.8 : (isEnabled ? 1 : 0.4))))
    }
}

/// Outlined, quiet: secondary actions.
struct QuietButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.font(14, .medium))
            .foregroundColor(Theme.text)
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .background(RoundedRectangle(cornerRadius: Theme.radius)
                .fill(configuration.isPressed ? Theme.raised : Theme.panel))
            .overlay(RoundedRectangle(cornerRadius: Theme.radius).stroke(Theme.line, lineWidth: 1))
    }
}

/// Small all-caps eyebrow label with wide tracking.
struct Eyebrow: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(Theme.font(11, .medium))
            .tracking(1.8)
            .foregroundColor(Theme.muted)
    }
}
