#!/bin/sh
# Run shack-pad's tests in their own virtualenv (default ~/build/venvs/shack-pad).
# No hardware is needed: drivers run against shack_pad.fake.FakeHid.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
venv=${SHACK_PAD_VENV:-$HOME/build/venvs/shack-pad}
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" -q install pytest jsonschema
fi
# Pillow and hidapi are optional at runtime; the tests that need them say so.
"$venv/bin/pip" -q install -e "$here[hid,test]"
exec "$venv/bin/python" -m pytest -q "$here/tests" "$@"
