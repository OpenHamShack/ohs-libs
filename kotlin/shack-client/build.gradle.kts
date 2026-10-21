// No runtime dependency beyond the Kotlin standard library: JSON, HTTP and
// SQLite access are written against the JDK/Android platform (java.net,
// java.sql only for the JVM driver), so the Fire TV build stays small and
// nothing here can disagree with the Remote's own pinned versions.
// sqlite-jdbc is for the tests, which run the store on a real SQLite.
dependencies {
    testImplementation("org.xerial:sqlite-jdbc:3.46.1.3")
}
