import SwiftUI
import UIKit

/// Little Toby's colours, the same tokens the desktop app and the web app use
/// (SPACE_*, INK_*, TONE_* in scripts/linux_agent_apple.py), with light-mode
/// counterparts so the app follows the phone's appearance.
enum Theme {
    static let accent = Color(hex: 0x5A8CFF)
    static let ok = Color(hex: 0x6FE6A8)
    static let warn = Color(hex: 0xF3C969)
    static let alert = Color(hex: 0xFF8080)

    /// Behind everything.
    static let background = Color.adaptive(light: 0xF5F6FB, dark: 0x0B0C17)
    /// Cards and sheets.
    static let surface = Color.adaptive(light: 0xFFFFFF, dark: 0x0F1022)
    /// A control at rest.
    static let control = Color.adaptive(light: 0xEBEDF5, dark: 0x191C33)
    static let line = Color.adaptive(light: 0xDDE0EC, dark: 0x262A45)

    static let inkBright = Color.adaptive(light: 0x10121F, dark: 0xF4F5FF)
    static let inkNormal = Color.adaptive(light: 0x10121F, dark: 0xF4F5FF, opacity: 0.78)
    static let inkQuiet = Color.adaptive(light: 0x10121F, dark: 0xF4F5FF, opacity: 0.52)

    // Toby's face, exactly as on the computer.
    static let skinLight = Color(red: 1.0, green: 0.85, blue: 0.35)
    static let skinDark = Color(red: 1.0, green: 0.65, blue: 0.15)
    static let faceInk = Color(red: 0.15, green: 0.10, blue: 0.05)
    static let blush = Color(red: 1.0, green: 0.45, blue: 0.35)

    static let cardRadius: CGFloat = 20
    static let controlRadius: CGFloat = 14
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: opacity)
    }

    static func adaptive(light: UInt32, dark: UInt32, opacity: Double = 1) -> Color {
        Color(uiColor: UIColor { traits in
            let hex = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: CGFloat((hex >> 16) & 0xFF) / 255,
                           green: CGFloat((hex >> 8) & 0xFF) / 255,
                           blue: CGFloat(hex & 0xFF) / 255,
                           alpha: CGFloat(opacity))
        })
    }
}

/// A rounded card on the app's surface colour.
struct CardStyle: ViewModifier {
    var padding: CGFloat = 16

    func body(content: Content) -> some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                .strokeBorder(Theme.line, lineWidth: 0.5))
    }
}

extension View {
    func card(padding: CGFloat = 16) -> some View { modifier(CardStyle(padding: padding)) }
}

/// The app's primary and secondary buttons.
struct TobyButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, destructive }
    var kind: Kind = .primary

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .frame(maxWidth: .infinity, minHeight: 48)
            .foregroundStyle(kind == .primary ? Color.white : (kind == .destructive ? Theme.alert : Theme.inkBright))
            .background(background, in: RoundedRectangle(cornerRadius: Theme.controlRadius, style: .continuous))
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(Motion.standard, value: configuration.isPressed)
    }

    private var background: Color {
        switch kind {
        case .primary: return Theme.accent
        case .secondary, .destructive: return Theme.control
        }
    }
}
