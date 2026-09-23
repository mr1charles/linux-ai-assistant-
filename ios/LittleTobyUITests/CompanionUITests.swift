import XCTest

/// Drives the real app against a real Toby bridge running on the build
/// machine (ios/ci/test_desktop.py): pair by typing the code, check the
/// comparison number, ask for something that needs permission, allow it
/// from the phone, and watch it finish. Screenshots of every screen are
/// attached to the results, in light or dark depending on TOBY_APPEARANCE.
final class CompanionUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["TOBY_SERVER"] ?? "http://127.0.0.1:8765" }
    private var code: String { env["TOBY_PAIR_CODE"] ?? "K7M4XQ2P" }
    private var appearance: String { env["TOBY_APPEARANCE"] ?? "light" }

    override func setUp() {
        continueAfterFailure = false
    }

    func testPairAskApproveAndLook() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-uiTesting", "-uiTestReset", "-uiTestServer", server, "-uiTestAppearance", appearance]
        app.launch()

        // -- pairing --------------------------------------------------------------------
        let pair = app.buttons["welcome.pair"]
        XCTAssertTrue(pair.waitForExistence(timeout: 10))
        snap("01-welcome")
        pair.tap()
        let codeField = app.textFields["pair.code"]
        XCTAssertTrue(codeField.waitForExistence(timeout: 5))
        codeField.tap()
        codeField.typeText(code)
        snap("02-pairing")
        app.buttons["pair.submit"].tap()
        let compare = app.staticTexts["pair.compare"]
        XCTAssertTrue(compare.waitForExistence(timeout: 15), "the comparison number should show while the computer decides")
        snap("03-compare-number")
        let done = app.buttons["pair.done"]
        if !done.waitForExistence(timeout: 30) {
            snap("04-pairing-failed")
            XCTFail("the computer approved, so pairing should finish" + said(app, "pair.error"))
        }
        snap("04-paired")
        done.tap()

        // -- home, connected ------------------------------------------------------------------
        if !waitForText(app, containing: "Connected to", timeout: 20) {
            snap("05-not-connected")
            XCTFail("paired, so the phone should connect" + said(app, "home.headline") + said(app, "home.connectionCard"))
        }
        XCTAssertTrue(app.otherElements["home.status"].waitForExistence(timeout: 10)
                      || app.descendants(matching: .any)["home.status"].waitForExistence(timeout: 10))
        sleep(1)
        snap("05-home")

        // -- ask for something that needs permission ---------------------------------------------
        app.tabBars.buttons["Chat"].tap()
        let field = app.descendants(matching: .any)["chat.field"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText("run the tests in my project")
        app.buttons["chat.send"].tap()

        let allow = app.buttons["approval.allow"]
        XCTAssertTrue(allow.waitForExistence(timeout: 30), "running a command should ask the phone first")
        XCTAssertTrue(app.staticTexts["approval.title"].label.contains("npm test"))
        snap("06-permission")
        allow.tap()

        XCTAssertTrue(waitForText(app, containing: "tests passed", timeout: 60), "Toby's reply should arrive")
        sleep(1)
        snap("07-chat")

        app.tabBars.buttons["Home"].tap()
        sleep(1)
        snap("08-home-after-task")

        // -- tasks ------------------------------------------------------------------------------
        app.tabBars.buttons["Tasks"].tap()
        XCTAssertTrue(waitForText(app, containing: "run the tests", timeout: 15))
        snap("09-tasks")
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS[c] %@", "run the tests")).firstMatch.tap()
        XCTAssertTrue(waitForText(app, containing: "Run npm test", timeout: 10))
        sleep(1)
        snap("10-task-detail")

        // -- the computer -----------------------------------------------------------------------------
        app.tabBars.buttons["Computer"].tap()
        sleep(2)
        snap("11-computer-overview")
        app.buttons["Terminal"].tap()
        XCTAssertTrue(waitForText(app, containing: "passed", timeout: 10), "the command's real output should show")
        snap("12-terminal")
        app.buttons["Screen"].tap()
        XCTAssertTrue(waitForText(app, containing: "Screen view is off", timeout: 10),
                      "with screen view off on the computer, the phone says so rather than showing anything")
        snap("13-screen-off")

        // -- settings ------------------------------------------------------------------------------------
        app.tabBars.buttons["Settings"].tap()
        sleep(1)
        snap("14-settings")
    }

    // -- helpers -------------------------------------------------------------------------------------------
    private func snap(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = "\(appearance)-\(name)"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// What an element on screen says, for a failure message.
    private func said(_ app: XCUIApplication, _ identifier: String) -> String {
        let element = app.descendants(matching: .any)[identifier].firstMatch
        guard element.exists else { return "" }
        let texts = element.descendants(matching: .staticText).allElementsBoundByIndex.map(\.label)
        return " [\(identifier): \(([element.label] + texts).filter { !$0.isEmpty }.joined(separator: " / "))]"
    }

    private func waitForText(_ app: XCUIApplication, containing text: String, timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "label CONTAINS[c] %@", text)
        let match = app.descendants(matching: .any).matching(predicate).firstMatch
        return match.waitForExistence(timeout: timeout)
    }
}
