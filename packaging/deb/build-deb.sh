#!/usr/bin/env bash
# Build an OpenHamShack app's .deb from its shack-app.toml, with plain
# dpkg-deb.
#
#   packaging/deb/build-deb.sh <shack-app.toml | stage-dir> [--out DIR]
#
# Generalised from openhamshack-nb's tools/build-deb.sh, and the same in kind:
# no debhelper (one launcher, one desktop entry, icons, metainfo and a
# Python tree are not worth its rules/control machinery), Architecture:
# all, and the app runs on the system's python3. What was per-app there
# comes from the descriptor here (packaging/README.md): names, version,
# Depends/Recommends, summary and description, icons from the brand kit.
#
# Layout, the same for every app:
#   /usr/bin/<executable>                 sh launcher (below)
#   /usr/lib/<executable>/                the app's Python tree + launcher.py
#   /usr/lib/<executable>/vendor/         [linux] vendor: pure-Python pip
#                                         packages Debian lacks or has too old
#   /usr/share/applications/<id>.desktop
#   /usr/share/icons/hicolor/<n>x<n>/apps/<id>.png, scalable/apps/<id>.svg
#   /usr/share/metainfo/<id>.metainfo.xml
#   /usr/share/doc/<executable>/copyright
#
# The private tree under /usr/lib rather than site-packages keeps us off the
# distro's module path (we never shadow a system package, and two apps can
# carry different shack_desktop versions side by side), as openhamshack-nb does.
# Vendored packages must be pure Python: the script refuses any compiled
# file, since an Architecture: all package must not carry one.
#
# Output: OpenHamShack-<Name>-<version>-linux-all.deb (REPO-LAYOUT §5) and
# its .sha256, in --out (default: the current directory, never the repo).
set -euo pipefail

kit="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PY="${SHACK_PY:-python3}"
src="${1:?usage: build-deb.sh <shack-app.toml|stage-dir> [--out DIR]}"
shift
out="$(pwd)"
while [ $# -gt 0 ]; do
    case "$1" in
        --out) out="$(mkdir -p "$2" && cd "$2" && pwd)"; shift 2 ;;
        *) echo "build-deb.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
for t in dpkg-deb gzip md5sum sha256sum; do
    command -v "$t" >/dev/null || { echo "build-deb.sh: needs $t" >&2; exit 1; }
done

work="$(mktemp -d "${TMPDIR:-$HOME/build}/shack-deb.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ -d "$src" ]; then
    stage="$src"
else
    stage="$work/stage"
    "$PY" "$kit/tools/shackpkg.py" stage "$src" "$stage" >/dev/null
fi
j() { "$PY" -c 'import json,sys; v=json.load(open(sys.argv[1])); [v:=v[k] for k in sys.argv[2].split(".")]; print(", ".join(v) if isinstance(v, list) else v)' "$stage/app.json" "$1"; }

id="$(j id)"; name="$(j name)"; version="$(j version)"; exe="$(j executable)"
summary="$(j summary)"; section="$(j linux.section)"; file_name="$(j file_name)"
depends="$(j linux.depends)"; recommends="$(j linux.recommends)"
maintainer="$(j maintainer)"; homepage="$(j homepage)"
vendor_reqs="$("$PY" -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["linux"]["vendor"]))' "$stage/app.json")"

root="$work/pkgroot"
lib="$root/usr/lib/$exe"
mkdir -p "$root/DEBIAN" "$lib" "$root/usr/bin" "$root/usr/share/applications" \
    "$root/usr/share/icons/hicolor/scalable/apps" "$root/usr/share/metainfo" "$root/usr/share/doc/$exe"

# --- payload -----------------------------------------------------------------
cp -a "$stage/src/." "$lib/"
install -m 644 "$stage/launcher.py" "$lib/launcher.py"
if [ -n "$vendor_reqs" ]; then
    "$PY" -m pip --version >/dev/null 2>&1 || {
        echo "build-deb.sh: $PY has no pip, needed to vendor: $vendor_reqs (set SHACK_PY to a venv's python)" >&2; exit 1; }
    # Plain --target install with this machine's markers: the target is
    # Linux too, and the compiled-file check below catches anything that
    # is not pure Python.
    # shellcheck disable=SC2086
    "$PY" -m pip install --quiet --disable-pip-version-check --no-compile --target "$lib/vendor" $vendor_reqs
    rm -rf "$lib/vendor/bin"
    # pywebview carries the other platforms' engines (WebView2 DLLs, the
    # Android jar, the Windows, macOS, Qt and CEF backends): dead weight on
    # Debian, where only its GTK backend runs, and the DLLs would otherwise
    # trip the compiled-file check below.
    rm -rf "$lib/vendor/webview/lib" "$lib/vendor/webview/__pyinstaller" "$lib/vendor/webview/platforms/android"
    rm -f "$lib/vendor/webview/platforms/"{winforms,edgechromium,win32,mshtml,cocoa,qt,cef}.py
    compiled="$(find "$lib/vendor" -type f \( -name '*.so' -o -name '*.so.*' -o -name '*.pyd' -o -name '*.dylib' -o -name '*.dll' \) | head -5)"
    if [ -n "$compiled" ]; then
        echo "build-deb.sh: vendored packages carry compiled files, not allowed in Architecture: all:" >&2
        echo "$compiled" >&2
        exit 1
    fi
fi
find "$lib" -depth -name '__pycache__' -type d -exec rm -rf {} +
find "$lib" -name '*.py[co]' -delete

# --- launcher ------------------------------------------------------------------
# POSIX sh (Debian's /bin/sh is dash). The install root is worked out from
# the script's own location, so the same file works at /usr/bin and in a
# `dpkg-deb -x` extraction (as openhamshack-nb's packaging tests use it).
# PYTHONPATH rather than a sys.path edit in launcher.py: the app's server
# may be a second process started as `python3 -m <app>` (shack_desktop's
# self_command), and it must find the same tree.
cat > "$root/usr/bin/$exe" <<EOF
#!/bin/sh
# Launcher for $name (packaging/deb/build-deb.sh).
set -e
self=\$(readlink -f "\$0" 2>/dev/null) || self="\$0"
libdir="\$(dirname "\$(dirname "\$self")")/lib/$exe"
PYTHONPATH="\$libdir:\$libdir/vendor\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 "\$libdir/launcher.py" "\$@"
EOF

# --- desktop integration -------------------------------------------------------
cats="$("$PY" -c 'import json,sys; print(";".join(json.load(open(sys.argv[1]))["categories"]) + ";")' "$stage/app.json")"
keys="$("$PY" -c 'import json,sys; k=json.load(open(sys.argv[1]))["keywords"]; print(";".join(k) + ";" if k else "")' "$stage/app.json")"
cat > "$root/usr/share/applications/$id.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$name
Comment=$summary
Exec=$exe
Icon=$id
Terminal=false
StartupNotify=true
Categories=$cats
Keywords=$keys
EOF
for s in 16 32 48 64 128 256 512; do
    install -D -m 644 "$stage/icons/$s.png" "$root/usr/share/icons/hicolor/${s}x${s}/apps/$id.png"
done
install -m 644 "$stage/icons/app.svg" "$root/usr/share/icons/hicolor/scalable/apps/$id.svg"
"$PY" - "$stage/app.json" "$root/usr/share/metainfo/$id.metainfo.xml" <<'EOF'
import json, sys
from xml.sax.saxutils import escape
a = json.load(open(sys.argv[1]))
paras = "".join(f"<p>{escape(' '.join(p.split()))}</p>" for p in a["description"].split("\n\n"))
open(sys.argv[2], "w").write(f"""<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>{a['id']}</id>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>{escape(a['license'])}</project_license>
  <name>{escape(a['name'])}</name>
  <summary>{escape(a['summary'])}</summary>
  <description>{paras}</description>
  <launchable type="desktop-id">{a['id']}.desktop</launchable>
  <url type="homepage">{escape(a['homepage'])}</url>
  <developer id="com.openhamshack"><name>OpenHamShack</name></developer>
  <releases><release version="{escape(a['version'])}"/></releases>
</component>
""")
EOF

# --- copyright (machine-readable, pointing at the system's GPL text) -----------
cat > "$root/usr/share/doc/$exe/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: $exe
Source: $homepage

Files: *
Copyright: Brian Henderson, GM4BRI, and the OpenHamShack contributors
License: GPL-3.0-or-later
 On Debian systems the full text is in /usr/share/common-licenses/GPL-3.
EOF

# --- maintainer scripts: cache refreshes, shared by every app -------------------
install -m 755 "$kit/deb/postinst" "$root/DEBIAN/postinst"
install -m 755 "$kit/deb/postrm" "$root/DEBIAN/postrm"

# --- modes: everything world-readable whatever the umask ------------------------
find "$root/usr" -type d -exec chmod 755 {} +
find "$root/usr" -type f -exec chmod 644 {} +
chmod 755 "$root/usr/bin/$exe"

installed_size="$(du -sk "$root/usr" | cut -f1)"
long="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1]))["description"]; print("\n".join(" "+(l if l.strip() else ".") for l in d.splitlines()))' "$stage/app.json")"
{
    echo "Package: $exe"
    echo "Version: $version"
    echo "Section: $section"
    echo "Priority: optional"
    echo "Architecture: all"
    echo "Installed-Size: $installed_size"
    echo "Depends: $depends"
    [ -n "$recommends" ] && echo "Recommends: $recommends"
    echo "Maintainer: $maintainer"
    echo "Homepage: $homepage"
    echo "Description: $summary"
    echo "$long"
} > "$root/DEBIAN/control"

( cd "$root" && find usr -type f -exec md5sum {} + ) > "$root/DEBIAN/md5sums"
chmod 644 "$root/DEBIAN/control" "$root/DEBIAN/md5sums"

deb="$out/$file_name-linux-all.deb"
dpkg-deb --build --root-owner-group "$root" "$deb" >/dev/null
( cd "$out" && sha256sum "$(basename "$deb")" > "$(basename "$deb").sha256" )
echo "built $deb ($(du -h "$deb" | cut -f1))"
