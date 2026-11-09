import SwiftUI

struct ContentView: View {
    @Bindable var model: AppModel
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Label(model.macStatus, systemImage: "desktopcomputer")
                    Label(model.glassesStatus, systemImage: model.glassesConnected ? "eyeglasses" : "eyeglasses.slash")
                }
                .font(.subheadline)
                .frame(maxWidth: .infinity, alignment: .leading)

                if !model.heard.isEmpty {
                    Text("You: \(model.heard)").font(.callout).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                ScrollView {
                    Text(model.reply.isEmpty ? "Hold the button and talk to Argus." : model.reply)
                        .font(.title3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                Button {
                    Task { await model.look() }
                } label: {
                    Label("Look through my glasses", systemImage: "camera.viewfinder")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(!model.glassesConnected || model.busy)

                Circle()
                    .fill(model.listening ? Color.red : (model.busy ? Color.gray : Color.accentColor))
                    .frame(width: 140, height: 140)
                    .overlay(Image(systemName: model.busy ? "ellipsis" : "mic.fill").font(.system(size: 48)).foregroundStyle(.white))
                    .accessibilityLabel("Hold to talk to Argus")
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { _ in if !model.listening { model.beginTalking() } }
                        .onEnded { _ in Task { await model.endTalking() } })
            }
            .padding()
            .navigationTitle("Argus")
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
            }
            .sheet(isPresented: $showSettings, onDismiss: { Task { await model.refreshMac() } }) {
                SettingsView(glasses: model.glasses)
            }
        }
    }
}

struct SettingsView: View {
    let glasses: GlassesLink
    @AppStorage("macAddress") private var macAddress = ""
    @State private var token = Keychain.read("phoneToken") ?? ""
    @State private var message = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Your Mac (over Tailscale)") {
                    TextField("http://100.x.y.z:8765", text: $macAddress)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    SecureField("Token (argusctl phone-token)", text: $token)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                Section("Meta glasses") {
                    Text(glasses.isRegistered ? "Linked to your glasses" : "Not linked yet")
                    Button("Link my glasses (opens Meta AI)") {
                        Task {
                            do { try await glasses.register() } catch { message = error.localizedDescription }
                        }
                    }
                }
                if !message.isEmpty { Text(message).foregroundStyle(.red) }
            }
            .navigationTitle("Settings")
            .toolbar {
                Button("Done") {
                    Keychain.save("phoneToken", token.trimmingCharacters(in: .whitespacesAndNewlines))
                    dismiss()
                }
            }
        }
    }
}
