import UIKit
import SwiftUI

// Text(verbatim: "Inside a comment") is not a finding.
/* Nested /* block */ comment with "quotes" */

struct StationView: View {
    var body: some View {
        Text(verbatim: "Transmit blocked")  // expect-finding
        Text(verbatim: "\(callsign) was heard")  // expect-finding
    }
}

final class LegacyController: UIViewController {
    func show() {
        label.text = "Ready"  // expect-finding
        button.setTitle("Tune", for: .normal)  // expect-finding
        let alert = UIAlertController(title: "Careful", message: "The PA is hot.", preferredStyle: .alert)  // expect-finding (two literals)
        let content = UNMutableNotificationContent()
        content.title = #"Raw "title""#  // expect-finding
        // The multi-line literal below is a finding too, reported at its
        // opening quotes; a marker cannot go on that line in valid Swift.
        content.body = """
            Multi-line body text
            """
    }
}
