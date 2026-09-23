import SwiftUI

/// Hooks for the automated UI tests (LittleTobyUITests), which drive the
/// real app against a real Toby bridge on the build machine. None of this
/// changes behaviour unless the app is launched with -uiTesting.
enum UITestSupport {
    static var arguments: [String] { ProcessInfo.processInfo.arguments }
    static var isActive: Bool { arguments.contains("-uiTesting") }

    static func value(after flag: String) -> String? {
        guard isActive, let index = arguments.firstIndex(of: flag), index + 1 < arguments.count else { return nil }
        return arguments[index + 1]
    }

    /// The address the pairing screen starts with in tests.
    static var server: String? { value(after: "-uiTestServer") }

    static var colorScheme: ColorScheme? {
        switch value(after: "-uiTestAppearance") {
        case "dark": return .dark
        case "light": return .light
        default: return nil
        }
    }

    /// Start from nothing: no paired computers, no tokens, no preferences.
    static func prepare() {
        guard isActive, arguments.contains("-uiTestReset") else { return }
        for computer in ComputerStore().computers {
            Keychain.delete(for: computer.id)
        }
        if let domain = Bundle.main.bundleIdentifier {
            UserDefaults.standard.removePersistentDomain(forName: domain)
        }
    }
}
