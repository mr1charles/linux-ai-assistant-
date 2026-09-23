import XCTest
@testable import LittleToby

final class APITests: XCTestCase {
    /// A state exactly as the computer publishes it (see _publish_remote_state
    /// in scripts/linux_agent_apple.py).
    func testDecodesTheLiveState() throws {
        let json = """
        {"version": 42, "busy": true,
         "task": {"id": "t1", "text": "check if my project builds", "origin": "phone:Jacob's iPhone",
                  "state": "needs_permission", "created": 1760000000.5, "ended": null, "error": null,
                  "steps": [{"label": "Run npm test", "status": "current", "detail": ""}], "reply": ""},
         "steps": [{"label": "Run npm test", "status": "current"}],
         "reply": "", "confirm": {"pending": true, "text": "Run: npm test"},
         "approvals": [{"id": "a1", "kind": "action", "level": "confirm", "title": "Run: npm test",
                        "details": ["In ~/project"], "reason": "running a command", "created": 1, "expires": 601}],
         "history": [{"role": "user", "content": "hi"}],
         "jobs": [{"id": "j1", "name": "npm test", "command": "npm test", "cwd": "/home/a/project",
                   "state": "running", "progress": 72.5, "started": 1, "ended": null, "exit_code": null,
                   "task_id": "t1", "watched": false, "attempts": 1, "last_line": "[3/4] ok"}],
         "work_mode": false, "power": "awake", "screen_view": false, "viewing": false,
         "computer": {"id": "c1", "name": "Jacob's Laptop", "api": 2},
         "device": {"id": "d1", "name": "iPhone", "platform": "ios", "paired_at": 1, "last_seen": null},
         "events_last": 7}
        """
        let state = try JSON.decoder.decode(TobyState.self, from: Data(json.utf8))
        XCTAssertEqual(state.version, 42)
        XCTAssertEqual(state.task?.taskState, .needsPermission)
        XCTAssertTrue(state.task?.fromPhone ?? false)
        XCTAssertEqual(state.approvals?.first?.title, "Run: npm test")
        XCTAssertEqual(state.jobs?.first?.progress, 72.5)
        XCTAssertEqual(state.jobs?.first?.lastLine, "[3/4] ok")
        XCTAssertEqual(state.computer?.name, "Jacob's Laptop")
        XCTAssertEqual(state.eventsLast, 7)
    }

    /// Values the computer couldn't read arrive as null and stay unknown.
    func testUnmeasuredStatusStaysUnknown() throws {
        let json = """
        {"computer": {"id": "c1", "name": "Mac", "api": 2}, "hostname": "runner", "os": null, "kernel": "24.0",
         "uptime_s": null, "cpu_percent": null, "memory": null, "battery": null, "disk": null, "load": [1.2, 1.0, 0.9],
         "sampled_at": 1760000000, "toby": {"busy": false, "model": "qwen3:4b", "power": "awake",
         "current_task": null, "work_mode": false}, "policy": {"screen_view": false, "restricted_actions": false},
         "jobs_running": 0}
        """
        let status = try JSON.decoder.decode(SystemStatus.self, from: Data(json.utf8))
        XCTAssertNil(status.cpuPercent)
        XCTAssertNil(status.battery)
        XCTAssertEqual(Format.percent(status.cpuPercent), "—")
        XCTAssertEqual(status.toby?.model, "qwen3:4b")
        XCTAssertEqual(status.policy?.screenView, false)
    }

    func testErrorsMeanSomethingToAPerson() {
        XCTAssertEqual(TobyClient.map(status: 401, body: Data()), .unpaired)
        XCTAssertEqual(TobyClient.map(status: 429, body: Data()), .locked)
        XCTAssertEqual(TobyClient.map(status: 502, body: Data()), .tobyNotRunning,
                       "Tailscale's proxy answers 502 when Toby isn't listening")
        XCTAssertEqual(TobyClient.map(status: 403, body: Data(#"{"error": "screen_view_off", "code": "screen_view_off"}"#.utf8)),
                       .screenViewOff)
        XCTAssertEqual(TobyClient.map(status: 501, body: Data(#"{"error": "Not here yet.", "code": "unavailable"}"#.utf8)),
                       .unavailable("Not here yet."))
        XCTAssertEqual(TobyClient.map(URLError(.notConnectedToInternet)) as? TobyError, .phoneOffline)
        XCTAssertEqual(TobyClient.map(URLError(.timedOut)) as? TobyError, .computerUnreachable)
        XCTAssertEqual(TobyClient.map(URLError(.cannotFindHost)) as? TobyError, .computerUnreachable)
    }

    func testFormatting() {
        XCTAssertEqual(Format.duration(8040), "2h 14m")
        XCTAssertEqual(Format.duration(59), "59s")
        XCTAssertEqual(Format.duration(nil), "—")
        let now = Date()
        XCTAssertEqual(Format.ago(now.addingTimeInterval(-2), now: now), "just now")
        XCTAssertEqual(Format.ago(now.addingTimeInterval(-125), now: now), "2m ago")
    }

    func testQuickActionsPersist() {
        let defaults = UserDefaults(suiteName: "toby.tests.\(UUID().uuidString)")!
        let store = QuickActionStore(defaults: defaults)
        XCTAssertEqual(store.sections, ["My computer", "Toby"])
        store.add(.ask("Start the server", "Run npm start in ~/project in the background", symbol: "sparkles", section: "Mine"))
        let reloaded = QuickActionStore(defaults: defaults)
        XCTAssertEqual(reloaded.actions.last?.prompt, "Run npm start in ~/project in the background")
        reloaded.reset()
        XCTAssertEqual(QuickActionStore(defaults: defaults).actions.count, QuickActionStore.standard.count)
    }
}
