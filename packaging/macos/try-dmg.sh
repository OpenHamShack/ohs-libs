#!/bin/bash
# Prove a .dmg on the Mac mini over SSH: mount it, check Gatekeeper's
# verdict, open the app in the logged-in user's session, photograph it,
# check its sockets, quit it, unmount.
#
#   packaging/macos/try-dmg.sh <dmg> <executable> <png> [seconds]
#
# The photograph is WKWebView's own snapshot of the page
# (SHACK_DESKTOP_SNAPSHOT, shack_desktop/shell.py): `screencapture` over
# SSH is refused for want of Screen Recording permission, which only
# someone at the screen can grant. The window's existence and size come
# from CGWindowListCopyWindowInfo, which needs no permission (as
# openhamshack-nb's Mac proof did it). The app is quit with SIGTERM, which the
# shell turns into closing its window; no AppleEvent, so no Automation
# prompt is left on the screen.
set -euo pipefail
dmg="$1" exe="$2" png="$3" secs="${4:-12}"
mnt="$(mktemp -d "${TMPDIR:-/tmp}/shack-dmg.XXXXXX")"
hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$mnt" "$dmg" >/dev/null
trap 'hdiutil detach "$mnt" -quiet || true; rmdir "$mnt" 2>/dev/null || true' EXIT
app="$(find "$mnt" -maxdepth 1 -name '*.app' | head -1)"
echo "== Gatekeeper"
spctl -a -t exec -vv "$app" 2>&1 | sed 's/^/   /' || true
echo "== Open $(basename "$app")"
# The app writes its snapshot and log in this user's temporary folder and
# this script copies them out: an app opened from Finder that writes to
# an external volume (/Volumes/OpenHamShackBuild) makes macOS ask the person
# at the screen for permission first, and the app waits for the answer -
# the first runs of this script left two such prompts behind.
scratch="$(mktemp -d)"
shot="$scratch/shot.png"
open -n "$app" --env "SHACK_DESKTOP_SNAPSHOT=$shot" --env "SHACK_DESKTOP_LOG=$scratch/shell.log"
sleep "$secs"
pid="$(pgrep -ox "$exe" || true)"      # the oldest: the shell, not its server
[ -n "$pid" ] || { echo "   not running after ${secs}s"; exit 1; }
pids="$(pgrep -x "$exe" | tr '\n' ' ')"
echo "   processes: $pids"
for p in $pids; do { lsof -nP -a -p "$p" -iTCP -sTCP:LISTEN 2>/dev/null || true; } | awk 'NR>1 {print "   listening " $9 " (pid " $2 ")"}'; done
if { lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || true; } | awk -v ps=" $pids " 'index(ps, " " $2 " ") && $9 !~ /^127\.0\.0\.1:/' | grep -q .; then
    echo "   LISTENS BEYOND LOOPBACK"; exit 1
fi
/usr/bin/python3 - "$pids" <<'PY' || true
import sys, ctypes, ctypes.util
# CGWindowListCopyWindowInfo through ctypes and CoreFoundation, so this
# needs nothing installed (the system Python has no PyObjC).
pids = {int(p) for p in sys.argv[1].split()}
cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
cf.CFArrayGetCount.restype = ctypes.c_long
cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
cf.CFDictionaryGetValue.restype = ctypes.c_void_p
cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
cg.CGRectMakeWithDictionaryRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
key = lambda s: cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)
arr = cg.CGWindowListCopyWindowInfo(1 | 16, 0)   # on screen only, excluding desktop elements
for i in range(cf.CFArrayGetCount(arr)):
    d = cf.CFArrayGetValueAtIndex(arr, i)
    n = ctypes.c_int(0)
    cf.CFNumberGetValue(cf.CFDictionaryGetValue(d, key("kCGWindowOwnerPID")), 3, ctypes.byref(n))
    if n.value not in pids:
        continue
    rect = (ctypes.c_double * 4)()
    cg.CGRectMakeWithDictionaryRepresentation(cf.CFDictionaryGetValue(d, key("kCGWindowBounds")), rect)
    if rect[2] > 100:
        print(f"   window on screen: {int(rect[2])}x{int(rect[3])} at {int(rect[0])},{int(rect[1])} (pid {n.value})")
PY
for _ in $(seq 20); do [ -s "$shot" ] && break; sleep 0.5; done
if [ -s "$shot" ]; then cp "$shot" "$png"; echo "   snapshot $png ($(stat -f %z "$png") bytes)"; else echo "   no snapshot written"; fi
echo "== Quit (SIGTERM to the shell)"
kill -TERM "$pid"
for _ in $(seq 40); do pgrep -x "$exe" >/dev/null || break; sleep 0.25; done
if pgrep -x "$exe" >/dev/null; then
    echo "   still running: $(pgrep -x "$exe" | tr '\n' ' ')"; sed 's/^/   /' "$scratch/shell.log" 2>/dev/null || true; exit 1
fi
echo "   every process exited (window and server)"
echo "== The shell's log"
sed 's/^/   /' "$scratch/shell.log" 2>/dev/null || true
rm -rf "$scratch"
