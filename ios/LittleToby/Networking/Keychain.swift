import Foundation
import Security

/// Each computer's token for this phone, in the iOS Keychain. It stays on
/// this device (never in backups or iCloud), and is readable after the first
/// unlock so background refresh can check for notifications.
enum Keychain {
    private static let service = "com.littletoby.app.tokens"

    /// Why a token couldn't be kept, with the Keychain's own status code.
    struct SaveError: Error, Equatable {
        let status: OSStatus
    }

    static func save(_ token: String, for computerID: String) throws {
        let data = Data(token.utf8)
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: computerID]
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add.merge(attributes) { _, new in new }
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw SaveError(status: status) }
    }

    static func token(for computerID: String) -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: computerID,
                                    kSecReturnData as String: true,
                                    kSecMatchLimit as String: kSecMatchLimitOne]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(for computerID: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: computerID]
        SecItemDelete(query as CFDictionary)
    }
}
