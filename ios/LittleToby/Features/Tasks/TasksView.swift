import SwiftUI

struct TasksView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationStack {
            List {
                if let task = model.currentTask, model.busy {
                    Section("Now") {
                        NavigationLink(value: task.id) { TaskRow(task: task) }
                    }
                }
                let earlier = model.recentTasks.filter { !(model.busy && $0.id == model.currentTask?.id) }
                if !earlier.isEmpty {
                    Section("Earlier") {
                        ForEach(earlier) { task in
                            NavigationLink(value: task.id) { TaskRow(task: task) }
                        }
                    }
                }
            }
            .overlay {
                if model.recentTasks.isEmpty && !model.busy {
                    ContentUnavailableView("No tasks yet", systemImage: "checklist",
                                           description: Text("Ask Toby to do something on your computer and it shows up here, with every step."))
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle("Tasks")
            .navigationDestination(for: String.self) { id in TaskDetailView(taskID: id) }
            .refreshable { await model.refreshTasks() }
            .task { await model.refreshTasks() }
        }
    }
}

struct TaskRow: View {
    var task: TaskInfo

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(task.text).font(.body.weight(.medium)).foregroundStyle(Theme.inkBright).lineLimit(2)
                Spacer()
                StatePill(state: task.taskState)
            }
            HStack(spacing: 6) {
                Image(systemName: task.fromPhone ? "iphone" : "desktopcomputer")
                Text(task.fromPhone ? "From your phone" : "At the computer")
                Text("·")
                Text(Format.ago(epoch: task.created))
                if let total = task.stepsTotal, total > 0 {
                    Text("·")
                    Text("\(task.stepsDone ?? 0) of \(total) steps")
                }
            }
            .font(.caption)
            .foregroundStyle(Theme.inkQuiet)
        }
        .padding(.vertical, 4)
        .listRowBackground(Theme.surface)
    }
}

/// One task in full: every step and what it found, the reply, the files it
/// touched and the jobs it started. Raw command output only when you ask.
struct TaskDetailView: View {
    @Environment(AppModel.self) private var model
    var taskID: String
    @State private var task: TaskInfo?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let task {
                    header(task)
                    if let steps = task.steps, !steps.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Steps").font(.headline).foregroundStyle(Theme.inkBright)
                            ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                                StepRow(step: step)
                            }
                        }
                        .card()
                    }
                    if let reply = task.reply, !reply.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Toby said").font(.headline).foregroundStyle(Theme.inkBright)
                            Text(reply).font(.body).foregroundStyle(Theme.inkNormal).textSelection(.enabled)
                        }
                        .card()
                    }
                    if let problem = task.error {
                        Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.warn).card()
                    }
                    if let files = task.files, !files.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Files").font(.headline).foregroundStyle(Theme.inkBright)
                            ForEach(files, id: \.path) { file in FileRow(file: file) }
                        }
                        .card()
                    }
                    if let jobs = task.jobDetails, !jobs.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Commands").font(.headline).foregroundStyle(Theme.inkBright)
                            ForEach(jobs) { job in JobOutputDisclosure(job: job) }
                        }
                        .card()
                    }
                } else if let error {
                    ContentUnavailableView("Couldn't load this task", systemImage: "exclamationmark.triangle",
                                           description: Text(error))
                } else {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
                }
            }
            .padding(16)
        }
        .background(Theme.background.ignoresSafeArea())
        .navigationTitle("Task")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: taskID) { await keepFresh() }
    }

    private func header(_ task: TaskInfo) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                StatePill(state: task.taskState)
                Spacer()
                Text(Format.ago(epoch: task.created)).font(.caption).foregroundStyle(Theme.inkQuiet)
            }
            Text(task.text).font(.title3.weight(.semibold)).foregroundStyle(Theme.inkBright)
            Text(task.fromPhone ? "Asked from \(task.origin?.replacingOccurrences(of: "phone:", with: "") ?? "a phone")"
                 : "Asked at the computer")
                .font(.caption).foregroundStyle(Theme.inkQuiet)
            if task.taskState?.isFinished == false {
                HStack(spacing: 10) {
                    Button(task.taskState == .paused ? "Resume" : "Pause") {
                        Task { await model.control(task.taskState == .paused ? "resume" : "pause") }
                    }
                    .buttonStyle(TobyButtonStyle(kind: .secondary))
                    Button("Stop") { Task { await model.control("stop") } }
                        .buttonStyle(TobyButtonStyle(kind: .destructive))
                }
                .padding(.top, 4)
            }
        }
        .card()
    }

    /// Load the task, and keep it current while it's still running.
    private func keepFresh() async {
        while !Task.isCancelled {
            do {
                let fresh = try await model.task(taskID)
                withAnimation(Motion.standard) { task = fresh }
                error = nil
                if fresh.taskState?.isFinished ?? true { return }
            } catch let failure as TobyError {
                error = failure.message
            } catch {
                return
            }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
    }
}

struct FileRow: View {
    var file: FileTouch

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).foregroundStyle(Theme.accent).frame(width: 22)
            VStack(alignment: .leading, spacing: 1) {
                Text((file.path as NSString).lastPathComponent).font(.subheadline).foregroundStyle(Theme.inkBright)
                Text("\(file.action.capitalized) · \(file.path)").font(.caption).foregroundStyle(Theme.inkQuiet).lineLimit(1)
            }
        }
    }

    private var symbol: String {
        switch file.action {
        case "read", "looked in": return "doc.text.magnifyingglass"
        case "created", "changed", "added to": return "square.and.pencil"
        case "moved to trash": return "trash"
        case "moved here": return "arrow.right.doc.on.clipboard"
        case "downloading": return "arrow.down.circle"
        default: return "doc"
        }
    }
}

/// A command Toby ran. Its raw output is behind a tap, as asked.
struct JobOutputDisclosure: View {
    @Environment(AppModel.self) private var model
    var job: JobInfo
    @State private var expanded = false
    @State private var output: [String] = []

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            ScrollView(.horizontal) {
                Text(output.isEmpty ? "No output." : output.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(Theme.inkNormal)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
            }
            .frame(maxHeight: 260)
            .background(Theme.control, in: RoundedRectangle(cornerRadius: 12))
        } label: {
            JobSummary(job: job)
        }
        .task(id: expanded) {
            guard expanded else { return }
            if let full = try? await model.job(job.id) { output = full.output ?? [] }
        }
    }
}

struct JobSummary: View {
    var job: JobInfo

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(job.name).font(.subheadline.weight(.medium)).foregroundStyle(Theme.inkBright).lineLimit(1)
                Spacer()
                Text(label).font(.caption.weight(.semibold)).foregroundStyle(color)
            }
            if job.isRunning, let progress = job.progress {
                ProgressView(value: progress, total: 100).tint(Theme.accent)
            }
            if let line = job.lastLine, !line.isEmpty {
                Text(line).font(.system(.caption2, design: .monospaced)).foregroundStyle(Theme.inkQuiet).lineLimit(1)
            }
        }
    }

    private var label: String {
        switch job.state {
        case "running": return job.progress.map { "\(Int($0))%" } ?? "Running"
        case "paused": return "Paused"
        case "succeeded": return "Done"
        case "failed": return "Failed" + (job.exitCode.map { " (\($0))" } ?? "")
        case "stopped": return "Stopped"
        default: return job.state.capitalized
        }
    }

    private var color: Color {
        switch job.state {
        case "succeeded": return Theme.ok
        case "failed": return Theme.alert
        case "paused", "stopped": return Theme.warn
        default: return Theme.accent
        }
    }
}
