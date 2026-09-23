import XCTest
@testable import LittleToby

/// The pairing token has to survive in the Keychain, or the phone would
/// say "Paired" and then never connect.
final class KeychainTests: XCTestCase {
    private let computerID = "keychain-test-\(UUID().uuidString)"

    override func tearDown() {
        Keychain.delete(for: computerID)
    }

    func testTokenRoundTrip() throws {
        XCTAssertNil(Keychain.token(for: computerID))
        try Keychain.save("first-token", for: computerID)
        XCTAssertEqual(Keychain.token(for: computerID), "first-token")
        try Keychain.save("second-token", for: computerID)
        XCTAssertEqual(Keychain.token(for: computerID), "second-token", "saving again replaces the token")
        Keychain.delete(for: computerID)
        XCTAssertNil(Keychain.token(for: computerID))
    }

    func testAddingAComputerKeepsItsToken() throws {
        let defaults = UserDefaults(suiteName: "keychain-test-\(UUID().uuidString)")!
        let store = ComputerStore(defaults: defaults)
        let computer = PairedComputer(id: computerID, name: "Test Laptop",
                                      baseURL: URL(string: "https://laptop.tail1234.ts.net")!, pairedAt: Date())
        try store.add(computer, token: "a-token")
        XCTAssertEqual(store.active?.id, computerID)
        XCTAssertEqual(store.token(for: computerID), "a-token")
        store.remove(computerID)
        XCTAssertNil(store.token(for: computerID), "forgetting a computer deletes its token")
    }
}
