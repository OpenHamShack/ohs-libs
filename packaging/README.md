# OpenHamShack desktop packaging kit

One set of scripts that turns any OpenHamShack app into its desktop
installers: a `.deb` and an AppImage for Linux, an Inno Setup `.exe` for
Windows, and a signed, notarised `.dmg` for macOS (BETA-PLAN arch change
1: every app stands alone on all three). Each app describes itself once,
in a `shack-app.toml` at the root of its repository; every script reads
that. `example/` is a tiny web-UI app built into all four, which is how
the kit is proved.

| Path | Makes | Runs on |
|---|---|---|
| `tools/shackpkg.py` | the staged folder every builder starts from | any machine with Python 3.11+ (the brand kit must be there) |
| `deb/build-deb.sh` | `OpenHamShack-<Name>-<v>-linux-all.deb` | Debian/Ubuntu with `dpkg-deb` |
| `appimage/build-appimage.sh` | `OpenHamShack-<Name>-<v>-linux-x86_64.AppImage` | any Linux with podman (builds inside Debian bookworm) |
| `windows/build-windows.ps1` | `OpenHamShack-<Name>-<v>-windows-x64.exe` | Windows 10/11, PowerShell 5.1 |
| `macos/build-dmg.sh` | `OpenHamShack-<Name>-<v>-macos-arm64.dmg` | the Mac mini (signing identity in its keychain) |
| `pyinstaller/shack-app.spec` | the frozen app the last three wrap | (used by them) |

File names follow REPO-LAYOUT §5. Every builder also writes a `.sha256`
beside its installer, in `sha256sum` format.

## The descriptor: `shack-app.toml`

```toml
[app]
id = "com.openhamshack.icom"        # required; com.openhamshack.<kind> (REPO-LAYOUT §5)
kind = "icom"                       # required
name = "OpenHamShack Icom"          # required; the product name users see
summary = "IC-9700, IC-7610 and IC-705 control"   # required; one line
description = """Paragraphs for package managers; blank line between them."""
executable = "shack-icom"           # default shack-<kind>
version = { file = "VERSION" }      # required; or "1.2.3", or { python = "src/pkg/__init__.py" }
entry = "shack_icom.__main__:main"  # required; module or module:function, returns the exit code
icon = "icom"                       # required; a brand-kit icon (brand/icons/png/<icon>/)
categories = ["HamRadio"]           # freedesktop categories
keywords = ["IC-9700"]
license_file = "LICENSE"            # default LICENSE beside the descriptor
# homepage, publisher, maintainer, license have family defaults

[python]
paths = ["src"]                     # folders whose contents go on the app's import path
requirements = ["pywebview==6.2.1"] # pip requirements bundled into the frozen builds

[linux]
depends = ["python3 (>= 3.11)"]     # .deb Depends
recommends = ["python3-gi", "gir1.2-webkit2-4.1"]
vendor = ["pywebview==6.2.1"]       # pure-Python packages copied into the .deb (default: requirements)

[windows]
app_guid = "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"  # required; Inno AppId, made once, never changed
notices = "THIRD-PARTY-windows.txt" # required with bundles; installed as THIRD-PARTY.txt

[[windows.bundle]]                  # optional, repeatable: a program the Windows installer carries
name = "ffmpeg"                     # its folder beside the executable, first on PATH when the app starts
urls = ["https://.../ffmpeg-9.0.2-essentials_build.7z"]   # https only; mirrors after the first
sha256 = "4705843c..."              # checked at every build; a mismatch stops it
files = ["ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe"]  # archive members, taken out flat
licences = { "ffmpeg-LICENSE.txt" = "ffmpeg-9.0.2-essentials_build/LICENSE" }  # member, or { urls, sha256 }
check = { args = ["-hide_banner", "-encoders"], expect = ["libx264"] }   # optional; runs files[0]

[macos]
minimum_system = "12.0"             # bundle_id defaults to app.id

[pyinstaller]
spec = "packaging/qt-app.spec"      # optional: the app's own spec, staged as app.spec
```

The kit's `pyinstaller/shack-app.spec` is for web-UI apps: it leaves Qt
out and brings GTK's bindings in. A Qt app (QO-100 NB, Radio, DATV, ATV)
names its own spec; the AppImage builder then freezes with it (reading
`$SHACK_STAGE` as the kit's does). The Windows and macOS builders still
use the kit's spec: the Qt apps keep their own pipelines there.

`[linux] host_libs = "path"` likewise replaces `appimage/host-libs.txt`,
the libraries the AppImage takes out of its bundle and leaves to the host.
The kit's list is for WebKitGTK apps and takes every `libxcb*` out; a Qt
app keeps the xcb-util family that Qt 6's xcb plugin needs and many hosts
lack (the build image has them).

`[[windows.bundle]]` is for programs an app runs and Windows does not
have (ffmpeg, mpv): `windows/bundle.ps1` fetches each archive at build
time, never at run time, refuses it unless the SHA-256 matches, and puts
the named files in `<install>\<name>\` with their licence texts in
`licences\`. The frozen launcher puts those folders first on PATH, so
the app finds them as it finds any program and needs no per-OS code. The
check runs the first file with `args` and fails the build unless its
output holds every `expect` word: what the app relies on (an encoder,
a version) is proved when the installer is made. Linux and macOS
builds are unaffected; there the programs are the system's.

`shackpkg.py stage <descriptor> <dir>` checks all of that and resolves it
into a folder that needs nothing from this machine: `app.json` (the
resolved descriptor), `src/` (every `paths` entry merged), `launcher.py`
(the frozen entry), `requirements.txt`, `icons/` (16-1024 PNG, `.ico`,
`.svg` from the brand kit, `$SHACK_BRAND` or `~/openhamshack/brand`) and
`LICENSE`. The Linux builders stage for themselves when given a
descriptor; the Windows and Mac builders take a staged folder copied
across, because they have no Python until they fetch one and never have
the brand kit.

## Building the example

```sh
# Linux (this VM): .deb and AppImage
packaging/deb/build-deb.sh packaging/example/shack-app.toml --out ~/build/desktop-kit/out
packaging/appimage/build-appimage.sh packaging/example/shack-app.toml --out ~/build/desktop-kit/out
```

Windows and macOS: see `windows/README.md` and `macos/README.md`.
