import SwiftUI

/// The live task: what Toby is doing on the computer, step by step.
struct TaskCard: View {
    @Environment(AppModel.self) private var model
    var task: TaskInfo
    var live: Bool

    private var steps: [TaskStep] { task.steps ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(task.text)
                    .font(.headline)
                    .foregroundStyle(Theme.inkBright)
                    .lineLimit(2)
                Spacer(minLength: 8)
                StatePill(state: task.taskState)
            }
            if !steps.isEmpty {
                ProgressView(value: Double(steps.filter { $0.status == "done" }.count),
                             total: Double(max(steps.count, 1)))
                    .tint(Theme.accent)
                    .animation(Motion.move, value: steps.filter { $0.status == "done" }.count)
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(steps.prefix(6).enumerated()), id: \.offset) { _, step in
                        StepRow(step: step)
                    }
                    if steps.count > 6 {
                        Text("and \(steps.count - 6) more").font(.caption).foregroundStyle(Theme.inkQuiet)
                    }
                }
            }
            if live {
                HStack(spacing: 10) {
                    Button(task.taskState == .paused ? "Resume" : "Pause") {
                        Task { await model.control(task.taskState == .paused ? "resume" : "pause") }
                    }
                    .buttonStyle(TobyButtonStyle(kind: .secondary))
                    .accessibilityIdentifier("task.pause")
                    Button("Stop") { Task { await model.control("stop") } }
                        .buttonStyle(TobyButtonStyle(kind: .destructive))
                        .accessibilityIdentifier("task.stop")
                }
            }
        }
        .card()
        .accessibilityIdentifier("home.taskCard")
    }
}

struct StepRow: View {
    var step: TaskStep
    var showDetail = true

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            icon.frame(width: 18, height: 18).padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(step.label)
                    .font(.subheadline.weight(step.status == "current" ? .semibold : .regular))
                    .foregroundStyle(step.status == "done" ? Theme.inkQuiet : Theme.inkBright)
                    .strikethrough(step.status == "done", color: Theme.inkQuiet)
                if showDetail, let detail = step.detail, !detail.isEmpty, step.status == "done" || step.status == "error" {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(step.status == "error" ? Theme.alert : Theme.inkQuiet)
                        .lineLimit(3)
                }
            }
        }
        .animation(Motion.standard, value: step.status)
    }

    @ViewBuilder private var icon: some View {
        switch step.status {
        case "done":
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.ok)
        case "current":
            Image(systemName: "circle.fill").foregroundStyle(Theme.accent)
                .symbolEffect(.pulse, options: .repeating)
        case "error":
            Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.alert)
        default:
            Image(systemName: "circle").foregroundStyle(Theme.inkQuiet)
        }
    }
}

struct StatePill: View {
    var state: TaskState?

    var body: some View {
        Text(state?.label ?? "")
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(color.opacity(0.14), in: Capsule())
            .accessibilityIdentifier("task.state")
    }

    private var color: Color {
        switch state {
        case .completed?: return Theme.ok
        case .failed?, .cancelled?: return Theme.alert
        case .needsPermission?, .paused?: return Theme.warn
        default: return Theme.accent
        }
    }
}

/// The computer's real, measured status. A value the computer couldn't
/// read shows as a dash, never as a made-up number.
struct StatusCard: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let status = model.status
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.activeComputer?.name ?? "Computer").font(.headline).foregroundStyle(Theme.inkBright)
                    Text(status?.os ?? "Operating system not reported")
                        .font(.caption).foregroundStyle(Theme.inkQuiet)
                }
                Spacer()
                if let power = status?.toby?.power, power != "awake" {
                    Text(power == "sleeping" ? "Going to sleep" : "Shutting down")
                        .font(.caption.weight(.semibold)).foregroundStyle(Theme.warn)
                }
            }
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 12) {
                Metric(title: "CPU", value: Format.percent(status?.cpuPercent), symbol: "cpu")
                Metric(title: "Memory", value: Format.percent(status?.memory?.percent), symbol: "memorychip")
                Metric(title: "Battery", value: batteryText(status?.battery), symbol: batterySymbol(status?.battery))
                Metric(title: "Up for", value: Format.duration(uptime(status)), symbol: "clock")
            }
            if let task = status?.toby?.currentTask {
                Label("Working on: \(task)", systemImage: "gearshape.2")
                    .font(.footnote).foregroundStyle(Theme.inkNormal).lineLimit(2)
            }
            if let running = status?.jobsRunning, running > 0 {
                Label("\(running) background job\(running == 1 ? "" : "s") running", systemImage: "hammer")
                    .font(.footnote).foregroundStyle(Theme.inkNormal)
            }
            TimelineView(.periodic(from: .now, by: 5)) { context in
                Text(footer(status, now: context.date))
                    .font(.caption2).foregroundStyle(Theme.inkQuiet)
            }
        }
        .card()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("home.status")
    }

    private func uptime(_ status: SystemStatus?) -> Double? {
        guard let seconds = status?.uptimeS else { return nil }
        return Double(seconds)
    }

    private func footer(_ status: SystemStatus?, now: Date) -> String {
        guard model.statusUpdated != nil else { return "Waiting for the computer…" }
        var text = "Updated " + Format.ago(model.statusUpdated, now: now)
        if let name = status?.toby?.model { text += " · " + name }
        return text
    }

    private func batteryText(_ battery: Battery?) -> String {
        guard let battery, let percent = battery.percent else { return "—" }
        return "\(percent)%" + (battery.charging == true ? ", charging" : "")
    }

    private func batterySymbol(_ battery: Battery?) -> String {
        guard let percent = battery?.percent else { return "powerplug" }
        if battery?.charging == true { return "battery.100.bolt" }
        switch percent {
        case ..<15: return "battery.0"
        case ..<40: return "battery.25"
        case ..<65: return "battery.50"
        case ..<90: return "battery.75"
        default: return "battery.100"
        }
    }
}

struct Metric: View {
    var title: String
    var value: String
    var symbol: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.body)
                .foregroundStyle(Theme.accent)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 0) {
                Text(value)
                    .font(.title3.weight(.semibold).monospacedDigit())
                    .foregroundStyle(Theme.inkBright)
                    .contentTransition(.numericText())
                    .animation(Motion.standard, value: value)
                Text(title).font(.caption).foregroundStyle(Theme.inkQuiet)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// One-tap shortcuts. Each asks Toby or calls the computer directly.
struct QuickActionsView: View {
    @Environment(AppModel.self) private var model
    @State private var editing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Quick actions").font(.headline).foregroundStyle(Theme.inkBright)
                Spacer()
                Button("Edit") { editing = true }.font(.subheadline)
            }
            ForEach(model.quickActions.sections, id: \.self) { section in
                VStack(alignment: .leading, spacing: 8) {
                    Text(section.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(Theme.inkQuiet)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 10)], spacing: 10) {
                        ForEach(model.quickActions.actions.filter { $0.section == section }) { action in
                            QuickActionTile(action: action)
                        }
                    }
                }
            }
        }
        .sheet(isPresented: $editing) { NavigationStack { QuickActionsEditor() } }
    }
}

struct QuickActionTile: View {
    @Environment(AppModel.self) private var model
    @AppStorage("toby.haptics") private var haptics = true
    var action: QuickAction
    @State private var tapped = 0

    var body: some View {
        Button {
            tapped += 1
            perform()
        } label: {
            VStack(spacing: 8) {
                Image(systemName: action.symbol).font(.title3)
                Text(action.title).font(.caption.weight(.medium)).multilineTextAlignment(.center).lineLimit(2)
            }
            .frame(maxWidth: .infinity, minHeight: 76)
            .foregroundStyle(Theme.inkBright)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.line, lineWidth: 0.5))
        }
        .buttonStyle(PressableStyle())
        .disabled(disabled)
        .opacity(disabled ? 0.45 : 1)
        .sensoryFeedback(trigger: tapped) { _, _ -> SensoryFeedback? in
            haptics ? SensoryFeedback.impact(weight: .light) : nil
        }
        .accessibilityIdentifier("quick.\(action.kind.rawValue).\(action.title)")
    }

    private var disabled: Bool {
        switch action.kind {
        case .pause, .stop: return !model.busy
        case .reconnect, .tasks, .computer: return false
        default: return model.connection != .online || model.busy
        }
    }

    private func perform() {
        switch action.kind {
        case .ask:
            if let prompt = action.prompt { Task { await model.ask(prompt) } }
        case .status:
            Task {
                await model.refreshStatus()
                model.showToast("Status updated")
            }
        case .tasks: model.selectedTab = .tasks
        case .computer: model.selectedTab = .computer
        case .pause: Task { await model.control(model.currentTask?.taskState == .paused ? "resume" : "pause") }
        case .stop: Task { await model.control("stop") }
        case .reconnect: model.reconnect()
        case .continueTask:
            Task {
                if model.recentTasks.isEmpty { await model.refreshTasks() }
                if let last = model.recentTasks.first(where: { $0.taskState != .completed }) ?? model.recentTasks.first {
                    await model.ask("Continue the task I was doing earlier: \(last.text)")
                } else {
                    await model.ask("Continue what I was doing earlier")
                }
            }
        }
    }
}

struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(Motion.standard, value: configuration.isPressed)
    }
}
