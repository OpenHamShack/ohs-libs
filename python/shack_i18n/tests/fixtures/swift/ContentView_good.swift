import SwiftUI

struct StationView: View {
    var body: some View {
        // SwiftUI literal initialisers are catalog keys, never findings.
        Text("Transmit blocked")
        Button("Tune") { tune() }
        Text(verbatim: callsign)
        Text(verbatim: "\(freq)")  // data already formatted
        Text(verbatim: "14.074")
        label.text = String(localized: "Ready")
        print("Ready to transmit")
    }
}
