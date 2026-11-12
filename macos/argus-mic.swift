// argus-mic: which other apps are using a microphone right now (a call in FaceTime or the Phone app, Zoom,
// Teams, a meeting in a browser...). Argus runs it as
//
//   argus-mic --watch --except <Argus's process id>
//
// and reads one line of JSON each time the list changes: {"apps":["com.apple.FaceTime"]}. Without --watch it
// prints the list once. It reads only which processes have a microphone open (Core Audio's process list,
// macOS 14 and later); it never records or hears anything. It exits when Argus does.
import CoreAudio
import Foundation

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func processObjects() -> [AudioObjectID] {
    var where_ = address(kAudioHardwarePropertyProcessObjectList)
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &where_, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &where_, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func number<T: FixedWidthInteger>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ type: T.Type) -> T? {
    var where_ = address(selector)
    var value: T = 0
    var size = UInt32(MemoryLayout<T>.size)
    return AudioObjectGetPropertyData(object, &where_, 0, nil, &size, &value) == noErr ? value : nil
}

func bundleID(_ object: AudioObjectID) -> String {
    var where_ = address(kAudioProcessPropertyBundleID)
    var text: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &text) { AudioObjectGetPropertyData(object, &where_, 0, nil, &size, $0) }
    guard status == noErr, let found = text?.takeRetainedValue() else { return "" }
    return found as String
}

func listening(except skip: Set<pid_t>) -> [String] {
    var apps = Set<String>()
    for object in processObjects() {
        guard number(object, kAudioProcessPropertyIsRunningInput, UInt32.self) == 1 else { continue }
        let pid = number(object, kAudioProcessPropertyPID, Int32.self) ?? -1
        if skip.contains(pid) { continue }
        let name = bundleID(object)
        apps.insert(name.isEmpty ? "pid \(pid)" : name)
    }
    return apps.sorted()
}

func emit(_ apps: [String]) {
    let data = (try? JSONSerialization.data(withJSONObject: ["apps": apps])) ?? Data("{\"apps\":[]}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

var watch = false
var skip: Set<pid_t> = [getpid()]
var rest = Array(CommandLine.arguments.dropFirst())
while !rest.isEmpty {
    let arg = rest.removeFirst()
    switch arg {
    case "--watch": watch = true
    case "--except":
        guard let value = rest.first, let pid = Int32(value) else {
            FileHandle.standardError.write(Data("--except needs a process id\n".utf8)); exit(2)
        }
        rest.removeFirst()
        skip.insert(pid)
    default:
        FileHandle.standardError.write(Data("usage: argus-mic [--watch] [--except PID]\n".utf8)); exit(2)
    }
}

if !watch {
    emit(listening(except: skip))
    exit(0)
}
let parent = getppid()
var last: [String]? = nil
while getppid() == parent {               // Argus quit: stop too
    let now = listening(except: skip)
    if now != last {
        emit(now)
        last = now
    }
    Thread.sleep(forTimeInterval: 1.0)
}
