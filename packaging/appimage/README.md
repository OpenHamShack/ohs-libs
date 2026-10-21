# AppImage

    packaging/appimage/build-appimage.sh <shack-app.toml | stage-dir> [--out DIR]

One file that runs on any x86-64 Linux from Debian 12 / Ubuntu 22.04 on:
the app frozen by PyInstaller (one folder, the kit's shared spec) inside a
Debian bookworm container, so it needs nothing newer than glibc 2.36, and
wrapped by appimagetool 1.9.1 with the type2 runtime 20251108 (both pinned
by SHA-256 in `build-appimage.sh`).

## The web engine is the host's

A web-UI app's window is pywebview on WebKitGTK. The AppImage carries
Python, pywebview and PyGObject (3.48, the last series on
libgirepository-1.0, which bookworm through trixie and Ubuntu 22.04-24.04
all ship), but **not** GTK or WebKitGTK:

- WebKitGTK is about 100 MB, and its helper processes (WebKitWebProcess,
  WebKitNetworkProcess) are found by paths compiled into the library, so
  a relocated copy inside an AppImage needs patching that breaks with each
  WebKit release.
- A bundled GLib or libstdc++ from bookworm would be loaded ahead of the
  host's newer ones and break the host's WebKitGTK; `host-libs.txt` lists
  what is taken out of the bundle for that reason.

So a desktop with WebKitGTK 4.1 (`gir1.2-webkit2-4.1` on Debian/Ubuntu,
`webkit2gtk4.1` on Fedora; most GNOME and KDE desktops have it) gets the
app in its own window, and one without gets it in the default browser,
which is shack_desktop's designed fallback (`python/shack_desktop/docs/
DESKTOP.md`). Nothing fails either way.

## Testing it

- On a machine without FUSE (containers, CI): `./X.AppImage
  --appimage-extract-and-run`. The runtime leaves its extraction in
  `/tmp/appimage_extracted_<hash>` for reuse; delete it afterwards.
- Headless: `dbus-run-session -- xvfb-run -a ./X.AppImage
  --appimage-extract-and-run`. The fresh D-Bus session matters on a
  machine with no desktop: there GTK waits 25 s for an
  xdg-desktop-portal that never starts before it shows the window.
- Verified 09/10/2026: built on ohs-dev, shown in WebKitGTK under Xvfb
  on Debian 13 (host WebKitGTK 2.54), and served to the browser fallback
  inside a bare bookworm container (glibc 2.36, no GTK).
