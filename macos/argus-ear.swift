// argus-ear: Argus's microphone and speaker in one, with Apple's echo cancellation.
//
// Apple's voice processing (the same one FaceTime uses) removes from the microphone what the speakers are
// playing, plus background noise. It works best when the voice is played through the same audio engine, so
// Argus's voice goes through here too. Then Argus hears you clearly while it talks and doesn't hear itself.
//
//   argus-ear [mic rate, default 16000] [voice rate, default 24000]
//   stdout: the microphone, 16-bit mono PCM at the mic rate (after echo cancellation)
//   stdin:  one command per line: "play <id> <wav file>", "stop", "quit"
//   stderr: events, one per line: "ready", "done <id>", "error <message>"
//
// Build: swiftc -O -o argus-ear argus-ear.swift   (setup.sh does this)
import AVFoundation
import Foundation

setvbuf(stderr, nil, _IONBF, 0)
let events = DispatchQueue(label: "argus.ear.events")
func event(_ text: String) {
    events.async { FileHandle.standardError.write((text + "\n").data(using: .utf8)!) }
}

let args = CommandLine.arguments.dropFirst().compactMap { Double($0) }
let micRate = args.count > 0 ? args[0] : 16000
let voiceRate = args.count > 1 ? args[1] : 24000

let engine = AVAudioEngine()
let input = engine.inputNode
do {
    try input.setVoiceProcessingEnabled(true)
} catch {
    event("error voice processing isn't available: \(error.localizedDescription)")
    exit(2)
}
if #available(macOS 14.0, *) {
    // don't turn other apps' sound (music, videos) down while Argus listens
    input.voiceProcessingOtherAudioDuckingConfiguration =
        AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
}

// ---- the voice: played through this engine, so the echo canceller knows what to remove ----
let player = AVAudioPlayerNode()
engine.attach(player)
guard let voiceFormat = AVAudioFormat(standardFormatWithSampleRate: voiceRate, channels: 1) else { exit(3) }
engine.connect(player, to: engine.mainMixerNode, format: voiceFormat)

// ---- the microphone: converted to 16-bit mono at the mic rate ----
let micFormat = input.outputFormat(forBus: 0)
guard let pcm16 = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: micRate, channels: 1, interleaved: true),
      let toPCM16 = AVAudioConverter(from: micFormat, to: pcm16) else {
    event("error can't convert the microphone's audio (\(micFormat))")
    exit(3)
}
let stdout = FileHandle.standardOutput
let writer = DispatchQueue(label: "argus.ear.writer")       // never block the audio thread on a slow reader
input.installTap(onBus: 0, bufferSize: 1024, format: micFormat) { buffer, _ in
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * micRate / micFormat.sampleRate) + 32
    guard let out = AVAudioPCMBuffer(pcmFormat: pcm16, frameCapacity: capacity) else { return }
    var given = false
    var failure: NSError?
    toPCM16.convert(to: out, error: &failure) { _, status in
        if given {
            status.pointee = .noDataNow
            return nil
        }
        given = true
        status.pointee = .haveData
        return buffer
    }
    guard failure == nil, out.frameLength > 0, let samples = out.int16ChannelData else { return }
    let data = Data(bytes: samples[0], count: Int(out.frameLength) * 2)
    writer.async { stdout.write(data) }
}

func load(_ path: String) -> AVAudioPCMBuffer? {
    guard let file = try? AVAudioFile(forReading: URL(fileURLWithPath: path)),
          let source = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
          (try? file.read(into: source)) != nil else { return nil }
    if file.processingFormat == voiceFormat { return source }
    guard let convert = AVAudioConverter(from: file.processingFormat, to: voiceFormat) else { return nil }
    let frames = AVAudioFrameCount(Double(source.frameLength) * voiceRate / file.processingFormat.sampleRate) + 64
    guard let target = AVAudioPCMBuffer(pcmFormat: voiceFormat, frameCapacity: frames) else { return nil }
    var given = false
    var failure: NSError?
    convert.convert(to: target, error: &failure) { _, status in
        if given {
            status.pointee = .endOfStream
            return nil
        }
        given = true
        status.pointee = .haveData
        return source
    }
    return failure == nil ? target : nil
}

engine.prepare()
do {
    try engine.start()
} catch {
    event("error the audio engine didn't start: \(error.localizedDescription)")
    exit(4)
}
player.play()
event("ready")

let parent = getppid()
DispatchQueue.global().async {                // Argus quit: stop too
    while getppid() == parent { sleep(1) }
    exit(0)
}

while let line = readLine() {
    let parts = line.split(separator: " ", maxSplits: 2).map(String.init)
    guard let command = parts.first else { continue }
    switch command {
    case "play" where parts.count == 3:
        let id = parts[1]
        guard let buffer = load(parts[2]) else {
            event("error can't read \(parts[2])")
            event("done \(id)")
            continue
        }
        if !player.isPlaying { player.play() }
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in event("done \(id)") }
    case "stop":
        player.stop()                               // drops what's scheduled; their "done" events follow
        player.play()
    case "quit":
        engine.stop()
        exit(0)
    default:
        event("error unknown command \(command)")
    }
}
engine.stop()
