# Windows installer

    # on Linux: stage, then copy the stage and packaging/ to the Windows machine
    packaging/tools/shackpkg.py stage packaging/example/shack-app.toml <stage>
    # on Windows (PowerShell 5.1, elevated not needed):
    powershell -ExecutionPolicy Bypass -File packaging\windows\build-windows.ps1 -Stage <stage> [-Work C:\shack-build]

Makes `OpenHamShack-<Name>-<version>-windows-x64.exe` (Inno Setup 6.7.3,
per-user by default, Program Files with `/ALLUSERS`) from the app frozen by
the kit's shared PyInstaller spec on python.org's NuGet Python 3.13.16.
Unsigned for the beta; SmartScreen warns until the installers are signed.
An app's `[[windows.bundle]]` programs go into the frozen folder by
`bundle.ps1` (7-Zip 23.01's MSI is fetched, pinned, for the .7z
archives). It runs on Linux too, under PowerShell 7 with `-SevenZip
7zz`, which is how `packaging/tests` proves it (set `SHACK_PWSH` and
`SHACK_7Z` when they are not on PATH; the tests skip without them).

The family's Windows machine is the desktop at `windows-pc`, reached
through shack only (ohs-dev's key is not authorised there):

    ssh shack "ssh 360admin@windows-pc powershell -NoProfile -EncodedCommand <base64 UTF-16LE>"

Its default shell is cmd, so send PowerShell as `-EncodedCommand`, and copy
files in two hops (`scp` to shack, then from shack to
`360admin@windows-pc:C:/...`). Builds go in `C:\shack-build`.

`try-install.ps1 -Setup <exe> -Exe <executable> -Title <window title>`
proves an installer on that desktop: silent install, the app started in
the console user's session (a scheduled task: an SSH session's windows are
invisible), the app's own window photographed with PrintWindow, its
listening sockets checked to be loopback only, the window closed and every
process checked gone, then a silent uninstall. Someone is usually working
at that desktop: the app's window appears for about fifteen seconds.
`-Under <folder>` and `-SetupArgs <switches>` let it prove the Qt apps'
own installers too (NB, Radio, ATV, DATV): they install under
`Program Files\OpenHamShack <name>`, and their PlutoSDR driver task must be
deselected (`/MERGETASKS="!plutodrivers"`) or a silent install puts ADI's
drivers on the machine. A title that matches no window exactly may match
the end of one ("Welcome to <name>", NB's and Radio's first run), and the
close is repeated while the app is still running, since closing that
dialog brings up the main window.

Notes from the first build (09/10/2026):

- The NuGet Python has no Tcl/Tk, so a Windows bundle has no Tk; the shell
  falls back to a message box (Retry opens the browser again, Cancel
  quits) when WebView2 is missing.
- WebView2 is on every updated Windows 10/11; without it the shell uses
  the browser, never pywebview's Internet Explorer mode.
- PowerShell 5.1 reports a native program's stderr as NativeCommandError
  lines (PyInstaller's INFO log): noise, not failure; the exit code is the
  signal.
