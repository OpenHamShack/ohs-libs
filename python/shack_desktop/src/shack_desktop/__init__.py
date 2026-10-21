"""shack_desktop: the desktop form of OpenHamShack's web-UI apps.

QO-100 WB, Icom, Flex and Digital Modes are web apps; on a desktop they run
their own server on loopback, guarded by a per-launch token, inside a
native window (BETA-PLAN arch change 3). docs/DESKTOP.md is the guide for
an app adopting it, and says why this keeps "no servers inside the desktop
apps".

    import shack_desktop
    shack_desktop.run(make_wsgi_app, title="OpenHamShack Icom", kind="icom")

or, for a server that is its own process (aiohttp, an existing daemon):

    shack_desktop.run(command=shack_desktop.self_command("myapp", "--server"),
                      title="OpenHamShack Icom", kind="icom")
"""

from __future__ import annotations

from .child import LoopbackHTTPServer, ServerEnv, is_server_child, self_command, server_env, watch_parent
from .guard import Guard
from .paths import base_dir, data_dir
from .shell import ShellError, check_private, choose_engine, run

__version__ = "0.1.0"

__all__ = ["Guard", "LoopbackHTTPServer", "ServerEnv", "ShellError", "base_dir", "check_private", "choose_engine", "data_dir",
           "is_server_child", "run", "self_command", "server_env", "watch_parent"]
