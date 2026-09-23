import Foundation
import Observation

/// A computer this phone is paired with. The token lives in the Keychain.
struct PairedComputer: Codable, Hashable, Identifiable {
    var id: String            // the computer's id, from its Toby
    var name: String
    var baseURL: URL
    var pairedAt: Date
}

/// The computers this phone is paired with, and which one is in use.
@Observable
final class ComputerStore {
    private(set) var computers: [PairedComputer] = []
    private(set) var activeID: String?

    private let defaults: UserDefaults
    private static let listKey = "toby.computers"
    private static let activeKey = "toby.activeComputer"
    private static let deviceKey = "toby.deviceID"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let data = defaults.data(forKey: Self.listKey),
           let list = try? JSONDecoder().decode([PairedComputer].self, from: data) {
            computers = list
        }
        activeID = defaults.string(forKey: Self.activeKey) ?? computers.first?.id
    }

    func setActive(_ id: String?) {
        activeID = id
        defaults.set(id, forKey: Self.activeKey)
    }

    var active: PairedComputer? {
        computers.first { $0.id == activeID } ?? computers.first
    }

    /// A random id for this phone, made once. Sent when pairing, and used to
    /// derive the comparison number both screens show.
    var deviceID: String {
        if let id = defaults.string(forKey: Self.deviceKey) { return id }
        let id = UUID().uuidString
        defaults.set(id, forKey: Self.deviceKey)
        return id
    }

    func add(_ computer: PairedComputer, token: String) {
        Keychain.save(token, for: computer.id)
        computers.removeAll { $0.id == computer.id }
        computers.append(computer)
        setActive(computer.id)
        persist()
    }

    /// Forget a computer on this phone (its token is deleted too).
    func remove(_ id: String) {
        Keychain.delete(for: id)
        computers.removeAll { $0.id == id }
        if activeID == id { setActive(computers.first?.id) }
        persist()
    }

    func token(for id: String) -> String? { Keychain.token(for: id) }

    private func persist() {
        if let data = try? JSONEncoder().encode(computers) {
            defaults.set(data, forKey: Self.listKey)
        }
    }
}
