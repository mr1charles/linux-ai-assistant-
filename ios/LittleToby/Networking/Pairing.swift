import CryptoKit
import Foundation
import Observation

/// What a pairing QR code (or a typed address and code) comes down to.
struct PairingLink: Equatable {
    var baseURL: URL
    var code: String

    /// Pairing codes use characters that are hard to misread: no 0/O, 1/I/L.
    static let alphabet = Set("23456789ABCDEFGHJKMNPQRSTUVWXYZ")

    static func normalize(_ text: String) -> String {
        String(text.uppercased().filter { alphabet.contains($0) })
    }

    /// Reads the link the computer's QR code carries:
    /// https://computer.tailnet.ts.net/#pair=K7M4XQ2P
    static func parse(_ text: String) -> PairingLink? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), let scheme = url.scheme?.lowercased(),
              scheme == "https" || scheme == "http", url.host != nil,
              let fragment = url.fragment, fragment.hasPrefix("pair=") else { return nil }
        let code = normalize(String(fragment.dropFirst(5)))
        guard code.count == 8 else { return nil }
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.fragment = nil
        components.path = ""
        components.query = nil
        guard let base = components.url else { return nil }
        return PairingLink(baseURL: base, code: code)
    }

    /// An address typed by hand: "laptop.tail1234.ts.net" becomes https://…,
    /// an address with a port or on the local network keeps what was typed.
    static func baseURL(fromTyped text: String) -> URL? {
        var t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return nil }
        if !t.contains("://") { t = "https://" + t }
        guard var components = URLComponents(string: t), components.host?.isEmpty == false,
              components.scheme == "https" || components.scheme == "http" else { return nil }
        components.path = ""
        components.fragment = nil
        components.query = nil
        return components.url
    }

    /// The six digits both screens show while pairing — the same function as
    /// compare_code() in scripts/remote_bridge.py (HMAC-SHA256 of the device
    /// id, keyed with the pairing code; first four bytes, mod a million).
    static func compareCode(code: String, deviceID: String) -> String {
        let key = SymmetricKey(data: Data(code.utf8))
        let mac = HMAC<SHA256>.authenticationCode(for: Data(deviceID.utf8), using: key)
        let bytes = Array(mac.prefix(4))
        let value = (UInt32(bytes[0]) << 24) | (UInt32(bytes[1]) << 16) | (UInt32(bytes[2]) << 8) | UInt32(bytes[3])
        let number = Int(value % 1_000_000)
        let digits = String(number)
        return String(repeating: "0", count: max(0, 6 - digits.count)) + digits
    }
}

/// Walks one pairing through: claim the code, show the number, wait for the
/// computer to approve, keep the token.
@MainActor
@Observable
final class PairingFlow {
    enum Step: Equatable {
        case idle
        case claiming
        case waiting(compare: String, computerName: String)
        case paired(computerName: String)
        case failed(String)
    }

    private(set) var step: Step = .idle
    private var task: Task<Void, Never>?

    func start(link: PairingLink, deviceName: String, store: ComputerStore,
               onPaired: @escaping (PairedComputer) -> Void) {
        task?.cancel()
        step = .claiming
        let deviceID = store.deviceID
        task = Task {
            let client = TobyClient(baseURL: link.baseURL)
            do {
                let claim: ClaimResponse = try await client.post("/api/pair/claim", [
                    "code": link.code, "device_name": deviceName, "device_id": deviceID, "platform": "ios",
                ], auth: false)
                let expected = PairingLink.compareCode(code: link.code, deviceID: deviceID)
                guard claim.compare == expected else {
                    // Something other than your computer answered. Stop here.
                    step = .failed("The computer's number doesn't match this phone's. Don't pair; start again on the computer.")
                    return
                }
                step = .waiting(compare: expected, computerName: claim.computer.name)
                let deadline = Date().addingTimeInterval(TimeInterval(claim.expiresIn ?? 300))
                while Date() < deadline {
                    try Task.checkCancellation()
                    let result: PairWaitResponse
                    do {
                        result = try await client.get("/api/pair/wait",
                                                      query: [URLQueryItem(name: "claim", value: claim.claim)],
                                                      timeout: 30, auth: false)
                    } catch TobyError.computerUnreachable {
                        try await Task.sleep(nanoseconds: 2_000_000_000)
                        continue
                    }
                    switch result.status {
                    case "pending":
                        continue
                    case "approved":
                        guard let token = result.token else {
                            step = .failed("The computer approved, but the answer got lost on the way. Pair again.")
                            return
                        }
                        let computer = PairedComputer(id: claim.computer.id, name: claim.computer.name,
                                                      baseURL: link.baseURL, pairedAt: Date())
                        do {
                            try store.add(computer, token: token)
                        } catch let error as Keychain.SaveError {
                            step = .failed("The computer approved, but this phone couldn't keep the key in its "
                                           + "Keychain (error \(error.status)). Pair again.")
                            return
                        }
                        step = .paired(computerName: claim.computer.name)
                        onPaired(computer)
                        return
                    case "denied":
                        step = .failed("The computer said no.")
                        return
                    case "expired":
                        step = .failed("That took too long. Start pairing again on the computer.")
                        return
                    default:
                        step = .failed("The computer restarted while pairing. Start again on the computer.")
                        return
                    }
                }
                step = .failed("That took too long. Start pairing again on the computer.")
            } catch is CancellationError {
                step = .idle
            } catch let error as TobyError {
                switch error {
                case .server(403, _):
                    step = .failed("That code didn't work. Codes work once, for five minutes; start pairing again on the computer.")
                default:
                    step = .failed(error.message)
                }
            } catch {
                step = .failed("Pairing didn't work. Try again.")
            }
        }
    }

    func cancel() {
        task?.cancel()
        task = nil
        step = .idle
    }
}
