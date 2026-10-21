#!/bin/sh
# Run shack-rec's tests in their own virtualenv (default ~/build/venvs/shack-rec).
# A few seconds.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
venv=${SHACK_REC_VENV:-$HOME/build/venvs/shack-rec}
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" -q install pytest
fi
"$venv/bin/pip" -q install -e "$here[test]"
exec "$venv/bin/python" -m pytest -q -p no:cacheprovider "$here/tests" "$@"
