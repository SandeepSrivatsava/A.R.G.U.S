import Foundation
import MWDATCamera
import MWDATCore

/// Your Meta glasses, through Meta's Wearables Device Access Toolkit: knows when they're connected
/// and takes a photo when you (or Argus) ask. Video only runs while a photo is being taken.
@MainActor
final class GlassesLink {
    enum GlassesError: LocalizedError {
        case notConnected, noPermission, cameraUnavailable, timeout, captureFailed
        var errorDescription: String? {
            switch self {
            case .notConnected: return "Your glasses aren't connected."
            case .noPermission: return "Allow camera access for Argus in the Meta AI app."
            case .cameraUnavailable: return "The glasses camera isn't available right now."
            case .timeout: return "The glasses didn't send a picture in time."
            case .captureFailed: return "The glasses couldn't take a picture."
            }
        }
    }

    var onConnectionChange: ((Bool, String) -> Void)?
    private(set) var connected = false
    private(set) var deviceName = "Meta glasses"
    private var selector: AutoDeviceSelector?
    private var session: DeviceSession?
    private var camera: MWDATCamera.Camera?
    private var streaming = false
    private let sessionTokens = ListenerTokenBag()
    private let streamTokens = ListenerTokenBag()
    private var photoWaiter: CheckedContinuation<Data, Error>?

    var isRegistered: Bool {
        if case .registered = Wearables.shared.registrationState { return true }
        return false
    }

    /// Links this app to your glasses (opens the Meta AI app once to confirm).
    func register() async throws {
        try await Wearables.shared.startRegistration()
    }

    /// The Meta AI app calls back with a URL after registration or permission prompts.
    func handle(url: URL) async {
        guard let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              items.contains(where: { $0.name == "metaWearablesAction" }) else { return }
        _ = try? await Wearables.shared.handleUrl(url)
    }

    func start() {
        let wearables = Wearables.shared
        let selector = AutoDeviceSelector(wearables: wearables)
        self.selector = selector
        Task { [weak self] in
            for await deviceId in selector.activeDeviceStream() {
                guard let self else { return }
                let name = deviceId.flatMap { wearables.deviceForIdentifier($0)?.nameOrId() } ?? "Meta glasses"
                self.connected = deviceId != nil
                self.deviceName = name
                if deviceId == nil { self.stopCamera() }
                self.onConnectionChange?(self.connected, name)
            }
        }
    }

    func capturePhoto(timeout: Duration = .seconds(20)) async throws -> Data {
        try await startCamera()
        defer { stopCamera() }
        return try await withCheckedThrowingContinuation { (waiter: CheckedContinuation<Data, Error>) in
            photoWaiter = waiter
            if camera?.stream.capturePhoto(format: .jpeg) != true {
                photoWaiter = nil
                waiter.resume(throwing: GlassesError.captureFailed)
                return
            }
            Task { [weak self] in
                try? await Task.sleep(for: timeout)
                guard let self, let pending = self.photoWaiter else { return }
                self.photoWaiter = nil
                pending.resume(throwing: GlassesError.timeout)
            }
        }
    }

    private func startCamera() async throws {
        if camera != nil && streaming { return }
        guard connected, let selector else { throw GlassesError.notConnected }
        let wearables = Wearables.shared
        if try await wearables.checkPermissionStatus(.camera) != .granted {
            guard try await wearables.requestPermission(.camera) == .granted else { throw GlassesError.noPermission }
        }
        if session == nil {
            let newSession = try wearables.createSession(deviceSelector: selector)
            session = newSession
            newSession.statePublisher.listen { [weak self] state in
                Task { @MainActor in
                    if state == .stopped { self?.session = nil; self?.sessionTokens.clear() }
                }
            }.store(in: sessionTokens)
            try newSession.start()
            try await waitFor(seconds: 10) { [weak self] in self?.session?.state == .started }
        }
        let config = StreamConfiguration(videoCodec: VideoCodec.hvc1, resolution: StreamingResolution.low, frameRate: 24)
        guard let newCamera = try session?.addCamera(config: config) else { throw GlassesError.cameraUnavailable }
        camera = newCamera
        newCamera.stream.statePublisher.listen { [weak self] state in
            Task { @MainActor in self?.streaming = state == .streaming }
        }.store(in: streamTokens)
        newCamera.stream.photoDataPublisher.listen { [weak self] photo in
            Task { @MainActor in
                guard let self, let waiter = self.photoWaiter else { return }
                self.photoWaiter = nil
                waiter.resume(returning: photo.data)
            }
        }.store(in: streamTokens)
        newCamera.stream.start()
        try await waitFor(seconds: 10) { [weak self] in self?.streaming == true }
    }

    func stopCamera() {
        camera?.stop()
        camera = nil
        streaming = false
        streamTokens.clear()
        session?.stop()
    }

    private func waitFor(seconds: Double, _ condition: @escaping () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition() {
            if Date() > deadline { throw GlassesError.cameraUnavailable }
            try await Task.sleep(for: .milliseconds(100))
        }
    }
}
