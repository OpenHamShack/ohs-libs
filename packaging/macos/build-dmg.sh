#!/bin/bash
# Build an OpenHamShack app's signed, notarised .dmg on the Mac mini.
#
#   packaging/macos/build-dmg.sh --stage DIR [--work DIR] [--out DIR] [--no-notarize]
#
# --stage is the folder packaging/tools/shackpkg.py made on Linux, copied
# here (the Mac has no brand kit and, until this script fetches one, no
# Python new enough to read shack-app.toml). --work (default
# $SHACK_MAC_WORK, else ~/shack-mac-build; on the Mac mini use
# /Volumes/OpenHamShackBuild/..., the internal disk is small) holds everything
# it downloads or builds: a pinned Python, a virtualenv, PyInstaller's
# folders. Nothing is installed outside it and nothing needs an
# administrator.
#
# Steps, each a gate:
#   1. Python 3.13.16 from python-build-standalone (openhamshack-nb's pin,
#      SHA-256 checked), a venv, PyInstaller and the app's requirements.
#   2. The .icns from the staged brand-kit PNGs (iconutil), then the .app
#      from the kit's shared PyInstaller spec (one folder, arm64).
#   3. Signed with the Developer ID in the Mac mini's ohs-signing keychain,
#      exactly as PACKAGING.md says: hardened runtime, secure timestamp,
#      every Mach-O file inside first, then the bundle. The keychain is
#      unlocked with the password file's contents passed straight to
#      `security`; the password is never printed, copied or logged, and
#      neither is the notary API key (the notary profile holds it).
#   4. The signed app's server mode must refuse a request without the
#      launch token and answer one with it: under the hardened runtime,
#      which is where a missing entitlement would show.
#   5. The .dmg (app + Applications link), signed, notarised with
#      `notarytool --keychain-profile ohs-notary --wait`, stapled, and
#      checked by Gatekeeper (spctl). --no-notarize stops before
#      notarising, for a quick local try; such an image must not be shared.
#
# Output in --out (default <work>/out): OpenHamShack-<Name>-<v>-macos-arm64.dmg
# and its .sha256. arm64 only: the Mac mini is an M4, and an Intel or
# universal build needs an x86_64 Python and libraries beside it.
set -euo pipefail

kit="$(cd "$(dirname "$0")/.." && pwd)"
stage_in="" work="${SHACK_MAC_WORK:-$HOME/shack-mac-build}" out="" notarize=1
while [ $# -gt 0 ]; do
    case "$1" in
        --stage) stage_in="$2"; shift 2 ;;
        --work) work="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --no-notarize) notarize=0; shift ;;
        *) echo "build-dmg.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$stage_in" ] && [ -f "$stage_in/app.json" ] || { echo "build-dmg.sh: --stage DIR (with app.json) is required" >&2; exit 2; }
out="${out:-$work/out}"
die() { echo "build-dmg.sh: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$1"; }
for t in codesign hdiutil iconutil xcrun shasum plutil ditto curl file; do
    command -v "$t" >/dev/null || die "needs $t (Xcode Command Line Tools)"
done

IDENTITY="Developer ID Application: Brian Henderson (GTVYVL6ZY7)"
KEYCHAIN="$HOME/Library/Keychains/ohs-signing.keychain-db"
PASSFILE="$HOME/.config/ohs-signing/keychain-pass"
PROFILE="ohs-notary"
[ -f "$KEYCHAIN" ] || die "no signing keychain at $KEYCHAIN (this is the Mac mini's)"
[ -r "$PASSFILE" ] || die "no keychain password file at $PASSFILE"

mkdir -p "$work/cache" "$out"
stage="$work/stage"
rm -rf "$stage"
ditto "$stage_in" "$stage"
j() { plutil -extract "$1" raw -o - "$stage/app.json"; }
name="$(j name)"; version="$(j version)"; exe="$(j executable)"; file_name="$(j file_name)"
echo "$name $version ($(j id))"

step "Python 3.13.16 (python-build-standalone)"
py_tar="cpython-3.13.16+20261003-aarch64-apple-darwin-install_only.tar.gz"
py_sha="d8975d7df4f08f7b1c7aafcdfacbddcec3d366415f2c1a72b2466b6850815933"
py_url="https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.13.16%2B20261003-aarch64-apple-darwin-install_only.tar.gz"
if ! echo "$py_sha  $work/cache/$py_tar" | shasum -a 256 -c --status 2>/dev/null; then
    echo "   fetching $py_url"
    curl -fsSL -o "$work/cache/$py_tar.part" "$py_url"
    echo "$py_sha  $work/cache/$py_tar.part" | shasum -a 256 -c --status \
        || { rm -f "$work/cache/$py_tar.part"; die "$py_tar does not match its pinned SHA-256"; }
    mv "$work/cache/$py_tar.part" "$work/cache/$py_tar"
fi
if [ ! -x "$work/python/bin/python3" ]; then
    mkdir -p "$work/python"
    tar -xzf "$work/cache/$py_tar" -C "$work/python" --strip-components 1
fi
[ -x "$work/venv/bin/python" ] || "$work/python/bin/python3" -m venv "$work/venv"
"$work/venv/bin/pip" install --quiet --disable-pip-version-check --cache-dir "$work/cache/pip" \
    -r "$kit/macos/build-requirements.txt" -r "$stage/requirements.txt"

step "Icon and app bundle"
iconset="$work/app.iconset"
rm -rf "$iconset" && mkdir "$iconset"
for s in 16 32 128 256 512; do
    cp "$stage/icons/$s.png" "$iconset/icon_${s}x${s}.png"
    cp "$stage/icons/$((s * 2)).png" "$iconset/icon_${s}x${s}@2x.png"
done
iconutil -c icns -o "$stage/icons/app.icns" "$iconset"
SHACK_STAGE="$stage" "$work/venv/bin/pyinstaller" --noconfirm --clean --log-level WARN \
    --distpath "$work/dist" --workpath "$work/pyi" "$kit/pyinstaller/shack-app.spec"
app="$work/dist/$name.app"
[ -d "$app" ] || die "PyInstaller made no $app"

step "Sign ($IDENTITY)"
# PACKAGING.md's unlock, verbatim: the password goes from its file to
# `security` and nowhere else.
security unlock-keychain -p "$(cat "$PASSFILE")" "$KEYCHAIN"
sign() {
    codesign --force --options runtime --timestamp --keychain "$KEYCHAIN" -s "$IDENTITY" "$@" 2>&1 \
        | grep -v "replacing existing signature" || true
}
# Inside out: every Mach-O file but the main executable, deepest first, so
# each container is signed after what it contains.
find "$app/Contents" -type f ! -path "$app/Contents/MacOS/$exe" -print0 \
    | xargs -0 file -h | awk -F': ' '/Mach-O/ {print $1}' \
    | awk -F/ '{print NF "\t" $0}' | sort -rn | cut -f2- \
    | while IFS= read -r f; do sign "$f"; done
sign --entitlements "$kit/macos/entitlements.plist" "$app"
codesign --verify --deep --strict --verbose=1 "$app" 2>&1 | sed 's/^/   /'
codesign -dv "$app" 2>&1 | grep -E '^(Identifier|Authority=Developer|TeamIdentifier|Runtime|Timestamp)' | sed 's/^/   /'

step "Smoke test: the signed server needs its launch token"
port=$(( 20000 + RANDOM % 30000 ))
token="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')"
# Its stdin is a pipe this script holds open, as the shell's is: the
# server exits when its stdin closes (shack_desktop.watch_parent), so
# </dev/null would end it at once.
fifo="$work/smoke.fifo"
rm -f "$fifo" && mkfifo "$fifo"
SHACK_DESKTOP_HOST=127.0.0.1 SHACK_DESKTOP_PORT=$port SHACK_DESKTOP_TOKEN="$token" \
    "$app/Contents/MacOS/$exe" --server <"$fifo" >"$work/smoke.log" 2>&1 &
spid=$!
exec 9>"$fifo"
trap 'kill $spid 2>/dev/null || true' EXIT
for _ in $(seq 60); do
    curl -s -o /dev/null "http://127.0.0.1:$port/" && break
    kill -0 $spid 2>/dev/null || { cat "$work/smoke.log"; die "the signed server exited before it listened"; }
    sleep 0.5
done
no=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/")
yes=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Shack-Token: $token" "http://127.0.0.1:$port/")
exec 9>&-          # closing its stdin is how the shell stops it
for _ in $(seq 20); do kill -0 $spid 2>/dev/null || break; sleep 0.25; done
kill -0 $spid 2>/dev/null && { kill $spid; die "the signed server did not exit when its stdin closed"; }
rm -f "$fifo"
trap - EXIT
[ "$no" = 403 ] && [ "$yes" = 200 ] || { cat "$work/smoke.log"; die "server answered $no without the token and $yes with it"; }
echo "   403 without the token, 200 with it; exited when its stdin closed"

step "Disk image"
dmg="$out/$file_name-macos-arm64.dmg"
dmgroot="$work/dmgroot"
rm -rf "$dmgroot" "$dmg" && mkdir "$dmgroot"
ditto "$app" "$dmgroot/$name.app"
ln -s /Applications "$dmgroot/Applications"
hdiutil create -quiet -ov -fs HFS+ -format UDZO -volname "$name" -srcfolder "$dmgroot" "$dmg"
sign "$dmg"

if [ "$notarize" = 1 ]; then
    step "Notarise (notarytool, profile $PROFILE)"
    # Unlocked again here: signing was minutes ago, and other sessions on
    # the Mac mini use (and lock) the same keychain. A first submission
    # for a new bundle id has taken Apple over half an hour.
    security unlock-keychain -p "$(cat "$PASSFILE")" "$KEYCHAIN"
    xcrun notarytool submit "$dmg" --keychain-profile "$PROFILE" --keychain "$KEYCHAIN" --wait \
        --output-format plist > "$work/notary.plist"
    status="$(plutil -extract status raw -o - "$work/notary.plist")"
    sub="$(plutil -extract id raw -o - "$work/notary.plist")"
    echo "   submission $sub: $status"
    if [ "$status" != "Accepted" ]; then
        xcrun notarytool log "$sub" --keychain-profile "$PROFILE" --keychain "$KEYCHAIN" || true
        die "notarisation was not accepted"
    fi
    step "Staple and check"
    xcrun stapler staple -q "$dmg"
    xcrun stapler validate -q "$dmg" && echo "   ticket stapled"
    spctl -a -t open --context context:primary-signature -vv "$dmg" 2>&1 | sed 's/^/   /'
else
    echo; echo "NOT NOTARISED (--no-notarize): for this Mac only"
fi

( cd "$out" && shasum -a 256 "$(basename "$dmg")" > "$(basename "$dmg").sha256" )
echo
echo "built $dmg ($(du -h "$dmg" | cut -f1))"
