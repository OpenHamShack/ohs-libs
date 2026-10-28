// The client side of the spectrum/1, overlay/1, ui/1 and presence contracts
// (CLIENT-UI-PLAN.md §2, §3, §5, §7.2) as one module, where Python split
// shack_spectrum from shack_ui: the overlay's actions are ui actions, so
// the two cannot be consumed apart, and an app that draws a waterfall
// also draws screens. Packages stay separate (com.openhamshack.spectrum,
// .overlay, .ui, .presence) so a future split is only a build-file change.
//
// JSON comes from shack-client's reader (plain Maps and Lists) rather than a
// second parser: messages are handed in already parsed or as text, and the
// vectors are read with the same reader the other modules' tests use.
dependencies {
    api(project(":shack-client"))
}
