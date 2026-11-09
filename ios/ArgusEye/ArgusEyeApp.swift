import MWDATCore
import SwiftUI

@main
struct ArgusEyeApp: App {
    @State private var model: AppModel

    init() {
        do {
            try Wearables.configure()
        } catch {
            NSLog("Argus: couldn't start the glasses toolkit: \(error)")
        }
        _model = State(wrappedValue: AppModel())
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .onOpenURL { url in Task { await model.glasses.handle(url: url) } }
                .task { await model.start() }
        }
    }
}

/// Everything the screen shows and does.
@Observable
@MainActor
final class AppModel {
    var macStatus = "Checking your Mac…"
    var glassesStatus = "Glasses not connected"
    var glassesConnected = false
    var reply = ""
    var heard = ""
    var listening = false
    var busy = false
    let glasses = GlassesLink()
    let speech = SpeechIO()
    private var pollTask: Task<Void, Never>?

    func start() async {
        glasses.onConnectionChange = { [weak self] connected, name in
            guard let self else { return }
            self.glassesConnected = connected
            self.glassesStatus = connected ? "\(name) connected" : "Glasses not connected"
            Task { try? await MacLink.current().glassesStatus(connected: connected, device: name) }
        }
        glasses.start()
        _ = await speech.requestPermissions()
        await refreshMac()
        startServingTheMac()
    }

    func refreshMac() async {
        guard let link = try? MacLink.current() else {
            macStatus = "Set up your Mac in Settings"
            return
        }
        macStatus = await link.ping() ? "Connected to Argus on your Mac" : "Can't reach your Mac (is Tailscale on?)"
    }

    /// Hold to talk.
    func beginTalking() {
        guard !busy else { return }
        do {
            try speech.startListening()
            listening = true
        } catch {
            reply = "Microphone problem: \(error.localizedDescription)"
        }
    }

    func endTalking() async {
        guard listening else { return }
        listening = false
        let text = await speech.stopListening()
        heard = text
        guard !text.isEmpty else { return }
        let lower = text.lowercased()
        let wantsALook = glassesConnected && ["what am i looking at", "what is this", "what's this", "look at this",
                                              "read this", "what do you see"].contains { lower.contains($0) }
        await run { link in
            if wantsALook {
                let jpeg = try await self.glasses.capturePhoto()
                return try await link.look(jpeg: jpeg, question: text)
            }
            return try await link.ask(text)
        }
    }

    /// The Look button: a photo from the glasses, described out loud.
    func look() async {
        await run { link in
            let jpeg = try await self.glasses.capturePhoto()
            return try await link.look(jpeg: jpeg, question: "What am I looking at? Tell me briefly.")
        }
    }

    private func run(_ work: @escaping (MacLink) async throws -> String) async {
        busy = true
        defer { busy = false }
        do {
            let text = try await work(try MacLink.current())
            reply = text
            speech.speak(text)
        } catch {
            reply = error.localizedDescription
            speech.speak(reply)
        }
    }

    /// While the app is open, Argus on the Mac can ask for a picture ("Argus, what am I looking at?").
    private func startServingTheMac() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, let link = try? MacLink.current() else {
                    try? await Task.sleep(for: .seconds(5))
                    continue
                }
                do {
                    try await link.glassesStatus(connected: self.glassesConnected, device: self.glasses.deviceName)
                    if let id = try await link.nextCapture(), self.glassesConnected {
                        let jpeg = try await self.glasses.capturePhoto()
                        try await link.sendFrame(id: id, jpeg: jpeg)
                    }
                } catch {
                    try? await Task.sleep(for: .seconds(5))
                }
            }
        }
    }
}
