import Foundation
import Security

/// Talks to Argus on your Mac over Tailscale (the phone gateway). Every request carries the token.
struct MacLink {
    let base: URL
    let token: String

    enum LinkError: LocalizedError {
        case notConfigured, http(Int, String), badReply
        var errorDescription: String? {
            switch self {
            case .notConfigured: return "Set your Mac's address and token in Settings."
            case .http(let code, let text): return code == 401 ? "The token was rejected." : "Mac answered \(code): \(text)"
            case .badReply: return "The Mac sent an answer the app couldn't read."
            }
        }
    }

    static func current() throws -> MacLink {
        guard let address = UserDefaults.standard.string(forKey: "macAddress"), !address.isEmpty,
              let url = URL(string: address.hasPrefix("http") ? address : "http://\(address)"),
              let token = Keychain.read("phoneToken"), !token.isEmpty else { throw LinkError.notConfigured }
        return MacLink(base: url, token: token)
    }

    private func request(_ method: String, _ path: String, _ body: [String: Any]?, timeout: TimeInterval) async throws -> [String: Any] {
        var req = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: req)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard (200..<300).contains(status) else {
            throw LinkError.http(status, (json["error"] as? String) ?? (json["reply"] as? String) ?? "")
        }
        return json
    }

    func ping() async -> Bool {
        (try? await request("GET", "ping", nil, timeout: 8))?["ok"] as? Bool == true
    }

    func ask(_ text: String) async throws -> String {
        let reply = try await request("POST", "ask", ["text": text], timeout: 290)
        guard let text = reply["reply"] as? String else { throw LinkError.badReply }
        return text
    }

    func look(jpeg: Data, question: String) async throws -> String {
        let reply = try await request("POST", "look", ["image": jpeg.base64EncodedString(), "text": question], timeout: 290)
        guard let text = reply["reply"] as? String else { throw LinkError.badReply }
        return text
    }

    /// Tells the Mac whether the glasses are on. The answer says how often to send a glance (0: never).
    @discardableResult
    func glassesStatus(connected: Bool, device: String) async throws -> Int {
        let reply = try await request("POST", "glasses/status", ["connected": connected, "device": device], timeout: 15)
        return reply["glance_every"] as? Int ?? 0
    }

    /// A photo sent by itself, so Argus knows where things are ("where did I put my keys?"). The Mac keeps a few
    /// words about what's in view, never the photo. Only sent when you switched glances on, on the Mac.
    func sendGlance(jpeg: Data) async throws {
        _ = try await request("POST", "glasses/glance", ["image": jpeg.base64EncodedString()], timeout: 90)
    }

    /// Long poll: returns a request id when Argus wants a picture from the glasses.
    func nextCapture() async throws -> String? {
        try await request("GET", "glasses/next", nil, timeout: 40)["capture"] as? String
    }

    func sendFrame(id: String, jpeg: Data) async throws {
        _ = try await request("POST", "glasses/frame", ["request_id": id, "image": jpeg.base64EncodedString()], timeout: 60)
    }
}

/// The phone token lives in the iPhone's Keychain, not in settings files.
enum Keychain {
    static func save(_ key: String, _ value: String) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "argus",
                                   kSecAttrAccount as String: key]
        SecItemDelete(base as CFDictionary)
        var item = base
        item[kSecValueData as String] = Data(value.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(item as CFDictionary, nil)
    }

    static func read(_ key: String) -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "argus",
                                    kSecAttrAccount as String: key, kSecReturnData as String: true,
                                    kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
