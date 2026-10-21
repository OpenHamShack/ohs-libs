# macOS disk image

    # on Linux: stage, then copy the stage and packaging/ to the Mac mini
    packaging/tools/shackpkg.py stage packaging/example/shack-app.toml <stage>
    # on the Mac mini (ssh mac), everything on /Volumes/OpenHamShackBuild:
    packaging/macos/build-dmg.sh --stage <stage> --work /Volumes/OpenHamShackBuild/desktop-kit/work

Makes `OpenHamShack-<Name>-<version>-macos-arm64.dmg`: the app frozen by
the kit's shared PyInstaller spec on python-build-standalone 3.13.16,
signed with the Developer ID in the Mac mini's `ohs-signing` keychain
(hardened runtime, secure timestamp, inside out), checked in server mode
under the hardened runtime, put in a disk image with an Applications
link, signed again, notarised with the `ohs-notary` keychain profile,
stapled and checked by Gatekeeper. The commands are PACKAGING.md's,
verbatim. The keychain password goes from its file straight to `security`
and the notary key never leaves its profile; neither is printed or copied.
`--no-notarize` stops before notarising, for a quick local try.

`entitlements.plist` is empty on purpose: the example runs under the
hardened runtime with no exceptions (Python, PyObjC and WKWebView on an
M4). Add one only when a launch proves it is needed.

`try-dmg.sh <dmg> <executable> <png>` proves an image over SSH: mounts it,
asks Gatekeeper, opens the app in the logged-in session, reads its window
from CGWindowListCopyWindowInfo (no permission needed), has WKWebView
write a snapshot of the page (`SHACK_DESKTOP_SNAPSHOT`: `screencapture`
over SSH is refused without Screen Recording permission), checks its
sockets are loopback only, quits it with SIGTERM, and unmounts.

Traps met on the first run (09/10/2026):

- An app opened from Finder that writes to an external volume makes
  macOS ask the person at the screen, and the app waits for the answer.
  try-dmg.sh keeps the app's writes in the user's temporary folder.
- The frozen example first hung at start: http.server's reverse DNS
  lookup of 127.0.0.1 goes to mDNS on macOS (now
  `shack_desktop.LoopbackHTTPServer`).
- The build's smoke test runs the server with a held-open stdin pipe: a
  server started by the shell exits when its stdin closes, so
  `</dev/null` would end it at once.
- arm64 only; Intel Macs need an x86_64 (or universal) Python beside it.
