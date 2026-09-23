import Foundation
import Observation

/// A one-tap shortcut on the Home screen. Every one does something real:
/// either asks Toby (exactly as if you'd typed it) or calls the computer
/// directly (status, pause, stop).
struct QuickAction: Codable, Hashable, Identifiable {
    enum Kind: String, Codable, CaseIterable {
        case ask, status, tasks, computer, pause, stop, reconnect, continueTask
    }

    var id: String
    var title: String
    var symbol: String
    var kind: Kind
    var prompt: String?
    var section: String

    static func ask(_ title: String, _ prompt: String, symbol: String, section: String = "My computer") -> QuickAction {
        QuickAction(id: UUID().uuidString, title: title, symbol: symbol, kind: .ask, prompt: prompt, section: section)
    }

    static func builtIn(_ kind: Kind, _ title: String, symbol: String, section: String) -> QuickAction {
        QuickAction(id: "builtin.\(kind.rawValue)", title: title, symbol: symbol, kind: kind, prompt: nil, section: section)
    }
}

@Observable
final class QuickActionStore {
    private(set) var actions: [QuickAction]
    private let defaults: UserDefaults
    private static let key = "toby.quickActions"

    static let standard: [QuickAction] = [
        .builtIn(.status, "Check status", symbol: "gauge.with.dots.needle.33percent", section: "My computer"),
        .ask("Open browser", "Open Firefox", symbol: "safari"),
        .ask("Open terminal", "Open a terminal", symbol: "terminal"),
        .ask("Open files", "Open my files", symbol: "folder"),
        .ask("Start coding", "Open VS Code", symbol: "chevron.left.forwardslash.chevron.right"),
        .builtIn(.reconnect, "Reconnect", symbol: "arrow.clockwise", section: "My computer"),
        .builtIn(.continueTask, "Continue last task", symbol: "arrow.uturn.forward", section: "Toby"),
        .builtIn(.tasks, "Current task", symbol: "checklist", section: "Toby"),
        .builtIn(.pause, "Pause", symbol: "pause.circle", section: "Toby"),
        .builtIn(.stop, "Stop", symbol: "stop.circle", section: "Toby"),
        .builtIn(.computer, "View computer", symbol: "desktopcomputer", section: "Toby"),
    ]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let data = defaults.data(forKey: Self.key),
           let saved = try? JSONDecoder().decode([QuickAction].self, from: data) {
            actions = saved
        } else {
            actions = Self.standard
        }
    }

    var sections: [String] {
        var seen: [String] = []
        for action in actions where !seen.contains(action.section) { seen.append(action.section) }
        return seen
    }

    func add(_ action: QuickAction) { actions.append(action); save() }
    func remove(at offsets: IndexSet) { actions.remove(atOffsets: offsets); save() }
    func move(from source: IndexSet, to destination: Int) { actions.move(fromOffsets: source, toOffset: destination); save() }
    func reset() { actions = Self.standard; save() }

    private func save() {
        if let data = try? JSONEncoder().encode(actions) { defaults.set(data, forKey: Self.key) }
    }
}
