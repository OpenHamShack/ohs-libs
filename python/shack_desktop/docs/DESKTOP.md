# The desktop form of a web-UI app

QO-100 WB, Icom, Flex and Digital Modes are web apps. BETA-PLAN arch change
3 gives each a desktop form: "the same app server, run locally, inside a
native window (a webview shell, or open in the default browser if a webview
isn't viable)". `shack_desktop.run()` is that shell, shared by all four.
Packaging it for Linux, Windows and macOS is `packaging/` in this
repository (`packaging/README.md`).

## Adopting it

Two shapes, whichever the app's server already is.

**A WSGI app, served inside the window's own process** (simplest to bundle):

```python
import shack_desktop

def make_app():                 # returns a WSGI callable
    return my_wsgi_app

shack_desktop.run(make_app, title="OpenHamShack Icom", kind="icom",
                  icon="/usr/share/icons/hicolor/256x256/apps/com.openhamshack.icom.png")
```

The shell serves it from a thread, on loopback, with the guard already
wrapped around it.

**A server that is its own process** (aiohttp, an existing daemon): the
program re-enters itself with a flag, and that branch reads what the shell
gave it.

```python
import shack_desktop

def main():
    if "--server" in sys.argv:
        env = shack_desktop.server_env()      # host 127.0.0.1, port, token
        shack_desktop.watch_parent()          # exit if the window's process dies
        app = web.Application(middlewares=[env.guard.aiohttp_middleware()])
        ...
        web.run_app(app, host=env.host, port=env.port)
        return
    sys.exit(shack_desktop.run(command=shack_desktop.self_command("shack_icom", "--server"),
                               title="OpenHamShack Icom", kind="icom"))
```

For an `http.server` app, serve with `shack_desktop.LoopbackHTTPServer`
rather than `ThreadingHTTPServer`: the stdlib one asks for its own name by
reverse DNS at bind, and on macOS that lookup of 127.0.0.1 goes to mDNS,
which an app opened from Finder may not use without the Local Network
permission (the example hung there).

`self_command` runs the same program again: `python -m <module>` from
source, the bundle's own executable when frozen by PyInstaller. For a WSGI
app use `env.guard.wsgi(app)`; for `http.server`, call
`env.guard.check_handler(self)` first in each `do_*` and return when it is
False.

`kind` names the app's folder (`shack_desktop.data_dir(kind)`: XDG data /
`%LOCALAPPDATA%\OpenHamShack` / `~/Library/Application Support/OpenHamShack`,
beside the shared station store) and its single-instance lock. Pass the
app's `shack_pad.DesktopPad` as `pad=` (or a function that makes one) and
the pad follows the window's focus whenever shack_pad is installed.

The page must use relative URLs (it does already: GUIDE §2, "URLs: never
absolute"), and needs nothing else: the token cookie rides on every request,
fetch() and WebSocket without the page knowing. One thing a page's
Content-Security-Policy must allow: `script-src 'self' 'unsafe-eval'`.
pywebview injects its JS bridge (which carries window focus to the pad)
with evaluate_js, and WebKitGTK refuses it under a stricter policy.

## What the shell guarantees

| | |
|---|---|
| Address | 127.0.0.1 only. The in-process server binds port 0 there; a child is told the port and refused by `server_env()` if told any host but loopback. |
| Token | 256 bits from `secrets.token_urlsafe`, new every launch, in the child's environment (never its command line, which other users can read). |
| Checked before showing | A request without the token must get 403, and the port must not answer on the machine's network address. Either failure: an error, no window. |
| Token leaves the URL | The first load (`?shack_token=`) is answered with a 303 to the same address without it, setting an HttpOnly, SameSite=Strict cookie named for the port. |
| DNS rebinding | Host must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`. |
| Window | pywebview with the system engine (WebKitGTK, WebView2, WKWebView), `private_mode=True`. Never Qt; never IE (pywebview's fallback when WebView2 is missing). |
| Size | The app's `size` when it fits the primary screen's usable area (taskbar, panels, menu bar and Dock left out, in logical pixels whatever the DPI scaling); otherwise the window opens maximised, with a restored size that fits too. Asked of the OS (user32, Gdk, NSScreen), not `webview.screens()`, which loads pywebview's default toolkit and reports whole monitors. |
| Zoom | Ctrl+minus, Ctrl+plus, Ctrl+0 and Ctrl+wheel zoom the page as a browser does, for a layout still too big. WebView2 does this itself (`zoomable=True` stops pywebview blocking the wheel); on WebKitGTK and WKWebView the page reports the keys and the shell sets the engine's zoom. Not kept between launches (`private_mode`). |
| Fallback | The default browser, and a small Tk window (Open again, Quit); on Windows, whose installers' Python has no Tk, a message box (Retry opens it again, Cancel quits); in a terminal without either, Ctrl+C. `SHACK_DESKTOP_UI=browser` forces it. |
| One copy | A second launch writes a request file and exits; the first raises its window (or reopens the browser) within 250 ms. |
| Shutdown | Window closed, Ctrl+C or SIGTERM: the server stops (a child's stdin is closed, then it is terminated, then killed). SIGINT/SIGTERM are taken by a sigwait() thread, because GTK's and Cocoa's main loops sit in C where a Python handler never runs. The child is in its own process group, so a terminal's Ctrl+C reaches the shell and not it. The shell killed outright: the child sees its stdin close and its parent vanish and exits (and on Linux, PR_SET_PDEATHSIG). |
| Network check without asking | On Linux and macOS "not on the network address" is answered by a bind that fails if the server holds every address, so no connection is made (on macOS a connection to a LAN address is itself Local Network access, a permission prompt). Windows lets both binds succeed, so there it is a connection. |

## Why this is not a "server inside a desktop app"

Brian's rule (09/10/2026) is that a desktop app is never a server for
anything else: products reach phones and TVs as apps on the controller, and
desktop apps are clients of it (BETA-PLAN arch change 2). What the rule
protects against is a desktop install quietly becoming something other
devices connect to: a port on the LAN, a second control path to a radio, a
second transmit lease, a surface to secure and support.

The loopback server here is none of those:

- **Nothing off the machine can reach it.** It binds 127.0.0.1, which no
  packet from another host can address; the shell checks the port does not
  answer on the machine's own network address before showing anything.
  There is no advert (no Bonjour, unlike the controller's
  `_openhamshack._tcp`), and the port is random per launch.
- **Nothing else on the machine can use it.** Every request needs a token
  that exists only in this launch's memory, the window's cookie jar and the
  child's environment (readable by the same user, who could equally drive
  the window). A web page in the browser that guesses the port gets 403; a
  rebinding page is refused by Host; a cross-site form cannot send the
  SameSite=Strict cookie.
- **It lives exactly as long as its window.** It is the window's renderer,
  started for it and stopped with it. It is the same code that the store
  form runs in a container, so there is no second implementation, but in
  the desktop form it only ever has one client.

So the webview-plus-loopback pair is one program split over two threads or
two processes, talking over a private channel; the token is what makes the
channel private. REPO-LAYOUT §4 records the same reading: "The web-UI apps'
desktop form runs their server on loopback for their own window, as
BETA-PLAN arch change 3 allows." The Qt desktop apps open no listener at
all.

## Platform notes

- **Linux:** pywebview's GTK backend needs PyGObject and WebKitGTK from the
  system (Debian: `python3-gi gir1.2-webkit2-4.1`); the .deb depends on
  them. The AppImage carries PyGObject but uses the system's WebKitGTK:
  bundling WebKit is about 100 MB and its helper processes break when
  relocated. Without it, the browser.
- **Windows:** WebView2 is part of Windows 11 and of updated Windows 10;
  LTSC and offline installs may lack it, and then the browser is used (not
  pywebview's Internet Explorer mode).
- **macOS:** WKWebView is always there.
- The page's `focus`/`blur` events, reported through pywebview's JS bridge,
  drive the pad: pywebview has no focus event of its own.
- On a Linux machine with no desktop session (a VM under Xvfb), GTK waits
  25 s for an xdg-desktop-portal that never starts before it shows the
  window. Run headless tests under `dbus-run-session`; real desktops have
  a portal.
- An app opened from Finder that writes to an external volume makes macOS
  ask the person at the screen first, and waits for the answer.

## Diagnostics

A frozen window app has no console. `SHACK_DESKTOP_LOG=<file>` writes the
shell's log there (engine chosen, port, signals). `SHACK_DESKTOP_SNAPSHOT=
<png>` on macOS has WKWebView write its own picture of the page after the
first load: how the kit proves a build on the Mac mini, where
`screencapture` over SSH is refused for want of Screen Recording
permission.

## Tests

`tests/` runs without a display: the window is a stand-in pywebview
(`tests/fakes/webview`) that loads the URL as a webview would, and the
browser a stand-in `$BROWSER`. `test_shell.py` starts real shells as
processes: token required, loopback only, fallback chosen when pywebview
will not import, single instance, shutdown (closed, SIGTERM, SIGKILL of the
shell), and the two server mistakes the shell refuses to show.
