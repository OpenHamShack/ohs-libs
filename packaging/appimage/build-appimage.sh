#!/usr/bin/env bash
# Build an OpenHamShack app's AppImage from its descriptor, inside a
# Debian bookworm container (Containerfile says why bookworm).
#
#   packaging/appimage/build-appimage.sh <shack-app.toml | stage-dir> [--out DIR]
#
# The app is frozen with PyInstaller (one folder, packaging/pyinstaller/
# shack-app.spec, the same spec as the Windows and macOS builds) and
# wrapped with appimagetool. The two downloads are pinned by version and
# SHA-256 (from GitHub's own release digests) and cached in
# $SHACK_APPIMAGE_CACHE (default ~/build/shack-appimage-cache): a changed
# upstream file stops the build instead of shipping. The container image
# is tagged with a hash of the Containerfile, so changing the file
# rebuilds it and nothing else does.
#
# Needs podman (rootless is fine) and network for the first build.
# Output: OpenHamShack-<Name>-<version>-linux-x86_64.AppImage and .sha256.
# Test it without FUSE: ./X.AppImage --appimage-extract-and-run.
set -euo pipefail

kit="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PY="${SHACK_PY:-python3}"
src="${1:?usage: build-appimage.sh <shack-app.toml|stage-dir> [--out DIR]}"
shift
out="$(pwd)"
while [ $# -gt 0 ]; do
    case "$1" in
        --out) out="$(mkdir -p "$2" && cd "$2" && pwd)"; shift 2 ;;
        *) echo "build-appimage.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
command -v podman >/dev/null || { echo "build-appimage.sh: needs podman" >&2; exit 1; }

cache="${SHACK_APPIMAGE_CACHE:-$HOME/build/shack-appimage-cache}"
mkdir -p "$cache/tools" "$cache/pip"

# name -> "sha256 url". appimagetool 1.9.1 and type2-runtime 20251108, the
# newest tagged releases on 09/10/2026 (the "continuous" tags move).
pin() {
    case "$1" in
        appimagetool) echo "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0 https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage" ;;
        runtime-x86_64) echo "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64" ;;
    esac
}
fetch() {
    local name="$1" sha url dest
    read -r sha url <<<"$(pin "$name")"
    dest="$cache/tools/$name"
    if [ -f "$dest" ] && echo "$sha  $dest" | sha256sum -c --status; then
        return
    fi
    echo "   fetching $url"
    curl -fsSL -o "$dest.part" "$url"
    if ! echo "$sha  $dest.part" | sha256sum -c --status; then
        rm -f "$dest.part"
        echo "build-appimage.sh: $name does not match its pinned SHA-256; upstream changed it - check and update the pin" >&2
        exit 1
    fi
    mv "$dest.part" "$dest"
    chmod 755 "$dest"
}
echo "== pinned tools"
fetch appimagetool
fetch runtime-x86_64

work="$(mktemp -d "${TMPDIR:-$HOME/build}/shack-appimage.XXXXXX")"
trap 'rm -rf "$work"' EXIT
if [ -d "$src" ]; then
    stage="$(cd "$src" && pwd)"
else
    stage="$work/stage"
    "$PY" "$kit/tools/shackpkg.py" stage "$src" "$stage" >/dev/null
fi

image="localhost/shack-appimage-build:$(sha256sum "$kit/appimage/Containerfile" | cut -c1-12)"
if ! podman image exists "$image"; then
    echo "== build image $image"
    podman build -q -t "$image" -f "$kit/appimage/Containerfile" "$kit/appimage" >/dev/null
fi

echo "== build in $image"
podman run --rm \
    -v "$kit:/kit:ro" -v "$stage:/stage:ro" -v "$cache/tools:/tools:ro" \
    -v "$cache/pip:/cache/pip" -v "$out:/out" \
    "$image" /kit/appimage/in-container.sh

file_name="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["file_name"])' "$stage/app.json")"
img="$out/$file_name-linux-x86_64.AppImage"
( cd "$out" && sha256sum "$(basename "$img")" > "$(basename "$img").sha256" )
echo "built $img ($(du -h "$img" | cut -f1))"
