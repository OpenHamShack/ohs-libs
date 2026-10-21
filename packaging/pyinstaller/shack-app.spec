# -*- mode: python -*-
# PyInstaller spec for any OpenHamShack app, from its staged folder
# (packaging/tools/shackpkg.py). Used by the AppImage, Windows and macOS
# builders alike, so the three frozen forms of an app contain the same
# thing:
#
#   SHACK_STAGE=<stage> pyinstaller --distpath DIST --workpath WORK shack-app.spec
#
# One-folder (onedir), never one-file: a one-file program unpacks itself
# to a temporary folder on every start (slow, and antivirus products
# quarantine it), and the AppImage and .app are single files to the user
# anyway.
#
# The app's own packages are collected whole (every submodule, every data
# file) rather than left to import analysis: apps load parts dynamically
# (a server started as `<exe> --server`, plugins, static files), and a
# missing module in a frozen build is only found on someone's PC.
import json
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

stage = Path(os.environ["SHACK_STAGE"]).resolve()
sys.path.insert(0, str(stage / "src"))      # collect_all imports the packages
app = json.loads((stage / "app.json").read_text())

datas, binaries, hidden = [], [], []
for pkg in app["python"]["packages"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h

excludes = [
    # Never Qt: pywebview's Qt backend would pull in QtWebEngine, 100+ MB
    # (PACKAGING.md), and shack_desktop never asks for it.
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "webview.platforms.qt",
    # pywebview's CEF and Android backends, never used on a desktop.
    "webview.platforms.cef", "webview.platforms.android", "cefpython3",
]
if sys.platform != "win32":
    excludes += ["webview.platforms.winforms", "webview.platforms.edgechromium", "webview.platforms.mshtml",
                 "webview.platforms.win32", "clr", "clr_loader", "pythonnet"]
else:
    # The Internet Explorer engine: shack_desktop uses the browser instead
    # when WebView2 is missing (shell.py).
    excludes += ["webview.platforms.mshtml"]
if sys.platform != "darwin":
    excludes += ["webview.platforms.cocoa"]

if sys.platform.startswith("linux"):
    # PyGObject is bundled, the GTK and WebKitGTK it drives are not: the
    # AppImage uses the system's (appimage/README.md). Excluding the
    # gi.repository namespaces keeps PyInstaller's hooks from copying the
    # build container's whole GTK stack and typelibs in; at run time gi's
    # own importer finds them on the host. gi's Python overrides are real
    # modules and are collected explicitly.
    from PyInstaller.utils.hooks import collect_submodules
    excludes += [f"gi.repository.{ns}" for ns in ("Gtk", "Gdk", "GdkPixbuf", "WebKit2", "JavaScriptCore",
                                                    "Soup", "GLib", "GObject", "Gio", "Pango", "cairo", "Atk",
                                                    "HarfBuzz", "freetype2", "xlib")]
    # Only the overrides of the namespaces pywebview's GTK backend uses:
    # the others (Gst, ...) would each wake a PyInstaller hook looking for
    # a typelib the build container does not have.
    keep = {"GLib", "GObject", "Gio", "Gtk", "Gdk", "Pango", "GdkPixbuf", "keysyms"}
    hidden += collect_submodules("gi", filter=lambda n: not n.startswith("gi.overrides.") or n.rsplit(".", 1)[-1] in keep)
    hidden += ["webview.platforms.gtk", "cairo"]

a = Analysis(
    [str(stage / "launcher.py")],
    pathex=[str(stage / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = None
if sys.platform == "win32":
    icon = str(stage / "icons" / "app.ico")
elif sys.platform == "darwin" and (stage / "icons" / "app.icns").exists():
    icon = str(stage / "icons" / "app.icns")   # made by macos/build-dmg.sh with iconutil

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app["executable"],
    console=False,                    # a window app: no console on Windows, no Terminal on macOS
    icon=icon,
    upx=False,                        # UPX-packed binaries are what antivirus flags first
    codesign_identity=None,           # macOS signing is build-dmg.sh's, after the bundle is whole
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=app["executable"], upx=False)

if sys.platform == "darwin":
    app_bundle = BUNDLE(
        coll,
        name=f"{app['name']}.app",
        icon=icon,
        bundle_identifier=app["macos"]["bundle_id"],
        version=app["version"],
        info_plist={
            "CFBundleName": app["name"],
            "CFBundleDisplayName": app["name"],
            "CFBundleShortVersionString": app["version"],
            "CFBundleVersion": app["version"],
            "LSMinimumSystemVersion": app["macos"]["minimum_system"],
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": f"{app['license']}, {app['publisher']}",
            # The window talks to its own server on 127.0.0.1 over plain
            # HTTP; WKWebView needs App Transport Security to allow local
            # networking for that (and nothing more).
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
