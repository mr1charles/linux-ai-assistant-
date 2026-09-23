import BackgroundTasks
import Foundation
import UserNotifications

/// Notifications from the computer.
///
/// While the app is open, events arrive over the live connection and show
/// as a banner inside the app. When it's in the background, iOS decides how
/// often the app may check in (Background App Refresh), so a notification
/// can come late; for ones that always arrive at once, the computer can also
/// send them through ntfy (see docs/COMPANION.md). Proper push to this app
/// needs a paid Apple Developer account and a push key on the computer.
final class Notifier {
    static let shared = Notifier()
    static let refreshID = "com.littletoby.app.refresh"

    /// What each kind of event is called in Settings.
    static let categories: [(kind: String, title: String)] = [
        ("task_done", "A task you started from here finished"),
        ("task_failed", "A task didn't finish"),
        ("needs_permission", "Toby needs your OK"),
        ("job_done", "A build, test or download finished"),
        ("job_failed", "A build, test or download failed"),
        ("power", "The computer is going to sleep"),
        ("device", "A phone was paired or unpaired"),
    ]

    private let defaults = UserDefaults.standard

    var enabled: Bool {
        get { defaults.bool(forKey: "toby.notify.enabled") }
        set { defaults.set(newValue, forKey: "toby.notify.enabled") }
    }

    func wants(_ kind: String) -> Bool {
        defaults.object(forKey: "toby.notify.\(kind)") as? Bool ?? true
    }

    func setWants(_ kind: String, _ on: Bool) {
        defaults.set(on, forKey: "toby.notify.\(kind)")
    }

    func requestAuthorization() async -> Bool {
        (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    static func lastEventKey(_ computerID: String) -> String { "toby.lastEvent.\(computerID)" }

    func scheduleRefresh() {
        guard enabled else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Runs when iOS gives the app a moment in the background: ask each
    /// paired computer for events since the last one seen, and notify.
    func checkInBackground() async {
        defer { scheduleRefresh() }
        guard enabled else { return }
        let store = ComputerStore()
        for computer in store.computers {
            guard let token = store.token(for: computer.id) else { continue }
            let client = TobyClient(baseURL: computer.baseURL, token: token)
            let key = Self.lastEventKey(computer.id)
            let after = defaults.integer(forKey: key)
            guard let response: EventsResponse = try? await client.get(
                "/api/events", query: [URLQueryItem(name: "after", value: String(after))], timeout: 10) else { continue }
            for event in response.events where event.id > after && wants(event.kind) {
                post(event, from: computer.name)
            }
            defaults.set(max(after, response.last), forKey: key)
        }
    }

    func post(_ event: TobyEvent, from computerName: String) {
        let content = UNMutableNotificationContent()
        content.title = event.title
        content.body = event.body.isEmpty ? computerName : event.body
        content.threadIdentifier = computerName
        content.sound = event.kind == "needs_permission" ? .default : nil
        let request = UNNotificationRequest(identifier: "toby.\(computerName).\(event.id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
