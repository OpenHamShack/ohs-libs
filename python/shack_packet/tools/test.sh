#!/bin/sh
# Run shack-packet's tests in their own virtualenv (default ~/build/venvs/shack-packet).
# About a minute: the receive tests decode minutes of audio, and the
# direwolf interop tests (skipped without it) run gen_packets and atest.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
venv=${SHACK_PACKET_VENV:-$HOME/build/venvs/shack-packet}
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" -q install pytest
fi
# shack-rig from this checkout: the radio links' tests drive its fakes.
"$venv/bin/pip" -q install -e "$here/../shack_rig" -e "$here[test]"
# The direwolf tests write wav files: under ~/build, never /tmp (a small
# tmpfs every session on the host shares).
base=$HOME/build/pytest-shack-packet-$$
trap 'rm -rf "$base"' EXIT
"$venv/bin/python" -m pytest -q -p no:cacheprovider --basetemp="$base" "$here/tests" "$@"
