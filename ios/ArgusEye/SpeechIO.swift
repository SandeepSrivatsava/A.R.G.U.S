import AVFoundation
import Speech

/// Push-to-talk on the iPhone: speech is turned into text on the phone, and replies are spoken through
/// whatever is connected (your Meta glasses' speakers when they're on, over Bluetooth).
@MainActor
final class SpeechIO {
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var transcript = ""
    private let synthesizer = AVSpeechSynthesizer()

    func requestPermissions() async -> Bool {
        let speech = await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0 == .authorized) }
        }
        let mic = await AVAudioApplication.requestRecordPermission()
        return speech && mic
    }

    private func activateAudio() throws {
        let session = AVAudioSession.sharedInstance()
        // Bluetooth HFP lets the glasses' microphones and speakers be used when they're connected.
        try session.setCategory(.playAndRecord, mode: .default, options: [.allowBluetooth, .allowBluetoothA2DP, .defaultToSpeaker])
        try session.setActive(true)
    }

    func startListening() throws {
        stopSpeaking()
        try activateAudio()
        transcript = ""
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recognizer?.supportsOnDeviceRecognition == true { request.requiresOnDeviceRecognition = true }
        self.request = request
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { buffer, _ in
            request.append(buffer)
        }
        engine.prepare()
        try engine.start()
        task = recognizer?.recognitionTask(with: request) { [weak self] result, _ in
            guard let text = result?.bestTranscription.formattedString else { return }
            Task { @MainActor in self?.transcript = text }
        }
    }

    /// Stops listening and returns what was said.
    func stopListening() async -> String {
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        try? await Task.sleep(for: .milliseconds(600))   // let the last words arrive
        task?.cancel()
        task = nil
        request = nil
        return transcript.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func speak(_ text: String) {
        try? activateAudio()
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-GB")
        synthesizer.speak(utterance)
    }

    func stopSpeaking() {
        synthesizer.stopSpeaking(at: .immediate)
    }
}
