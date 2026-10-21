"""What a desktop app's entry point looks like, in the two forms, for the
subprocess tests.

    launch.py inproc                 run(app_factory): WSGI in this process
    launch.py command [server args]  run(command=...): tests/server.py as a child
"""
import os
import sys
from pathlib import Path

import shack_desktop

HERE = Path(__file__).resolve().parent


def wsgi_app():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"hello from in-process"]
    return app


def main():
    state = Path(os.environ["TEST_STATE_DIR"])
    if sys.argv[1] == "inproc":
        code = shack_desktop.run(wsgi_app, title="Test", kind="test", state_dir=state)
    else:
        code = shack_desktop.run(command=[sys.executable, str(HERE / "server.py"), *sys.argv[2:]],
                                 title="Test", kind="test", state_dir=state, ready_timeout=10)
    sys.exit(code)


if __name__ == "__main__":
    main()
