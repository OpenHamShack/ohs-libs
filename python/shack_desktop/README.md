# shack-desktop

The desktop form of OpenHamShack's web-UI apps (QO-100 WB, Icom, Flex,
Digital Modes): the app's own server on 127.0.0.1, guarded by a per-launch
token, shown in a native window (pywebview on the system's web engine) or
the default browser when there is none. Design and adoption guide:
[docs/DESKTOP.md](docs/DESKTOP.md); packaging: `../../packaging/`.

- `shell.py`: `run()`: server, privacy check, window or browser, shutdown
- `guard.py`: the token check and cookie exchange, with WSGI, aiohttp and
  http.server adapters
- `child.py`: the server-process half (`server_env()`, `watch_parent()`,
  `self_command()`)
- `instance.py`: one copy per app; a second launch raises the first
- `paths.py`: the per-user data folder per OS

Core is stdlib only; `pip install shack-desktop[window]` adds pywebview.
Licence GPL-3.0-or-later.
