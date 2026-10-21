#!/bin/sh
# Build every Python package's wheel into $1 (default ~/build/ohs-libs-wheels).
# Wheels are built straight from each package directory (--wheel, no
# sdist): shack-bandplan copies ../../data/bandplan in at build time, which
# an sdist would have cut off.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${SHACK_PY:-$HOME/build/venvs/ohs-libs/bin/python}
OUT=${1:-$HOME/build/ohs-libs-wheels}
mkdir -p "$OUT"
for pkg in "$ROOT"/python/*/; do
    [ -f "$pkg/pyproject.toml" ] || continue
    "$PY" -m build --wheel --outdir "$OUT" "$pkg" >/dev/null
    rm -rf "$pkg/build" "$pkg"/src/*.egg-info.build 2>/dev/null || true
done
ls -1 "$OUT"
