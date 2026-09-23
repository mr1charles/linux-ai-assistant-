import SwiftUI

/// What's happening on the computer, designed for a phone rather than a
/// shrunken desktop: its windows, a live view of the screen or just the
/// window Toby is working in, commands Toby ran, and files it touched.
struct ComputerView: View {
    @Environment(AppModel.self) private var model

    enum Mode: String, CaseIterable, Identifiable {
        case overview = "Overview", screen = "Screen", toby = "Toby View", terminal = "Terminal", files = "Files"
        var id: String { rawValue }
    }

    @State private var mode: Mode = .overview

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                WorkModeCard()
                    .padding(.horizontal, 16)
                Picker("View", selection: $mode) {
                    ForEach(Mode.allCases) { mode in Text(mode.rawValue).tag(mode) }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .accessibilityIdentifier("computer.mode")
                Group {
                    switch mode {
                    case .overview: OverviewSection()
                    case .screen: ScreenSection(view: "full")
                    case .toby: ScreenSection(view: "toby")
                    case .terminal: TerminalSection()
                    case .files: FilesSection()
                    }
                }
                .frame(maxHeight: .infinity)
                .transition(.opacity)
                .animation(Motion.standard, value: mode)
            }
            .padding(.top, 8)
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle(model.activeComputer?.name ?? "Computer")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

/// Toby Work Mode: working on the screen like a person with a pen, mouse
/// and keyboard, looking at the result of each action before the next.
struct WorkModeCard: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let on = model.state?.workMode ?? false
        HStack(spacing: 12) {
            Image(systemName: "pencil.tip.crop.circle")
                .font(.title2)
                .foregroundStyle(on ? Theme.accent : Theme.inkQuiet)
                .symbolEffect(.bounce, value: on)
            VStack(alignment: .leading, spacing: 2) {
                Text("Work Mode").font(.headline).foregroundStyle(Theme.inkBright)
                Text(on ? "Toby looks at the screen and works on it with a pen, mouse and keyboard."
                        : "Turn on to let Toby work visually: circle, highlight, write, drag, click.")
                    .font(.caption).foregroundStyle(Theme.inkQuiet)
            }
            Spacer()
            Toggle("Work Mode", isOn: Binding(get: { on }, set: { value in Task { await model.setWorkMode(value) } }))
                .labelsHidden()
                .disabled(model.connection != .online)
                .accessibilityIdentifier("computer.workMode")
        }
        .card(padding: 14)
    }
}

struct OverviewSection: View {
    @Environment(AppModel.self) private var model
    @State private var overview: Overview?
    @State private var error: TobyError?

    var body: some View {
        List {
            if let overview {
                let groups = Dictionary(grouping: overview.windows, by: { $0.workspace ?? "" })
                ForEach(groups.keys.sorted(), id: \.self) { workspace in
                    Section(workspace.isEmpty ? "Windows" : "Workspace \(workspace)") {
                        ForEach(groups[workspace] ?? []) { window in
                            Button {
                                Task {
                                    if await model.focus(window) { await load() }
                                }
                            } label: {
                                WindowRow(window: window)
                            }
                            .listRowBackground(Theme.surface)
                        }
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .overlay {
            if let error {
                ContentUnavailableView("Can't see the computer", systemImage: "desktopcomputer.trianglebadge.exclamationmark",
                                       description: Text(error.message))
            } else if overview?.windows.isEmpty == true {
                ContentUnavailableView("No windows open", systemImage: "macwindow",
                                       description: Text("Or the computer's window manager couldn't be asked. Toby reads this from Hyprland."))
            } else if overview == nil {
                ProgressView()
            }
        }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    private func load() async {
        do {
            let fresh = try await model.overview()
            withAnimation(Motion.standard) { overview = fresh }
            error = nil
        } catch let failure as TobyError {
            error = failure
        } catch {}
    }
}

struct WindowRow: View {
    var window: WindowInfo

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.title3)
                .foregroundStyle(window.focused == true ? Theme.accent : Theme.inkQuiet)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(window.app.isEmpty ? "Window" : window.app.capitalized)
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.inkBright)
                Text(window.title).font(.caption).foregroundStyle(Theme.inkQuiet).lineLimit(1)
            }
            Spacer()
            if window.focused == true {
                Text("In front").font(.caption2.weight(.semibold)).foregroundStyle(Theme.accent)
            }
        }
        .accessibilityHint("Switches the computer to this window")
    }

    private var symbol: String {
        let app = window.app.lowercased()
        if app.contains("firefox") || app.contains("chrom") || app.contains("brave") { return "safari" }
        if app.contains("kitty") || app.contains("foot") || app.contains("alacritty") || app.contains("term") { return "terminal" }
        if app.contains("code") { return "chevron.left.forwardslash.chevron.right" }
        if app.contains("steam") || app.contains("game") { return "gamecontroller" }
        if app.contains("discord") { return "bubble.left.and.bubble.right" }
        if app.contains("thunar") || app.contains("nautilus") || app.contains("files") { return "folder" }
        return "macwindow"
    }
}

/// A live screenshot, refreshed about once a second while you're looking,
/// and never while you aren't. Pinch to zoom.
struct ScreenSection: View {
    @Environment(AppModel.self) private var model
    var view: String
    @State private var image: UIImage?
    @State private var error: TobyError?
    @State private var updated: Date?
    @State private var zoom: CGFloat = 1
    @GestureState private var pinch: CGFloat = 1

    var body: some View {
        VStack(spacing: 8) {
            if let image, error == nil {
                ScrollView([.horizontal, .vertical]) {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(width: UIScreen.main.bounds.width * zoom * pinch - 32)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .shadow(color: .black.opacity(0.25), radius: 12, y: 6)
                        .padding(16)
                }
                .gesture(MagnifyGesture()
                    .updating($pinch) { value, state, _ in state = value.magnification }
                    .onEnded { value in zoom = min(4, max(1, zoom * value.magnification)) })
                .onTapGesture(count: 2) { withAnimation(Motion.move) { zoom = zoom > 1 ? 1 : 2.5 } }
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text("Live · \(Format.ago(updated, now: context.date))" + (view == "toby" ? " · the window Toby is working in" : ""))
                        .font(.caption2).foregroundStyle(Theme.inkQuiet)
                }
            } else if let error {
                if error == .screenViewOff {
                    ContentUnavailableView("Screen view is off", systemImage: "eye.slash",
                                           description: Text("It's off until you turn on \"Let paired phones see the screen\" in Toby's Settings on the computer. While a phone is looking, the computer says so."))
                } else {
                    ContentUnavailableView("Can't show the screen", systemImage: "rectangle.slash",
                                           description: Text(error.message))
                }
            } else {
                ProgressView().frame(maxHeight: .infinity)
            }
        }
        .accessibilityIdentifier("computer.screen")
        .task(id: view) {
            let width = Int(UIScreen.main.bounds.width * UIScreen.main.scale)
            while !Task.isCancelled {
                do {
                    let data = try await model.screen(view: view, maxWidth: min(width, 1600))
                    if let picture = UIImage(data: data) {
                        image = picture
                        updated = Date()
                        error = nil
                    }
                } catch let failure as TobyError {
                    error = failure
                    if failure == .screenViewOff {
                        try? await Task.sleep(nanoseconds: 5_000_000_000)
                        continue
                    }
                } catch {
                    return
                }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }
}

/// Commands Toby ran, and their output: the raw terminal view, for when you
/// want it.
struct TerminalSection: View {
    @Environment(AppModel.self) private var model
    @State private var selected: String?
    @State private var output: [String] = []

    private var jobs: [JobInfo] { model.state?.jobs ?? [] }

    var body: some View {
        VStack(spacing: 10) {
            if jobs.isEmpty {
                ContentUnavailableView("No commands yet", systemImage: "terminal",
                                       description: Text("Builds, tests and downloads Toby runs show up here with their output."))
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(jobs) { job in
                            Button {
                                selected = job.id
                            } label: {
                                JobChip(job: job, selected: (selected ?? jobs.first?.id) == job.id)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 16)
                }
                if let job = jobs.first(where: { $0.id == (selected ?? jobs.first?.id) }) {
                    JobSummary(job: job).padding(.horizontal, 16)
                    ScrollViewReader { proxy in
                        ScrollView {
                            Text(output.isEmpty ? "No output yet." : output.joined(separator: "\n"))
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(Theme.inkNormal)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(12)
                            Color.clear.frame(height: 1).id("end")
                        }
                        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .padding(.horizontal, 16)
                        .onChange(of: output.count) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
                    }
                    .task(id: job.id) {
                        while !Task.isCancelled {
                            if let full = try? await model.job(job.id) {
                                output = full.output ?? []
                                if !full.isRunning { return }
                            }
                            try? await Task.sleep(nanoseconds: 1_000_000_000)
                        }
                    }
                }
            }
        }
        .accessibilityIdentifier("computer.terminal")
    }
}

struct JobChip: View {
    var job: JobInfo
    var selected: Bool

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(job.isRunning ? Theme.accent : (job.state == "succeeded" ? Theme.ok : Theme.alert))
                .frame(width: 7, height: 7)
            Text(job.name).font(.caption.weight(.medium)).lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .foregroundStyle(selected ? Color.white : Theme.inkBright)
        .background(selected ? Theme.accent : Theme.control, in: Capsule())
    }
}

struct FilesSection: View {
    @Environment(AppModel.self) private var model
    @State private var files: [FileTouch]?
    @State private var error: TobyError?

    var body: some View {
        List {
            ForEach(files ?? [], id: \.path) { file in
                FileRow(file: file).listRowBackground(Theme.surface)
            }
        }
        .scrollContentBackground(.hidden)
        .overlay {
            if let error {
                ContentUnavailableView("Can't list files", systemImage: "folder.badge.questionmark",
                                       description: Text(error.message))
            } else if files?.isEmpty == true {
                ContentUnavailableView("No files yet", systemImage: "folder",
                                       description: Text("Files Toby reads, writes, moves or deletes for you show up here."))
            } else if files == nil {
                ProgressView()
            }
        }
        .refreshable { await load() }
        .task { await load() }
        .accessibilityIdentifier("computer.files")
    }

    private func load() async {
        do {
            files = try await model.files()
            error = nil
        } catch let failure as TobyError {
            error = failure
        } catch {}
    }
}
