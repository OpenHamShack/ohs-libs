#!/usr/bin/env bash
# Build the shack-sdr wheels (rust/shack_sdr: the Rust extension and its
# Python half in one wheel, RADIO-SDR-PLAN §2.3), check each, and write
# SHA256SUMS.
#
#   tools/build-sdr-wheels.sh [--no-test] [--out DIR] [TARGET...]
#
# TARGET is linux-amd64, linux-arm64, windows-amd64 or macos (all four when
# none is named). Wheels land in ~/build/ohs-dist/shack-sdr/ (or --out),
# never in the repository; each consuming app pins them by sha256 in its
# own tools/vendor/shack-sdr.lock. One wheel per platform: the module is
# abi3-py311, so it serves CPython 3.11 and every later one.
#
# The pattern is openhamshack-nb's tools/build-wheels.sh, moved here:
# - Linux and Windows build in rust/build.Containerfile's bookworm image
#   (toolchain pinned by digest, maturin by hash). Linux wheels are tagged
#   manylinux_2_35, the newest glibc maturin finds the module needs;
#   Windows is cross-built with mingw.
# - macOS builds natively on the Mac mini (`ssh mac`) as one universal2
#   wheel, under /Volumes/OpenHamShackBuild/shack-sdr-wheel with a pinned
#   rustup, Python and maturin of its own; the working copy is sent as a
#   tarball and the wheel copied back. SHACK_MAC names the Mac where `mac`
#   is not an ssh alias (shack: user@mac-mini).
#
# Every wheel is then run through tools/check-encumbered.py (nothing
# encumbered linked, defined or carried: §5.5) - a finding fails the build -
# and, where it can run, installed into a fresh venv and smoke-tested
# (rust/shack_sdr/tools/wheel_smoke.py: import, RTTY decoded, every
# encumbered codec missing): linux-amd64 in the container's Python 3.11 (the
# oldest the wheel promises) and this host's; macos in the Mac's Python,
# arm64 and x86_64 under Rosetta 2. linux-arm64 and windows-amd64 are built
# and checked but not run here.
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
rust=$here/rust
out=$HOME/build/ohs-dist/shack-sdr
test=1
mac_local=0
targets=()
while (($#)); do
    case $1 in
        --no-test) test=0 ;;
        --out) out=$2; shift ;;
        --mac-local) mac_local=1 ;;   # internal: the half that runs on the Mac
        linux-amd64|linux-arm64|windows-amd64|macos) targets+=("$1") ;;
        *) echo "usage: $0 [--no-test] [--out DIR] [linux-amd64|linux-arm64|windows-amd64|macos]..." >&2; exit 2 ;;
    esac
    shift
done
((${#targets[@]})) || targets=(linux-amd64 linux-arm64 windows-amd64 macos)

# Zip entries carry this time, so the same commit makes the same wheel bytes.
EPOCH=315532800
MATURIN_VERSION=1.15.0           # as rust/shack_sdr/wheel-requirements.txt

# ------------------------------------------------------------ on the Mac
if ((mac_local)); then
    MACWORK=$(cd "$here/.." && pwd)
    TOOLS=$MACWORK/tools CACHE=$MACWORK/cache
    mkdir -p "$TOOLS" "$CACHE"
    RUST_TOOLCHAIN=1.98.1        # as rust/build.Containerfile
    # The same pins as openhamshack-nb's tools/build-macos.sh.
    RUSTUP=(rustup-init-1.29.1 ec1b9233e7f72990ecd8e62063fa7f6c3dfc2bec8e97f88bff165f9100ac696a
            https://static.rust-lang.org/rustup/archive/1.29.1/aarch64-apple-darwin/rustup-init)
    PYSA=(cpython-3.13.16+20261003-aarch64-apple-darwin-install_only.tar.gz
          d8975d7df4f08f7b1c7aafcdfacbddcec3d366415f2c1a72b2466b6850815933
          https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.13.16%2B20261003-aarch64-apple-darwin-install_only.tar.gz)
    PYSA_X86=(cpython-3.13.16+20261003-x86_64-apple-darwin-install_only.tar.gz
              8e9cb087305bfb8969f68a905f79f41469d4aa5220c1aa71ada7fc9953bdba0f
              https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.13.16%2B20261003-x86_64-apple-darwin-install_only.tar.gz)
    fetch() {
        local dest=$CACHE/$1
        if [[ ! -f $dest ]] || [[ $(shasum -a 256 "$dest" | cut -d' ' -f1) != "$2" ]]; then
            curl -fsSL -o "$dest.part" "$3"
            [[ $(shasum -a 256 "$dest.part" | cut -d' ' -f1) == "$2" ]] || { echo "bad sha256: $3" >&2; exit 1; }
            mv "$dest.part" "$dest"
        fi
        echo "$dest"
    }
    export RUSTUP_HOME=$TOOLS/rustup CARGO_HOME=$TOOLS/cargo
    if [[ ! -x $CARGO_HOME/bin/cargo ]]; then
        r=$(fetch "${RUSTUP[@]}"); chmod +x "$r"
        "$r" -y -q --no-modify-path --profile minimal --default-toolchain "$RUST_TOOLCHAIN"
    fi
    "$CARGO_HOME/bin/rustup" target add --toolchain "$RUST_TOOLCHAIN" aarch64-apple-darwin x86_64-apple-darwin >/dev/null
    PYHOME=$TOOLS/python-3.13.16
    [[ -x $PYHOME/bin/python3.13 ]] || { mkdir -p "$PYHOME" && tar -xzf "$(fetch "${PYSA[@]}")" -C "$PYHOME" --strip-components 1; }
    if [[ ! -x $TOOLS/maturin-$MATURIN_VERSION/bin/maturin ]]; then
        "$PYHOME/bin/python3.13" -m venv "$TOOLS/maturin-$MATURIN_VERSION"
        "$TOOLS/maturin-$MATURIN_VERSION/bin/pip" install -q --disable-pip-version-check --require-hashes \
            --only-binary=:all: -r "$rust/shack_sdr/wheel-requirements.txt"
    fi
    rm -rf "$MACWORK/wheels"; mkdir -p "$MACWORK/wheels"
    (cd "$rust/shack_sdr" && env PATH="$CARGO_HOME/bin:$PATH" RUSTUP_TOOLCHAIN=$RUST_TOOLCHAIN \
        MACOSX_DEPLOYMENT_TARGET=11.0 PYO3_NO_PYTHON=1 PYO3_BUILD_EXTENSION_MODULE=1 \
        CARGO_TARGET_DIR="$MACWORK/target" SOURCE_DATE_EPOCH=$EPOCH \
        RUSTFLAGS="--remap-path-prefix=$rust=/src --remap-path-prefix=$CARGO_HOME/registry=/cargo" \
        "$TOOLS/maturin-$MATURIN_VERSION/bin/maturin" build --release --target universal2-apple-darwin \
        --out "$MACWORK/wheels")
    "$PYHOME/bin/python3.13" "$here/tools/check-encumbered.py" "$MACWORK"/wheels/*.whl
    if ((test)); then
        echo "== macos wheel smoke test, arm64"
        "$PYHOME/bin/python3.13" "$rust/shack_sdr/tools/wheel_smoke.py" "$MACWORK"/wheels/*.whl "$MACWORK/venv-arm64"
        if arch -x86_64 /usr/bin/true 2>/dev/null; then
            PYX=$TOOLS/python-3.13.16-x86_64
            [[ -x $PYX/bin/python3.13 ]] || { mkdir -p "$PYX" && tar -xzf "$(fetch "${PYSA_X86[@]}")" -C "$PYX" --strip-components 1; }
            echo "== macos wheel smoke test, x86_64 (Rosetta 2)"
            arch -x86_64 "$PYX/bin/python3.13" "$rust/shack_sdr/tools/wheel_smoke.py" "$MACWORK"/wheels/*.whl "$MACWORK/venv-x86"
        fi
    fi
    exit 0
fi

# ------------------------------------------------------------ Linux side
IMAGE=localhost/shack-sdr-build:1.98.1-1
if [[ " ${targets[*]} " != " macos " ]] && ! podman image exists "$IMAGE"; then
    echo "== building $IMAGE (once)"
    podman build -t "$IMAGE" -f "$rust/build.Containerfile" "$rust"
fi
mkdir -p "$out" "$HOME/build/shack-sdr-target"

container() {
    podman run --rm -v "$here:/repo:ro" -v "$HOME/build/shack-sdr-target:/target" -v "$out:/out" \
        -v shack-sdr-cargo-registry:/usr/local/cargo/registry -w /repo/rust/shack_sdr \
        -e PYO3_NO_PYTHON=1 -e PYO3_BUILD_EXTENSION_MODULE=1 -e CARGO_TARGET_DIR=/target \
        -e RUSTFLAGS="--remap-path-prefix=/usr/local/cargo/registry=/cargo --remap-path-prefix=/repo=/src" \
        -e SOURCE_DATE_EPOCH=$EPOCH "$IMAGE" "$@"
}

build_in_container() {
    local t=$1 args=()
    case $t in
        linux-amd64) args=(--target x86_64-unknown-linux-gnu --compatibility manylinux_2_35); rm -f "$out"/*_x86_64.whl ;;
        linux-arm64) args=(--target aarch64-unknown-linux-gnu --compatibility manylinux_2_35); rm -f "$out"/*_aarch64.whl ;;
        windows-amd64) args=(--target x86_64-pc-windows-gnu); rm -f "$out"/*-win_amd64.whl ;;
    esac
    echo "== $t wheel in $IMAGE"
    container /opt/maturin/bin/maturin build --release "${args[@]}" --out /out
}

build_on_mac() {
    local w=/Volumes/OpenHamShackBuild/shack-sdr-wheel mac=${SHACK_MAC:-mac}
    echo "== macos wheel on the Mac mini"
    ssh "$mac" "test -d /Volumes/OpenHamShackBuild" || { echo "/Volumes/OpenHamShackBuild is not mounted on the Mac" >&2; exit 1; }
    (cd "$here" && git ls-files -z -co --exclude-standard -- LICENSE spec rust tools \
        | xargs -0 sh -c 'for f; do [ -e "$f" ] || [ -L "$f" ] && printf "%s\0" "$f"; done' sh \
        | tar --null -T - -czf - ) \
        | ssh "$mac" "rm -rf $w/src && mkdir -p $w/src && tar -xzf - -C $w/src && bash $w/src/tools/build-sdr-wheels.sh --mac-local $( ((test)) || echo --no-test)"
    rm -f "$out"/*-macosx_*.whl
    scp -q "$mac:$w/wheels/*.whl" "$out/"
}

for t in "${targets[@]}"; do
    case $t in
        macos) build_on_mac ;;
        *) build_in_container "$t" ;;
    esac
done

echo "== nothing encumbered in any wheel"
python3 "$here/tools/check-encumbered.py" "$out"/shack_sdr-*.whl

if ((test)) && [[ " ${targets[*]} " == *" linux-amd64 "* ]]; then
    whl=$(ls "$out"/shack_sdr-*_x86_64.whl)
    echo "== linux-amd64 wheel smoke test, Python 3.11 (the build container)"
    container sh -c "python3 /repo/rust/shack_sdr/tools/wheel_smoke.py /out/$(basename "$whl") /tmp/venv"
    echo "== linux-amd64 wheel smoke test, Python $(python3 -c 'import sys; print(sys.version.split()[0])') (this host)"
    rm -rf "$HOME/build/shack-sdr-wheel-venv"
    python3 "$here/rust/shack_sdr/tools/wheel_smoke.py" "$whl" "$HOME/build/shack-sdr-wheel-venv"
    rm -rf "$HOME/build/shack-sdr-wheel-venv"
fi

(cd "$out" && sha256sum shack_sdr-*.whl > SHA256SUMS && cat SHA256SUMS)
