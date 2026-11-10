// argus-maps: Apple Maps directions with traffic, through MapKit on this Mac. Free, and needs no account.
// setup.sh builds it into ~/.argus/bin/argus-maps; Argus (maps.py) runs it as
//
//   argus-maps --from "45.52,-122.68" --to "Portland Airport, OR" [--mode drive|walk|transit]
//              [--depart <seconds since 1970>] [--avoid-tolls] [--avoid-highways]
//
// and reads one line of JSON: {"routes":[{"name":"I-5 N","seconds":1440,"meters":19800,"notes":[]}]}
// or {"error":"..."}. A place is "latitude,longitude" or an address / place name. Nothing is stored.
import Foundation
import MapKit

func emit(_ object: [String: Any], code: Int32) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"error\":\"output\"}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(code)
}

func fail(_ message: String) -> Never {
    emit(["error": message], code: 1)
}

var alive: [AnyObject] = []           // searches in flight, kept until they answer
var options: [String: String] = [:]
var flags = Set<String>()
var rest = Array(CommandLine.arguments.dropFirst())
while !rest.isEmpty {
    let arg = rest.removeFirst()
    switch arg {
    case "--from", "--to", "--mode", "--depart":
        guard !rest.isEmpty else { fail("\(arg) needs a value") }
        options[arg] = rest.removeFirst()
    case "--avoid-tolls", "--avoid-highways":
        flags.insert(arg)
    default:
        fail("unknown option \(arg)")
    }
}
guard let fromText = options["--from"], let toText = options["--to"] else {
    fail("usage: argus-maps --from PLACE --to PLACE [--mode drive|walk|transit]")
}

let transport: MKDirectionsTransportType
switch options["--mode"] ?? "drive" {
case "drive": transport = .automobile
case "walk": transport = .walking
case "transit": transport = .transit
default: fail("MapKit on the Mac gives driving, walking and public transport directions")
}

func coordinate(_ text: String) -> CLLocationCoordinate2D? {
    let parts = text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
    guard parts.count == 2, let lat = Double(parts[0]), let lon = Double(parts[1]),
          (-90.0...90.0).contains(lat), (-180.0...180.0).contains(lon) else { return nil }
    return CLLocationCoordinate2D(latitude: lat, longitude: lon)
}

// A place from coordinates, or Apple Maps' best match for an address or name (near the other end of the trip).
func resolve(_ text: String, near: CLLocationCoordinate2D?, done: @escaping (MKMapItem?) -> Void) {
    if let point = coordinate(text) {
        done(MKMapItem(placemark: MKPlacemark(coordinate: point)))
        return
    }
    let request = MKLocalSearch.Request()
    request.naturalLanguageQuery = text
    if let near = near {
        request.region = MKCoordinateRegion(center: near, latitudinalMeters: 300_000, longitudinalMeters: 300_000)
    }
    let search = MKLocalSearch(request: request)
    alive.append(search)
    search.start { response, _ in
        done(response?.mapItems.first)
    }
}

func directions(from source: MKMapItem, to destination: MKMapItem) {
    let request = MKDirections.Request()
    request.source = source
    request.destination = destination
    request.transportType = transport
    request.requestsAlternateRoutes = true
    if let depart = options["--depart"], let seconds = Double(depart) {
        request.departureDate = Date(timeIntervalSince1970: seconds)
    } else {
        request.departureDate = Date()            // "now" makes MapKit include the current traffic
    }
    if #available(macOS 13.0, *) {
        if flags.contains("--avoid-tolls") { request.tollPreference = .avoid }
        if flags.contains("--avoid-highways") { request.highwayPreference = .avoid }
    }
    let lookup = MKDirections(request: request)
    alive.append(lookup)
    if transport == .transit {                    // MapKit gives public transport times, not the routes themselves
        lookup.calculateETA { response, error in
            guard let eta = response else { fail(error?.localizedDescription ?? "no public transport route found") }
            emit(["routes": [["name": "public transport", "seconds": Int(eta.expectedTravelTime),
                              "meters": Int(eta.distance), "notes": [String]()]]], code: 0)
        }
        return
    }
    lookup.calculate { response, error in
        guard let routes = response?.routes, !routes.isEmpty else {
            fail(error?.localizedDescription ?? "no route found")
        }
        let found: [[String: Any]] = routes.prefix(3).map { route in
            var notes = route.advisoryNotices
            if #available(macOS 13.0, *), route.hasTolls { notes.append("has tolls") }
            return ["name": route.name, "seconds": Int(route.expectedTravelTime), "meters": Int(route.distance),
                    "notes": notes]
        }
        emit(["routes": found], code: 0)
    }
}

DispatchQueue.main.asyncAfter(deadline: .now() + 30) { fail("Apple Maps didn't answer in time") }
resolve(fromText, near: coordinate(toText)) { source in
    guard let source = source else { fail("Apple Maps couldn't find \(fromText)") }
    resolve(toText, near: source.placemark.coordinate) { destination in
        guard let destination = destination else { fail("Apple Maps couldn't find \(toText)") }
        directions(from: source, to: destination)
    }
}
dispatchMain()
