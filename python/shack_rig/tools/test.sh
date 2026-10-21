#!/bin/sh
# Run shack-rig's tests in their own virtualenv (default ~/build/venvs/shack-rig).
# No radio is needed or touched: the tests run against shack_rig.icom.fake (an
# IC-9700 on the loopback address) and radios made of ptys.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
venv=${SHACK_RIG_VENV:-$HOME/build/venvs/shack-rig}
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" -q install pytest
fi
"$venv/bin/pip" -q install -e "$here[test]"
exec "$venv/bin/python" -m pytest -q "$here/tests" "$@"
