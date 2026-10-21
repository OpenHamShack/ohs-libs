"""An app's server as its own process, as child.py documents it, for the
subprocess tests. --no-guard and --bind-all are the two mistakes the shell
must refuse to show; --die-after N exits by itself after N seconds."""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import shack_desktop

env = shack_desktop.server_env()
guard = env.guard
no_guard = "--no-guard" in sys.argv
with open(os.environ["TEST_PID_FILE"], "w") as f:
    f.write(str(os.getpid()))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if not no_guard and not guard.check_handler(self):
            return
        body = b"hello from the child"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


host = "0.0.0.0" if "--bind-all" in sys.argv else env.host
httpd = (ThreadingHTTPServer if host == "0.0.0.0" else shack_desktop.LoopbackHTTPServer)((host, env.port), Handler)
shack_desktop.watch_parent(on_gone=lambda: os._exit(0))
if "--die-after" in sys.argv:
    secs = float(sys.argv[sys.argv.index("--die-after") + 1])
    threading.Thread(target=lambda: (time.sleep(secs), os._exit(7)), daemon=True).start()
httpd.serve_forever()
