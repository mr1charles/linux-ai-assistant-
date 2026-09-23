import SwiftUI

/// Talk to Toby: type or speak, and see the conversation the computer keeps.
struct ChatView: View {
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @State private var speech = SpeechRecognizer()
    @AppStorage("toby.voice.autosend") private var autoSend = true
    @FocusState private var focused: Bool

    private var messages: [HistoryMessage] { model.state?.history ?? [] }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            if messages.isEmpty && model.sending == nil {
                                emptyState
                            }
                            ForEach(Array(messages.enumerated()), id: \.offset) { _, message in
                                MessageBubble(role: message.role, text: message.content)
                            }
                            if let sending = model.sending {
                                MessageBubble(role: "user", text: sending, pending: true)
                                    .transition(.arrive)
                            }
                            if model.busy {
                                WorkingBubble(task: model.currentTask)
                                    .transition(.arrive)
                            }
                            Color.clear.frame(height: 1).id("bottom")
                        }
                        .padding(16)
                        .animation(Motion.enter, value: messages.count)
                        .animation(Motion.enter, value: model.busy)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .onChange(of: messages.count) { _, _ in
                        withAnimation(Motion.move) { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                    .onChange(of: model.busy) { _, _ in
                        withAnimation(Motion.move) { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                    .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                composer
            }
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle("Chat")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            TobyFace(mood: model.mood, size: 90)
            Text("Tell Toby what to do on \(model.activeComputer?.name ?? "your computer").")
                .font(.headline).foregroundStyle(Theme.inkBright).multilineTextAlignment(.center)
            Text("\"Is my game still running?\"  \"Run the tests in ~/project.\"  \"Open Claude Code in my project and carry on.\"")
                .font(.subheadline).foregroundStyle(Theme.inkQuiet).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    private var composer: some View {
        VStack(spacing: 6) {
            if let error = speech.error {
                Text(error).font(.caption).foregroundStyle(Theme.warn).frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField(speech.isListening ? "Listening…" : "Message Toby", text: $text, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($focused)
                    .submitLabel(.send)
                    .onSubmit(send)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 11)
                    .background(Theme.control, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                    .accessibilityIdentifier("chat.field")
                Button(action: toggleMic) {
                    Image(systemName: speech.isListening ? "waveform" : "mic.fill")
                        .symbolEffect(.variableColor.iterative, isActive: speech.isListening)
                        .font(.system(size: 18, weight: .semibold))
                        .frame(width: 44, height: 44)
                        .foregroundStyle(speech.isListening ? Color.white : Theme.inkBright)
                        .background(speech.isListening ? Theme.accent : Theme.control, in: Circle())
                }
                .accessibilityLabel(speech.isListening ? "Stop listening" : "Talk to Toby")
                .accessibilityIdentifier("chat.mic")
                Button(action: send) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 18, weight: .bold))
                        .frame(width: 44, height: 44)
                        .foregroundStyle(.white)
                        .background(canSend ? Theme.accent : Theme.accent.opacity(0.35), in: Circle())
                }
                .disabled(!canSend)
                .accessibilityLabel("Send")
                .accessibilityIdentifier("chat.send")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.bar)
        .onChange(of: speech.transcript) { _, heard in
            if speech.isListening { text = heard }
        }
    }

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && model.connection == .online
    }

    private func send() {
        let message = text
        guard canSend else { return }
        text = ""
        Task { await model.ask(message) }
    }

    private func toggleMic() {
        if speech.isListening {
            speech.stop()
            if autoSend { send() }
        } else {
            focused = false
            Task { await speech.start() }
        }
    }
}

struct MessageBubble: View {
    var role: String
    var text: String
    var pending = false

    private var isUser: Bool { role == "user" }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 48) }
            Text(text)
                .font(.body)
                .foregroundStyle(isUser ? Color.white : Theme.inkBright)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(isUser ? Theme.accent : Theme.surface,
                            in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .opacity(pending ? 0.7 : 1)
                .textSelection(.enabled)
            if !isUser { Spacer(minLength: 48) }
        }
        .accessibilityIdentifier(isUser ? "chat.user" : "chat.toby")
    }
}

/// Toby at work, in the conversation: the step it's on right now.
struct WorkingBubble: View {
    var task: TaskInfo?

    var body: some View {
        HStack(spacing: 10) {
            TobyFace(mood: task?.taskState == .thinking ? .thinking : .focused, size: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text(task?.taskState?.label ?? "Working")
                    .font(.caption.weight(.semibold)).foregroundStyle(Theme.accent)
                Text(currentStep).font(.subheadline).foregroundStyle(Theme.inkNormal).lineLimit(2)
            }
            Spacer()
        }
        .padding(12)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .accessibilityIdentifier("chat.working")
    }

    private var currentStep: String {
        task?.steps?.first(where: { $0.status == "current" })?.label ?? task?.text ?? "Thinking…"
    }
}
