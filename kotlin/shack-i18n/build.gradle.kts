// ShackFormat on its own module (REPO-LAYOUT.md §2 "shack-i18n"): a screen
// that only shows a frequency does not need the store and sync with it.
// The tests read the shared vectors with shack-client's JSON reader rather
// than a second one; the library itself depends on nothing.
dependencies {
    testImplementation(project(":shack-client"))
}
