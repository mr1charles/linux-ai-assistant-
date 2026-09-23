import SwiftUI

struct HomeView: View {
    @Environment(AppModel.self) private var model
    @State private var speech = SpeechRecognizer()
    @AppStorage("toby.voice.autosend") private var autoSend = true

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    ComputerHeader()
                    Button(action: toggleListening) {
                        TobyFace(mood: model.mood, size: 150, listening: speech.isListening)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("home.toby")
                    .accessibilityHint("Tap to talk to Toby")
                    headline
                    if model.connection.isProblem {
                        ConnectionCard(connection: model.connection)
                            .transition(.arrive)
                    }
                    if let approval = model.approvals.first {
                        ApprovalBanner(approval: approval)
                            .transition(.arrive)
                    }
                    if let task = model.currentTask, model.busy || recentlyFinished(task) {
                        NavigationLink(value: task.id) {
                            TaskCard(task: task, live: model.busy)
                        }
                        .buttonStyle(.plain)
                        .transition(.arrive)
                    }
                    StatusCard()
                    QuickActionsView()
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 32)
                .animation(Motion.enter, value: model.connection)
                .animation(Motion.enter, value: model.approvals.first?.id)
                .animation(Motion.enter, value: model.currentTask?.id)
            }
            .background(Theme.background.ignoresSafeArea())
            .refreshable { await model.refreshStatus() }
            .navigationDestination(for: String.self) { id in TaskDetailView(taskID: id) }
            .toolbar(.hidden, for: .navigationBar)
        }
    }

    @ViewBuilder private var headline: some View {
        VStack(spacing: 6) {
            Text(headlineText)
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.inkBright)
                .multilineTextAlignment(.center)
                .contentTransition(.opacity)
            if speech.isListening {
                Text(speech.transcript.isEmpty ? "Listening…" : speech.transcript)
                    .font(.body)
                    .foregroundStyle(Theme.inkNormal)
                    .multilineTextAlignment(.center)
            } else if let reply = model.state?.reply, !reply.isEmpty, !model.busy {
                Text(reply)
                    .font(.body)
                    .foregroundStyle(Theme.inkNormal)
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
            } else if let error = speech.error {
                Text(error).font(.footnote).foregroundStyle(Theme.warn).multilineTextAlignment(.center)
            }
        }
        .padding(.horizontal, 8)
        .accessibilityIdentifier("home.headline")
    }

    private var headlineText: String {
        let name = model.activeComputer?.name ?? "your computer"
        switch model.connection {
        case .online:
            if !model.approvals.isEmpty { return "Toby needs your OK" }
            return model.busy ? "Working on \(name)" : "Connected to \(name)"
        case .connecting: return "Connecting to \(name)…"
        default: return "\(name): \(model.connection.title)"
        }
    }

    private func recentlyFinished(_ task: TaskInfo) -> Bool {
        guard let ended = task.ended else { return false }
        return Date().timeIntervalSince1970 - ended < 120
    }

    private func toggleListening() {
        if speech.isListening {
            speech.stop()
            let said = speech.transcript
            if autoSend, !said.trimmingCharacters(in: .whitespaces).isEmpty {
                Task { await model.ask(said) }
            }
        } else {
            Task { await speech.start() }
        }
    }
}

/// The computer's name and connection, with a switcher when you have several.
struct ComputerHeader: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack {
            Menu {
                ForEach(model.computers.computers) { computer in
                    Button {
                        model.switchTo(computer.id)
                    } label: {
                        if computer.id == model.computers.activeID {
                            Label(computer.name, systemImage: "checkmark")
                        } else {
                            Text(computer.name)
                        }
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "desktopcomputer")
                    Text(model.activeComputer?.name ?? "No computer")
                        .font(.headline)
                    if model.computers.computers.count > 1 {
                        Image(systemName: "chevron.down").font(.caption.weight(.bold))
                    }
                }
                .foregroundStyle(Theme.inkBright)
            }
            .disabled(model.computers.computers.count < 2)
            Spacer()
            ConnectionPill(connection: model.connection, busy: model.busy)
        }
        .padding(.top, 8)
    }
}

struct ConnectionPill: View {
    var connection: Connection
    var busy: Bool

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(busy && connection == .online ? "Working" : connection.title)
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.inkNormal)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Theme.control, in: Capsule())
        .accessibilityIdentifier("home.connection")
        .animation(Motion.standard, value: connection)
    }

    private var color: Color {
        switch connection {
        case .online: return busy ? Theme.accent : Theme.ok
        case .connecting: return Theme.inkQuiet
        case .asleep, .unreachable, .phoneOffline: return Theme.warn
        default: return Theme.alert
        }
    }
}

/// Every connection problem explained, with the one thing that might help.
struct ConnectionCard: View {
    @Environment(AppModel.self) private var model
    var connection: Connection
    @State private var pairing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(connection.title, systemImage: symbol)
                .font(.headline)
                .foregroundStyle(Theme.inkBright)
            Text(connection.explanation)
                .font(.subheadline)
                .foregroundStyle(Theme.inkNormal)
            if connection == .unpaired {
                Button("Pair again") { pairing = true }
                    .buttonStyle(TobyButtonStyle())
            } else if connection != .phoneOffline {
                Button("Try again") { model.reconnect() }
                    .buttonStyle(TobyButtonStyle(kind: .secondary))
            }
        }
        .card()
        .sheet(isPresented: $pairing) { PairingView() }
        .accessibilityIdentifier("home.connectionCard")
    }

    private var symbol: String {
        switch connection {
        case .asleep: return "moon.zzz"
        case .phoneOffline: return "wifi.slash"
        case .unpaired: return "link.badge.plus"
        case .locked: return "lock"
        case .tobyNotRunning: return "power"
        default: return "exclamationmark.triangle"
        }
    }
}

struct ApprovalBanner: View {
    @Environment(AppModel.self) private var model
    var approval: Approval

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: approval.isRestricted ? "exclamationmark.shield" : "hand.raised")
                .font(.title2)
                .foregroundStyle(approval.isRestricted ? Theme.alert : Theme.warn)
            VStack(alignment: .leading, spacing: 2) {
                Text("Toby wants to").font(.caption).foregroundStyle(Theme.inkQuiet)
                Text(approval.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.inkBright)
                    .lineLimit(2)
            }
            Spacer()
            Image(systemName: "chevron.right").foregroundStyle(Theme.inkQuiet)
        }
        .card()
        .contentShape(Rectangle())
        .onTapGesture { NotificationCenter.default.post(name: .showApproval, object: approval.id) }
        .accessibilityIdentifier("home.approvalBanner")
    }
}

extension Notification.Name {
    static let showApproval = Notification.Name("toby.showApproval")
}
