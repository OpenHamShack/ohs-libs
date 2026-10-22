#!/bin/sh
# Build shack_sdr's Python extension with this host's cargo and lay out an
# importable copy of the package in $1 (shack_sdr/ with _native in it), for
# tools/test.sh and for working on the Python half without maturin. The
# wheels are tools/build-sdr-wheels.sh's: this is not how anything ships.
#
#   rust/shack_sdr/tools/dev-build.sh <dir>     then PYTHONPATH=<dir>
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
out=${1:?usage: dev-build.sh <dir>}
CARGO=${CARGO:-$(command -v cargo || echo "$HOME/.cargo/bin/cargo")}
# Not linked against libpython: an extension module takes its symbols from
# the interpreter that loads it, as a wheel's does.
PYO3_BUILD_EXTENSION_MODULE=1 "$CARGO" build --quiet --release --locked --features python \
    --manifest-path "$here/Cargo.toml"
target=$(cd "$here/.." && pwd)/target/release
mkdir -p "$out"
rm -rf "$out/shack_sdr"
cp -r "$here/python/shack_sdr" "$out/shack_sdr"
case "$(uname -s)" in
    Darwin) cp "$target/libshack_sdr.dylib" "$out/shack_sdr/_native.abi3.so" ;;
    *) cp "$target/libshack_sdr.so" "$out/shack_sdr/_native.abi3.so" ;;
esac
