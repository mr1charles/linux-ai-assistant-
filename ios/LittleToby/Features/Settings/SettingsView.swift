import SwiftUI
import UserNotifications

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("toby.appearance") private var appearance = "system"
    @AppStorage("toby.haptics") private var haptics = true
    @AppStorage("toby.voice.autosend") private var autoSend = true
    @State private var notifications = Notifier.shared.enabled
    @State private var pairing = false
    @State private var confirmLogout = false
    @State private var notificationsDenied = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    ForEach(model.computers.computers) { computer in
                        Button {
                            model.switchTo(computer.id)
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(computer.name).foregroundStyle(Theme.inkBright)
                                    Text(computer.baseURL.host ?? "").font(.caption).foregroundStyle(Theme.inkQuiet)
                                }
                                Spacer()
                                if computer.id == model.computers.activeID {
                                    Image(systemName: "checkmark").foregroundStyle(Theme.accent)
                                }
                            }
                        }
                        .swipeActions {
                            Button("Forget", role: .destructive) { model.forget(computer.id) }
                        }
                    }
                    Button("Pair another computer") { pairing = true }
                        .accessibilityIdentifier("settings.pair")
                } header: {
                    Text("Computers")
                } footer: {
                    Text("Swipe to forget a computer on this phone only. To unpair it properly, use Log out below.")
                }

                if let computer = model.activeComputer {
                    Section(computer.name) {
                        NavigationLink("Paired phones") { DevicesView() }
                        LabeledContent("Screen view", value: policyText(model.status?.policy?.screenView, on: "On", off: "Off"))
                        LabeledContent("Restricted actions", value: policyText(model.status?.policy?.restrictedActions,
                                                                               on: "Allowed, with a question", off: "Refused"))
                        Button("Log out of \(computer.name)", role: .destructive) { confirmLogout = true }
                            .accessibilityIdentifier("settings.logout")
                    }
                }

                Section {
                    Toggle("Notifications", isOn: $notifications)
                        .onChange(of: notifications) { _, on in setNotifications(on) }
                    if notifications {
                        ForEach(Notifier.categories, id: \.kind) { category in
                            Toggle(category.title, isOn: Binding(
                                get: { Notifier.shared.wants(category.kind) },
                                set: { Notifier.shared.setWants(category.kind, $0) }))
                        }
                    }
                } header: {
                    Text("Notifications")
                } footer: {
                    Text(notificationsDenied
                         ? "Notifications are turned off for Little Toby in the iPhone's Settings."
                         : "While Little Toby is open, these arrive at once. In the background, iOS decides how often the app may check, so they can be late; for instant ones, see \"Notifications\" in the Little Toby guide (ntfy).")
                }

                Section("Talking") {
                    Toggle("Send when I stop speaking", isOn: $autoSend)
                }

                Section("Appearance") {
                    Picker("Theme", selection: $appearance) {
                        Text("Match the phone").tag("system")
                        Text("Light").tag("light")
                        Text("Dark").tag("dark")
                    }
                    Toggle("Haptics", isOn: $haptics)
                }

                Section("Quick actions") {
                    NavigationLink("Edit quick actions") { QuickActionsEditor() }
                }

                Section {
                    PermissionRow(level: "Safe", color: Theme.ok,
                                  text: "Looking: status, files, programs, windows, opening apps and pages. Done at once.")
                    PermissionRow(level: "Asks first", color: Theme.warn,
                                  text: "Changing: writing, moving or deleting files (to the trash), running commands, stopping programs, downloading, using the mouse and keyboard.")
                    PermissionRow(level: "Restricted", color: Theme.alert,
                                  text: "sudo, recursive deletes, disks, your keys and passwords, anything outside your home folder. Refused unless allowed on the computer.")
                } header: {
                    Text("What Toby asks about")
                } footer: {
                    Text("If nobody answers a question within ten minutes, Toby takes it as no.")
                }

                Section("About") {
                    LabeledContent("Version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "")
                    Text("Little Toby runs on your computer. This app talks to it over Tailscale, a private network between your own devices; nothing goes through anyone else's server.")
                        .font(.footnote).foregroundStyle(Theme.inkQuiet)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle("Settings")
            .sheet(isPresented: $pairing) { PairingView() }
            .confirmationDialog("Log out of \(model.activeComputer?.name ?? "this computer")?",
                                isPresented: $confirmLogout, titleVisibility: .visible) {
                Button("Log out", role: .destructive) { Task { await model.logOut() } }
            } message: {
                Text("This phone is unpaired on the computer and forgotten here. You can pair again any time.")
            }
            .task { await model.refreshStatus() }
        }
    }

    private func policyText(_ value: Bool?, on: String, off: String) -> String {
        guard let value else { return "Unknown" }
        return value ? on : off
    }

    private func setNotifications(_ on: Bool) {
        Notifier.shared.enabled = on
        guard on else { return }
        Task {
            let granted = await Notifier.shared.requestAuthorization()
            notificationsDenied = !granted
            if !granted {
                notifications = false
                Notifier.shared.enabled = false
            } else {
                Notifier.shared.scheduleRefresh()
            }
        }
    }
}

private struct PermissionRow: View {
    var level: String
    var color: Color
    var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Circle().fill(color).frame(width: 8, height: 8)
                Text(level).font(.subheadline.weight(.semibold))
            }
            Text(text).font(.caption).foregroundStyle(Theme.inkQuiet)
        }
        .padding(.vertical, 2)
    }
}

/// Every phone paired with this computer. Unpair a lost one from here.
struct DevicesView: View {
    @Environment(AppModel.self) private var model
    @State private var devices: [DeviceInfo] = []
    @State private var error: String?
    @State private var toRevoke: DeviceInfo?

    var body: some View {
        List {
            ForEach(devices) { device in
                HStack(spacing: 12) {
                    Image(systemName: device.platform == "ios" ? "iphone" : "globe")
                        .foregroundStyle(Theme.accent).frame(width: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(device.name)
                            if device.thisDevice == true {
                                Text("This phone").font(.caption2.weight(.semibold)).foregroundStyle(Theme.accent)
                            }
                        }
                        Text(device.lastSeen == nil ? "Not used yet" : "Last used \(Format.ago(epoch: device.lastSeen))")
                            .font(.caption).foregroundStyle(Theme.inkQuiet)
                    }
                }
                .swipeActions {
                    Button("Unpair", role: .destructive) { toRevoke = device }
                }
            }
        }
        .overlay {
            if let error {
                ContentUnavailableView("Can't list phones", systemImage: "iphone.slash", description: Text(error))
            }
        }
        .navigationTitle("Paired phones")
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog("Unpair \(toRevoke?.name ?? "")?", isPresented: Binding(
            get: { toRevoke != nil }, set: { if !$0 { toRevoke = nil } }), titleVisibility: .visible) {
            Button("Unpair", role: .destructive) {
                guard let device = toRevoke else { return }
                Task {
                    if device.thisDevice == true {
                        await model.logOut()
                    } else {
                        try? await model.revoke(device)
                        await load()
                    }
                }
            }
        } message: {
            Text("It stops working with this computer at once.")
        }
    }

    private func load() async {
        do {
            devices = try await model.devices()
            error = nil
        } catch let failure as TobyError {
            error = failure.message
        } catch {}
    }
}

struct QuickActionsEditor: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var adding = false
    @State private var title = ""
    @State private var prompt = ""

    var body: some View {
        List {
            Section {
                ForEach(model.quickActions.actions) { action in
                    Label {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(action.title)
                            if let prompt = action.prompt {
                                Text("Asks: \"\(prompt)\"").font(.caption).foregroundStyle(Theme.inkQuiet)
                            }
                        }
                    } icon: {
                        Image(systemName: action.symbol)
                    }
                }
                .onDelete { model.quickActions.remove(at: $0) }
                .onMove { model.quickActions.move(from: $0, to: $1) }
            } footer: {
                Text("Your own actions send exactly what you write to Toby, as if you'd typed it.")
            }
            Section {
                Button("Add an action") { adding = true }
                Button("Reset to the standard set", role: .destructive) { model.quickActions.reset() }
            }
        }
        .navigationTitle("Quick actions")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { EditButton() }
            ToolbarItem(placement: .topBarLeading) { Button("Done") { dismiss() } }
        }
        .alert("New quick action", isPresented: $adding) {
            TextField("Name, e.g. Start the server", text: $title)
            TextField("What to ask Toby", text: $prompt)
            Button("Add") {
                let name = title.trimmingCharacters(in: .whitespaces)
                let ask = prompt.trimmingCharacters(in: .whitespaces)
                if !name.isEmpty, !ask.isEmpty {
                    model.quickActions.add(.ask(name, ask, symbol: "sparkles", section: "Mine"))
                }
                title = ""
                prompt = ""
            }
            Button("Cancel", role: .cancel) {}
        }
    }
}

/// "Toby wants to…": the question, what exactly, and why it might be risky.
struct ApprovalSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @AppStorage("toby.haptics") private var haptics = true
    var approval: Approval
    @State private var confirmRestricted = false
    @State private var answering = false

    var body: some View {
        VStack(spacing: 18) {
            TobyFace(mood: .waiting, size: 80)
            VStack(spacing: 6) {
                Text("Toby wants to").font(.subheadline).foregroundStyle(Theme.inkQuiet)
                Text(approval.title.hasSuffix("?") ? String(approval.title.dropLast()) : approval.title)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(Theme.inkBright)
                    .multilineTextAlignment(.center)
                    .accessibilityIdentifier("approval.title")
            }
            if let details = approval.details, !details.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(details, id: \.self) { line in
                        HStack(alignment: .top, spacing: 8) {
                            Text("·").foregroundStyle(Theme.inkQuiet)
                            Text(line).font(.subheadline).foregroundStyle(Theme.inkNormal)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .card(padding: 14)
            }
            if approval.isRestricted {
                Label("Careful: \(approval.reason ?? "this could do serious damage").", systemImage: "exclamationmark.shield")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.alert)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .background(Theme.alert.opacity(0.12), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            Spacer(minLength: 0)
            HStack(spacing: 12) {
                Button("Don't allow") { answer(false) }
                    .buttonStyle(TobyButtonStyle(kind: .secondary))
                    .accessibilityIdentifier("approval.deny")
                Button("Allow") {
                    if approval.isRestricted { confirmRestricted = true } else { answer(true) }
                }
                .buttonStyle(TobyButtonStyle())
                .accessibilityIdentifier("approval.allow")
            }
            .disabled(answering)
        }
        .padding(24)
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .background(Theme.background)
        .sensoryFeedback(trigger: approval.id) { _, _ -> SensoryFeedback? in haptics ? .warning : nil }
        .confirmationDialog("Allow a restricted action?", isPresented: $confirmRestricted, titleVisibility: .visible) {
            Button("Allow anyway", role: .destructive) { answer(true) }
        } message: {
            Text(sentence(approval.reason) ?? "This could do serious damage.")
        }
    }

    private func sentence(_ text: String?) -> String? {
        guard let text, let first = text.first else { return nil }
        return String(first).uppercased() + text.dropFirst() + "."
    }

    private func answer(_ allow: Bool) {
        answering = true
        Task {
            await model.approve(approval, allow: allow)
            dismiss()
        }
    }
}
