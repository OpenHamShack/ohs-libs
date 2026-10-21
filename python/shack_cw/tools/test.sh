#!/bin/sh
# Run shack-cw's tests in their own virtualenv (default ~/build/venvs/shack-cw).
# About half a minute: the on-air cases render and decode minutes of audio.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
venv=${SHACK_CW_VENV:-$HOME/build/venvs/shack-cw}
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" -q install pytest
fi
"$venv/bin/pip" -q install -e "$here[test]"
exec "$venv/bin/python" -m pytest -q -p no:cacheprovider "$here/tests" "$@"
