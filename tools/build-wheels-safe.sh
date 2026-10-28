#!/usr/bin/env bash
# Build the shack-sdr wheels into a folder nobody else is using, and say
# which: the only way agents should build them.
#
#   tools/build-wheels-safe.sh <topic> [linux-amd64|linux-arm64|windows-amd64|macos]...
#
# Why: tools/build-sdr-wheels.sh writes wherever --out says, and agents
# working at once picked ~/build/ohs-dist/shack-sdr-<topic> by hand, and
# three times one overwrote another's wheels (to 28/10/2026). A lock in
# openhamshack-nb names the folder its hashes came from, so an overwritten
# folder leaves a lock vouching for wheels that are gone, and vendoring
# from it again copies somebody else's. A folder is used once, ever.
#
# So this picks the next ~/build/ohs-dist/shack-sdr-<topic><n> (n = 1, 2,
# ...) that is free, where taken means either
#   - the folder exists (any state: a failed build keeps its name too), or
#   - a shack-sdr.lock names it: tools/vendor/shack-sdr.lock in every
#     checkout of openhamshack-nb (~/openhamshack-nb and any
#     ~/openhamshack-nb-*, or OHS_NB_REPOS, space-separated) and each of
#     their worktrees on disk, and as committed at each of their local
#     branches - a branch whose worktree is gone may still be merged;
# and takes it with mkdir, which fails if another build took the same
# name a moment earlier (it then tries the next).
#
# The tree must be clean: the folder records the commit it was built from
# (BUILD-INFO), which tools/vendor-shack-sdr.sh in openhamshack-nb writes
# into the lock, and a dirty tree's wheels are not that commit's.
#
# Then tools/build-sdr-wheels.sh --out <folder> builds (all four targets
# when none is named), and itself runs tools/check-encumbered.py and the
# wheel smoke test (rust/shack_sdr/tools/wheel_smoke.py) on what it can
# run; check-encumbered is run once more over the finished folder, so
# nothing reaches a lock that was not checked as it stands. It ends by
# printing the folder and its SHA256SUMS, for vendor-shack-sdr.sh <folder>.
#
# OHS_DIST (default ~/build/ohs-dist) and SHACK_SDR_BUILDER (default
# tools/build-sdr-wheels.sh) are for the tests.
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
dist=${OHS_DIST:-$HOME/build/ohs-dist}
builder=${SHACK_SDR_BUILDER:-$here/tools/build-sdr-wheels.sh}

(($# >= 1)) || { echo "usage: $0 <topic> [linux-amd64|linux-arm64|windows-amd64|macos]..." >&2; exit 2; }
topic=$1; shift
# Lower case, and not ending in a digit: "adsb" gives adsb1, adsb2; "m2"
# would give m21, which reads as m2's 1 or m's 21.
[[ $topic =~ ^[a-z][a-z0-9-]*[a-z-]$|^[a-z]$ ]] \
    || { echo "build-wheels-safe: topic must be lower-case letters, digits and -, not ending in a digit" >&2; exit 2; }
for t in "$@"; do
    case $t in linux-amd64|linux-arm64|windows-amd64|macos) ;; *) echo "build-wheels-safe: unknown target $t" >&2; exit 2 ;; esac
done

if [[ -n $(git -C "$here" status --porcelain) ]]; then
    echo "build-wheels-safe: $here has uncommitted changes; commit first, so the wheels are one commit's" >&2
    exit 1
fi
commit=$(git -C "$here" rev-parse HEAD)

# Every shack-sdr-* name any lock mentions, on disk or committed.
repos=${OHS_NB_REPOS:-$(ls -d "$HOME"/openhamshack-nb "$HOME"/openhamshack-nb-* 2>/dev/null || true)}
taken=$(
    for r in $repos; do
        [ -d "$r" ] || continue
        git -C "$r" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | while read -r wt; do
            cat "$wt/tools/vendor/shack-sdr.lock" 2>/dev/null || true
        done
        git -C "$r" for-each-ref --format='%(refname)' refs/heads 2>/dev/null | while read -r ref; do
            git -C "$r" show "$ref:tools/vendor/shack-sdr.lock" 2>/dev/null || true
        done
    done | grep -o 'shack-sdr-[A-Za-z0-9_-]*[A-Za-z0-9]' | sort -u
)

mkdir -p "$dist"
n=1
while :; do
    name=shack-sdr-$topic$n
    if ! grep -qx "$name" <<<"$taken" && mkdir "$dist/$name" 2>/dev/null; then
        break
    fi
    n=$((n + 1))
    ((n < 1000)) || { echo "build-wheels-safe: no free name for $topic" >&2; exit 1; }
done
out=$dist/$name
info() { printf '%s\n' "commit=$commit" "built=$(date -Is)" "host=$(hostname)" "status=$1" > "$out/BUILD-INFO"; }
info building
echo "build-wheels-safe: building ohs-libs ${commit:0:7} into $out"

if ! "$builder" --out "$out" "$@"; then
    info failed
    echo "build-wheels-safe: the build failed; $out keeps its name (never reused) - see above" >&2
    exit 1
fi
if ! python3 "$here/tools/check-encumbered.py" "$out"/shack_sdr-*.whl; then
    info failed
    echo "build-wheels-safe: check-encumbered failed on $out" >&2
    exit 1
fi
(cd "$out" && sha256sum shack_sdr-*.whl > SHA256SUMS)
info built
echo
echo "folder: $out"
echo "commit: $commit"
cat "$out/SHA256SUMS"
echo "next, in openhamshack-nb: tools/vendor-shack-sdr.sh $out"
