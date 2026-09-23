import AVFoundation
import Foundation
import Observation
import Speech

/// Talk to Toby: the phone's own speech recognition, on the device where
/// the phone supports it, so what you say isn't sent anywhere but Toby.
/// Nothing listens until you tap the microphone (or Toby's face), and it
/// stops when you tap again.
@MainActor
@Observable
final class SpeechRecognizer {
    private(set) var transcript = ""
    private(set) var isListening = false
    private(set) var error: String?

    @ObservationIgnored private let recognizer = SFSpeechRecognizer()
    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var request: SFSpeechAudioBufferRecognitionRequest?
    @ObservationIgnored private var task: SFSpeechRecognitionTask?

    func start() async {
        error = nil
        guard await Self.authorize() else {
            error = "To talk to Toby, allow the microphone and speech recognition for Little Toby in Settings."
            return
        }
        guard let recognizer, recognizer.isAvailable else {
            error = "Speech recognition isn't available right now. You can type instead."
            return
        }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement, options: .duckOthers)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
            let engine = AVAudioEngine()
            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            if recognizer.supportsOnDeviceRecognition {
                request.requiresOnDeviceRecognition = true
            }
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
                request.append(buffer)
            }
            engine.prepare()
            try engine.start()
            transcript = ""
            isListening = true
            self.engine = engine
            self.request = request
            task = recognizer.recognitionTask(with: request) { [weak self] result, error in
                let text = result?.bestTranscription.formattedString
                let final = result?.isFinal ?? false
                Task { @MainActor in
                    guard let self else { return }
                    if let text { self.transcript = text }
                    if final || error != nil { self.stop() }
                }
            }
        } catch {
            self.error = "Couldn't start the microphone."
            stop()
        }
    }

    func stop() {
        guard isListening || engine != nil else { return }
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.finish()
        engine = nil
        request = nil
        task = nil
        isListening = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private static func authorize() async -> Bool {
        let speech: Bool = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
        guard speech else { return false }
        return await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }
}
