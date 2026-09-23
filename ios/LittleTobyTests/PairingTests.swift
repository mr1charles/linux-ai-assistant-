import XCTest
@testable import LittleToby

final class PairingTests: XCTestCase {
    /// The same vector tests/test_remote_bridge.py checks against
    /// remote_bridge.compare_code(), so both sides show the same number.
    func testComparisonNumberMatchesTheComputer() {
        XCTAssertEqual(PairingLink.compareCode(code: "K7M4XQ2P", deviceID: "0F8C2D1E-7A3B-4C5D-9E6F-102938475601"),
                       "068302")
    }

    func testComparisonNumberIsAlwaysSixDigits() {
        for i in 0..<200 {
            XCTAssertEqual(PairingLink.compareCode(code: "ABCDEFGH", deviceID: "device-\(i)").count, 6)
        }
    }

    func testParsesTheQRCodeLink() {
        let link = PairingLink.parse("https://laptop.tail1234.ts.net/#pair=K7M4XQ2P")
        XCTAssertEqual(link?.code, "K7M4XQ2P")
        XCTAssertEqual(link?.baseURL.absoluteString, "https://laptop.tail1234.ts.net")
    }

    func testRejectsOtherQRCodes() {
        XCTAssertNil(PairingLink.parse("https://example.com/"))
        XCTAssertNil(PairingLink.parse("https://example.com/#pair=SHORT"))
        XCTAssertNil(PairingLink.parse("ftp://example.com/#pair=K7M4XQ2P"))
        XCTAssertNil(PairingLink.parse("not a link"))
    }

    func testTypedCodesAreForgiving() {
        XCTAssertEqual(PairingLink.normalize(" k7m4-xq2p "), "K7M4XQ2P")
        XCTAssertEqual(PairingLink.normalize("0O1IL"), "", "characters that are easy to misread are never part of a code")
    }

    func testTypedAddresses() {
        XCTAssertEqual(PairingLink.baseURL(fromTyped: "laptop.tail1234.ts.net")?.absoluteString,
                       "https://laptop.tail1234.ts.net")
        XCTAssertEqual(PairingLink.baseURL(fromTyped: "http://192.168.1.20:8765/")?.absoluteString,
                       "http://192.168.1.20:8765")
        XCTAssertNil(PairingLink.baseURL(fromTyped: ""))
        XCTAssertNil(PairingLink.baseURL(fromTyped: "ftp://x"))
    }
}
