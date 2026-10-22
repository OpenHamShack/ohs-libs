#!/bin/sh
# Everything ohs-libs checks before a release (REPO-LAYOUT.md §3): every
# package's tests, the packaging kit's descriptor tests, every vector set,
# the band-plan validator, the gettext templates, the i18n check on this
# repository itself, one version for every artefact, and the Rust
# workspace (rust/): cargo test, cargo deny, shack_sdr's Python half
# against its freshly built extension, and the check that nothing
# encumbered is linked or carried (tools/check-encumbered.py).
#
# Usage: tools/test.sh            (uses $SHACK_PY, else ~/build/venvs/ohs-libs/bin/python)
#
# The Rust steps use this host's cargo (PATH or ~/.cargo/bin) and cargo-deny
# (PATH, $SHACK_CARGO_DENY or ~/build/cargo-deny/bin), the same pinned
# toolchain as rust/build.Containerfile; without them they fail rather than
# pass unchecked. SHACK_SKIP_RUST=1 skips them, loudly, for a host with no
# Rust at all.
#
# Each package runs in its own pytest process: they share test file names
# (test_vectors.py), and a package must pass on its own, as it is released.
# Temporary files go under ~/build, never /tmp (a small tmpfs every session
# on the host shares; the sync vectors write 10,000-record databases).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${SHACK_PY:-$HOME/build/venvs/ohs-libs/bin/python}
BASETEMP=${SHACK_TEST_TMP:-$HOME/build/pytest-ohs-libs-$$}
export PYTHONDONTWRITEBYTECODE=1
trap 'rm -rf "$BASETEMP"' EXIT
cd "$ROOT"
mkdir -p "$BASETEMP"

fail=0
step() { printf '\n== %s\n' "$1"; }

step "one version everywhere"
"$PY" - <<'PY' || fail=1
import pathlib, re, sys, tomllib
root = pathlib.Path(".")
want = (root / "VERSION").read_text().strip()
bad = []
for pp in sorted(root.glob("python/*/pyproject.toml")):
    got = tomllib.loads(pp.read_text())["project"]["version"]
    if got != want:
        bad.append(f"{pp}: {got}")
for init in sorted(root.glob("python/*/src/*/__init__.py")):
    m = re.search(r'^__version__ = "([^"]+)"', init.read_text(), re.M)
    if m and m.group(1) != want:
        bad.append(f"{init}: {m.group(1)}")
for b in bad:
    print(f"{b} differs from VERSION {want}")
sys.exit(1 if bad else 0)
PY

step "band-plan dataset"
"$PY" -m shack_bandplan.validate data/bandplan || fail=1

step "gettext templates"
"$PY" tools/extract_pot.py --check || fail=1

step "i18n check on ohs-libs"
"$PY" -m shack_i18n.check || fail=1

for ver in rust/*/Cargo.toml; do
    got=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ver" | head -1)
    if [ "$got" != "$(cat VERSION)" ]; then
        echo "$ver: $got differs from VERSION $(cat VERSION)"
        fail=1
    fi
done

if [ "${SHACK_SKIP_RUST:-}" = 1 ]; then
    step "rust/: SKIPPED (SHACK_SKIP_RUST=1) - shack_sdr was not tested"
else
    CARGO=$(command -v cargo || echo "$HOME/.cargo/bin/cargo")
    DENY=${SHACK_CARGO_DENY:-$(command -v cargo-deny || echo "$HOME/build/cargo-deny/bin/cargo-deny")}
    export PATH="$(dirname "$CARGO"):$PATH"
    step "rust: cargo test"
    if [ -x "$CARGO" ]; then
        (cd rust && "$CARGO" test --quiet --release --locked) || fail=1
    else
        echo "no cargo (see the comment at the top)"; fail=1
    fi
    step "rust: cargo deny check bans licenses"
    if [ -x "$DENY" ]; then
        (cd rust && "$DENY" --log-level error check bans licenses) || fail=1
    else
        echo "no cargo-deny (see the comment at the top)"; fail=1
    fi
    step "tests: rust/shack_sdr (Python half)"
    if CARGO="$CARGO" rust/shack_sdr/tools/dev-build.sh "$BASETEMP/sdr-py"; then
        PYTHONPATH="$BASETEMP/sdr-py" "$PY" -m pytest -q -p no:cacheprovider --basetemp="$BASETEMP/shack_sdr" \
            rust/shack_sdr/pytests || fail=1
        step "nothing encumbered in shack_sdr's build"
        "$PY" tools/check-encumbered.py "$BASETEMP/sdr-py" || fail=1
    else
        fail=1
    fi
fi

step "tests: tools/"
SHACK_SDR_BUILT="$BASETEMP/sdr-py" "$PY" -m pytest -q -p no:cacheprovider --basetemp="$BASETEMP/tools" tools/tests \
    || fail=1

for pkg in python/*/; do
    [ -d "$pkg/tests" ] || continue
    step "tests: $pkg"
    "$PY" -m pytest -q -p no:cacheprovider --basetemp="$BASETEMP/$(basename "$pkg")" "$pkg/tests" || fail=1
done

step "tests: packaging/"
"$PY" -m pytest -q -p no:cacheprovider --basetemp="$BASETEMP/packaging" packaging/tests || fail=1

if [ "$fail" -ne 0 ]; then
    printf '\nFAILED\n'
    exit 1
fi
printf '\nall green\n'
