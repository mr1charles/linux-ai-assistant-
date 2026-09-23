import Foundation
import Network
import Observation
import SwiftUI
import UIKit

/// How the phone is connected to the computer right now, in words the app
/// can show. Every state that isn't "online" says what happened and what to do.
enum Connection: Equatable {
    case notPaired, connecting, online, asleep, unreachable, phoneOffline, tobyNotRunning, unpaired, locked

    var title: String {
        switch self {
        case .notPaired: return "Not paired"
        case .connecting: return "Connecting"
        case .online: return "Connected"
        case .asleep: return "Asleep"
        case .unreachable: return "Can't reach it"
        case .phoneOffline: return "You're offline"
        case .tobyNotRunning: return "Toby isn't running"
        case .unpaired: return "Unpaired"
        case .locked: return "Locked for a minute"
        }
    }

    var explanation: String {
        switch self {
        case .notPaired: return "Pair a computer to start."
        case .connecting: return "Reaching your computer…"
        case .online: return ""
        case .asleep: return "Your computer went to sleep. Wake it (open the lid or press a key) and Toby will reconnect by itself."
        case .unreachable: return "It may be asleep, turned off, or offline. Check Tailscale is on on both this phone and the computer."
        case .phoneOffline: return "This phone has no internet connection. Toby will reconnect when it's back."
        case .tobyNotRunning: return "The computer is reachable, but Toby isn't running or its phone connection is off. On the computer, run: toby start"
        case .unpaired: return "This phone was unpaired from the computer. Pair it again to keep using Toby."
        case .locked: return "Too many wrong attempts from this network. Wait a minute."
        }
    }

    var isProblem: Bool { self != .online && self != .connecting }
}

/// Toby's expression on the phone, following what's happening on the computer.
enum TobyMood: Equatable {
    case sleepy, idle, happy, thinking, focused, waiting, celebrating, concerned
}

enum Tab: Hashable { case home, chat, tasks, computer, settings }

/// Everything the screens show, and every action they can take, for the
/// computer currently in use.
@MainActor
@Observable
final class AppModel {
    let computers: ComputerStore
    let quickActions = QuickActionStore()

    private(set) var connection: Connection = .connecting
    private(set) var state: TobyState?
    private(set) var status: SystemStatus?
    private(set) var statusUpdated: Date?
    private(set) var recentTasks: [TaskInfo] = []
    private(set) var toast: String?
    private(set) var celebrateAt: Date?
    private(set) var lastFailureAt: Date?
    private(set) var phoneOnline = true
    /// A message sent from Chat that the computer hasn't echoed back yet.
    private(set) var sending: String?
    var selectedTab: Tab = .home

    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var statusTask: Task<Void, Never>?
    @ObservationIgnored private var cachedClient: (id: String, token: String, client: TobyClient)?
    @ObservationIgnored private let pathMonitor = NWPathMonitor()

    init(computers: ComputerStore = ComputerStore()) {
        self.computers = computers
        pathMonitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            Task { @MainActor in self?.phoneOnlineChanged(online) }
        }
        pathMonitor.start(queue: DispatchQueue(label: "toby.path"))
    }

    var activeComputer: PairedComputer? { computers.active }
    var busy: Bool { state?.busy ?? false }
    var currentTask: TaskInfo? { state?.task }
    var approvals: [Approval] { state?.approvals ?? [] }

    var client: TobyClient? {
        guard let computer = computers.active, let token = computers.token(for: computer.id) else { return nil }
        if let cached = cachedClient, cached.id == computer.id, cached.token == token { return cached.client }
        let client = TobyClient(baseURL: computer.baseURL, token: token)
        cachedClient = (computer.id, token, client)
        return client
    }

    var mood: TobyMood {
        switch connection {
        case .online: break
        case .connecting: return .idle
        case .asleep, .unreachable: return .sleepy
        default: return .concerned
        }
        if let at = celebrateAt, Date().timeIntervalSince(at) < 2.4 { return .celebrating }
        if !approvals.isEmpty { return .waiting }
        guard busy, let task = currentTask else {
            if let at = lastFailureAt, Date().timeIntervalSince(at) < 20 { return .concerned }
            return .happy
        }
        switch task.taskState {
        case .thinking?: return .thinking
        case .needsPermission?: return .waiting
        case .paused?: return .idle
        default: return .focused
        }
    }

    // -- polling ----------------------------------------------------------------------------
    func startPolling() {
        guard computers.active != nil else {
            connection = .notPaired
            return
        }
        if pollTask == nil {
            pollTask = Task { [weak self] in await self?.pollLoop() }
        }
        if statusTask == nil {
            statusTask = Task { [weak self] in await self?.statusLoop() }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
        statusTask?.cancel()
        statusTask = nil
    }

    func reconnect() {
        stopPolling()
        connection = .connecting
        startPolling()
    }

    private func phoneOnlineChanged(_ online: Bool) {
        let cameBack = online && !phoneOnline
        phoneOnline = online
        if !online {
            connection = .phoneOffline
        } else if cameBack {
            reconnect()
        }
    }

    private func pollLoop() async {
        var since: Int?
        var backoff: UInt64 = 1
        while !Task.isCancelled {
            guard let client else {
                connection = .notPaired
                pollTask = nil
                return
            }
            if !phoneOnline {
                connection = .phoneOffline
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                continue
            }
            do {
                var query: [URLQueryItem] = []
                if let since { query.append(URLQueryItem(name: "since", value: String(since))) }
                let next: TobyState = try await client.get("/api/state", query: query, timeout: 35)
                apply(next)
                since = next.version
                backoff = 1
            } catch is CancellationError {
                return
            } catch let error as TobyError {
                handle(error)
                if error == .unpaired {
                    pollTask = nil
                    return
                }
                since = nil     // after a gap, fetch the whole state again
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 15)
            } catch {
                connection = .unreachable
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 15)
            }
        }
    }

    private func statusLoop() async {
        while !Task.isCancelled {
            await refreshStatus()
            try? await Task.sleep(nanoseconds: 20_000_000_000)
        }
    }

    func refreshStatus() async {
        guard let client else { return }
        do {
            status = try await client.get("/api/status", timeout: 10)
            statusUpdated = Date()
        } catch {
            // the poll loop reports connection problems; keep the last reading
        }
    }

    private func handle(_ error: TobyError) {
        switch error {
        case .phoneOffline: connection = .phoneOffline
        case .computerUnreachable: connection = state?.power == "sleeping" ? .asleep : .unreachable
        case .tobyNotRunning: connection = .tobyNotRunning
        case .unpaired: connection = .unpaired
        case .locked: connection = .locked
        default: connection = .unreachable
        }
    }

    private func apply(_ next: TobyState) {
        let previous = state
        state = next
        connection = next.power == "sleeping" ? .asleep : .online

        let before: TaskState? = previous?.task?.taskState
        if let task = next.task, let now = task.taskState, now.isFinished,
           previous?.task?.id != task.id || before?.isFinished == false {
            if previous != nil {
                if now == .completed { celebrateAt = Date() }
                if now == .failed { lastFailureAt = Date() }
            }
            Task { await refreshTasks() }
        }
        if let sending, next.history?.contains(where: { $0.role == "user" && $0.content == sending }) == true {
            self.sending = nil
        }
        if let last = next.eventsLast, last > lastEventID {
            Task { await fetchEvents() }
        }
    }

    // -- notifications -------------------------------------------------------------------------
    private var lastEventID: Int {
        get { UserDefaults.standard.integer(forKey: "toby.lastEvent.\(computers.activeID ?? "")") }
        set { UserDefaults.standard.set(newValue, forKey: "toby.lastEvent.\(computers.activeID ?? "")") }
    }

    private func fetchEvents() async {
        guard let client else { return }
        do {
            let response: EventsResponse = try await client.get(
                "/api/events", query: [URLQueryItem(name: "after", value: String(lastEventID))])
            let fresh = response.events.filter { $0.id > lastEventID }
            lastEventID = max(lastEventID, response.last)
            if let newest = fresh.last(where: { Notifier.shared.wants($0.kind) }) {
                showToast(newest.title)
            }
        } catch {
            // next state change will try again
        }
    }

    func showToast(_ text: String) {
        withAnimation(Motion.enter) { toast = text }
        let shown = text
        Task {
            try? await Task.sleep(nanoseconds: 3_500_000_000)
            if toast == shown { withAnimation(Motion.exit) { toast = nil } }
        }
    }

    // -- actions ---------------------------------------------------------------------------------
    @discardableResult
    func ask(_ text: String) async -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let client else { return false }
        sending = trimmed
        do {
            let response: AskResponse = try await client.post("/api/ask", ["text": trimmed])
            if !response.ok {
                sending = nil
                showToast(response.message ?? "Toby couldn't take that right now.")
            }
            return response.ok
        } catch let error as TobyError {
            sending = nil
            if case .server(409, _) = error {
                showToast("Toby is still busy with the last thing.")
            } else {
                showToast(error.message)
            }
            return false
        } catch {
            sending = nil
            return false
        }
    }

    func approve(_ approval: Approval, allow: Bool) async {
        guard let client else { return }
        do {
            let _: OKResponse = try await client.post("/api/approve", ["id": approval.id, "allow": allow])
        } catch let error as TobyError {
            showToast(error.message)
        } catch {}
    }

    func control(_ action: String) async {
        guard let client else { return }
        do {
            let response: OKResponse = try await client.post("/api/task/\(action)")
            if let message = response.message, !response.ok { showToast(message) }
        } catch let error as TobyError {
            showToast(error.message)
        } catch {}
    }

    func setWorkMode(_ on: Bool) async {
        guard let client else { return }
        do {
            let _: OKResponse = try await client.post("/api/work_mode", ["on": on])
        } catch let error as TobyError {
            showToast(error.message)
        } catch {}
    }

    func focus(_ window: WindowInfo) async -> Bool {
        guard let client else { return false }
        do {
            let response: OKResponse = try await client.post("/api/window/focus", ["address": window.address])
            if let message = response.message { showToast(message) }
            return response.ok
        } catch let error as TobyError {
            showToast(error.message)
            return false
        } catch {
            return false
        }
    }

    func refreshTasks() async {
        guard let client else { return }
        if let response: TasksResponse = try? await client.get("/api/tasks") {
            recentTasks = response.tasks
        }
    }

    func task(_ id: String) async throws -> TaskInfo {
        guard let client else { throw TobyError.unpaired }
        return try await client.get("/api/tasks/\(id)")
    }

    func job(_ id: String, lines: Int = 300) async throws -> JobInfo {
        guard let client else { throw TobyError.unpaired }
        return try await client.get("/api/jobs/\(id)", query: [URLQueryItem(name: "lines", value: String(lines))])
    }

    func jobs() async throws -> [JobInfo] {
        guard let client else { throw TobyError.unpaired }
        let response: JobsResponse = try await client.get("/api/jobs")
        return response.jobs
    }

    func overview() async throws -> Overview {
        guard let client else { throw TobyError.unpaired }
        return try await client.get("/api/overview")
    }

    func files() async throws -> [FileTouch] {
        guard let client else { throw TobyError.unpaired }
        let response: FilesResponse = try await client.get("/api/files")
        return response.files
    }

    func screen(view: String, maxWidth: Int) async throws -> Data {
        guard let client else { throw TobyError.unpaired }
        return try await client.data("/api/screen", query: [URLQueryItem(name: "view", value: view),
                                                            URLQueryItem(name: "max", value: String(maxWidth))])
    }

    func devices() async throws -> [DeviceInfo] {
        guard let client else { throw TobyError.unpaired }
        let response: DevicesResponse = try await client.get("/api/devices")
        return response.devices
    }

    func revoke(_ device: DeviceInfo) async throws {
        guard let client else { throw TobyError.unpaired }
        let _: OKResponse = try await client.post("/api/devices/revoke", ["id": device.id])
    }

    /// Unpair this phone on the computer (so its token stops working
    /// everywhere), then forget the computer here.
    func logOut() async {
        guard let computer = computers.active else { return }
        if let client {
            let _: OKResponse? = try? await client.post("/api/logout")
        }
        forget(computer.id)
    }

    /// Forget a computer on this phone only.
    func forget(_ id: String) {
        stopPolling()
        computers.remove(id)
        cachedClient = nil
        state = nil
        status = nil
        recentTasks = []
        startPolling()
    }

    func switchTo(_ id: String) {
        guard id != computers.activeID else { return }
        stopPolling()
        computers.setActive(id)
        cachedClient = nil
        state = nil
        status = nil
        recentTasks = []
        connection = .connecting
        startPolling()
    }

    func paired(_ computer: PairedComputer) {
        cachedClient = nil
        state = nil
        status = nil
        selectedTab = .home
        reconnect()
    }
}
