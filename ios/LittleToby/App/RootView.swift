import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if model.computers.computers.isEmpty {
                WelcomeView()
            } else {
                MainTabs()
            }
        }
        .animation(Motion.gentle, value: model.computers.computers.isEmpty)
        .onAppear { model.startPolling() }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                model.startPolling()
            case .background:
                // No polling in the background: it would drain the battery
                // and iOS would suspend it anyway. Background refresh checks
                // for notifications now and then instead.
                model.stopPolling()
                Notifier.shared.scheduleRefresh()
            default:
                break
            }
        }
    }
}

struct MainTabs: View {
    @Environment(AppModel.self) private var model
    @AppStorage("toby.haptics") private var haptics = true
    @State private var dismissedApproval: String?

    var body: some View {
        @Bindable var model = model
        TabView(selection: $model.selectedTab) {
            HomeView()
                .tabItem { Label("Home", systemImage: "house") }
                .tag(Tab.home)
            ChatView()
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
                .tag(Tab.chat)
            TasksView()
                .tabItem { Label("Tasks", systemImage: "checklist") }
                .tag(Tab.tasks)
            ComputerView()
                .tabItem { Label("Computer", systemImage: "desktopcomputer") }
                .tag(Tab.computer)
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(Tab.settings)
        }
        .tint(Theme.accent)
        .overlay(alignment: .top) {
            if let toast = model.toast {
                Toast(text: toast)
                    .padding(.top, 6)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .sheet(item: Binding(
            get: { model.approvals.first(where: { $0.id != dismissedApproval }) },
            set: { value in if value == nil { dismissedApproval = model.approvals.first?.id } }
        )) { approval in
            ApprovalSheet(approval: approval)
        }
        .onReceive(NotificationCenter.default.publisher(for: .showApproval)) { _ in
            dismissedApproval = nil
        }
        .sensoryFeedback(trigger: model.celebrateAt) { _, _ -> SensoryFeedback? in haptics ? .success : nil }
        .sensoryFeedback(trigger: model.lastFailureAt) { _, _ -> SensoryFeedback? in haptics ? .error : nil }
    }
}

/// A short in-app notification that slides down and goes away by itself.
struct Toast: View {
    var text: String

    var body: some View {
        HStack(spacing: 10) {
            TobyFace(mood: .happy, size: 24)
            Text(text).font(.subheadline.weight(.medium)).foregroundStyle(Theme.inkBright).lineLimit(2)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.regularMaterial, in: Capsule())
        .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
        .accessibilityIdentifier("toast")
    }
}
