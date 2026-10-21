// swift-tools-version:5.9
// ohs-libs' Swift libraries (REPO-LAYOUT.md §2): the client side that
// OpenHamShack Remote needs on iPhone, iPad and Apple TV. The manifest sits
// at the repository root because SwiftPM resolves a package by its git tag
// from the root; the sources live under swift/.
//
// Tools version 5.9 (Swift 5 language mode) rather than 6: the store and
// sync client are lock-based classes shared between the UI and a sync
// thread, as in the Python and Kotlin ports, and Swift 6's strict checking
// would force an actor-shaped rewrite that the three ports could no longer
// be read side by side with. No third-party dependency: SQLite is the
// system's (SQLite3 module), HTTP is URLSession.
import PackageDescription

let package = Package(
    name: "ShackClient",
    platforms: [.iOS(.v16), .tvOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "ShackClient", targets: ["ShackClient"]),
        .library(name: "ShackFormat", targets: ["ShackFormat"]),
    ],
    targets: [
        .target(name: "ShackClient", path: "swift/Sources/ShackClient", linkerSettings: [.linkedLibrary("sqlite3")]),
        .target(name: "ShackFormat", path: "swift/Sources/ShackFormat"),
        // The tests read spec/vectors from the checkout (#filePath), never a
        // copy: the vectors are the contract between the ports.
        .testTarget(name: "ShackClientTests", dependencies: ["ShackClient"], path: "swift/Tests/ShackClientTests"),
        .testTarget(name: "ShackFormatTests", dependencies: ["ShackFormat", "ShackClient"], path: "swift/Tests/ShackFormatTests"),
    ]
)
