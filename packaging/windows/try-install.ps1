<#
.SYNOPSIS
Prove an installer on a real Windows desktop: install silently, start the
app in the logged-on user's session, photograph its window, close it,
uninstall.

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File try-install.ps1 -Setup <setup.exe> -Exe <executable> -Title <window title> [-Shot <png>] [-Seconds 12] [-Under <folder>] [-SetupArgs <args>]

-Under and -SetupArgs are for the apps whose installers are their own
rather than the kit's (QO-100 NB, Radio, ATV, DATV: tools/build-windows.ps1
in each repo). Those install under Program Files\OpenHamShack <name> (the
folder kept its pre-rename name on purpose), and offer ADI's PlutoSDR USB
drivers as a task that is ticked by default: a silent install would put
those drivers on the machine, so the caller deselects that task with
-SetupArgs '/MERGETASKS="!plutodrivers"'.

Run elevated, typically over SSH. An SSH session is not the desktop: a
window started from it lives in a session nobody sees, and a screenshot
from it is black. So the app, the photograph and the close are each a
one-off scheduled task running as whoever is logged on at the console
(interactive logon type: no password needed, and nothing runs if nobody is
there). The photograph is the app's window drawing itself (PrintWindow),
never a copy of the screen: the desktop is someone's own, and the first
version of this script, which copied the screen at the window's position,
photographed another program's window that was on top instead.

What it reports: the installed folder, every listening socket the app's
processes own (all must be 127.0.0.1), whether closing the window ended
every process (the shell stops its server), and that the uninstaller
removed the folder. It leaves nothing behind but the PNG.
#>
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [Parameter(Mandatory = $true)][string]$Exe,
    # The window's title (the app's name): the window is found by it among
    # the app's own processes' windows.
    [Parameter(Mandatory = $true)][string]$Title,
    [string]$Shot = "C:\Users\Public\shack-try-shot.png",
    [int]$Seconds = 12,
    # Where the installed $Exe.exe is looked for (two levels deep).
    [string]$Under = "$env:ProgramFiles\OpenHamShack",
    # Added to the installer's silent switches.
    [string]$SetupArgs = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Step([string]$what) { Write-Host ""; Write-Host "== $what" }

$user = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $user) { throw "nobody is logged on at the console: there is no desktop to show the app on" }
$prefix = "shack-try-$PID"

function AsUser([string]$name, [string]$exe, [string]$argline) {
    # A one-off task in the console user's session; started now, removed by Cleanup.
    if ($argline) { $action = New-ScheduledTaskAction -Execute $exe -Argument $argline }
    else { $action = New-ScheduledTaskAction -Execute $exe }
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "$prefix-$name" -Action $action -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName "$prefix-$name"
}
$dir = $null
function Cleanup {
    # After a failure part-way, the machine is still left as it was found.
    if ($dir -and (Test-Path $dir)) {
        Get-Process -Name $Exe -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Process (Join-Path $dir "unins000.exe") -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait | Out-Null
        Write-Host "   (uninstalled after the failure)"
    }
    Get-ScheduledTask -TaskName "$prefix-*" -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
    Remove-Item "C:\Users\Public\$prefix-*.ps1" -ErrorAction SilentlyContinue
}

try {
    Step "Install (silent, all users)"
    $p = Start-Process $Setup -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ALLUSERS $SetupArgs" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "setup exited $($p.ExitCode)" }
    $installed = Get-ChildItem $Under -Recurse -Filter "$Exe.exe" -Depth 1 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $installed) { throw "no $Exe.exe under $Under" }
    $script:dir = $installed.DirectoryName
    Write-Host "   $dir"

    Step "Start it as $user"
    AsUser "launch" $installed.FullName ""
    Start-Sleep -Seconds $Seconds
    $procs = @(Get-Process -Name $Exe -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { throw "$Exe is not running $Seconds s after the start" }
    Write-Host "   processes: $(($procs | ForEach-Object { $_.Id }) -join ', ')"
    $listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $procs.Id -contains $_.OwningProcess })
    foreach ($l in $listen) { Write-Host "   listening $($l.LocalAddress):$($l.LocalPort) (pid $($l.OwningProcess))" }
    $wide = @($listen | Where-Object { $_.LocalAddress -ne "127.0.0.1" })
    if ($wide.Count -gt 0) { throw "the app listens beyond loopback" }

    Step "Photograph the window"
    $shotPs1 = "C:\Users\Public\$prefix-shot.ps1"
    @"
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices;
public static class W {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  public struct RECT { public int L, T, R, B; }
  // The window titled exactly so, or else one whose title ends with it:
  // a Qt app's first run puts "Welcome to <name>" in front of its main
  // window, and that dialog is what the person at the screen sees.
  public static IntPtr Find(uint[] pids, string title) {
    IntPtr found = IntPtr.Zero, near = IntPtr.Zero;
    EnumWindows((h, l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (Array.IndexOf(pids, pid) < 0 || !IsWindowVisible(h)) return true;
      var sb = new StringBuilder(256); GetWindowText(h, sb, 256);
      var t = sb.ToString();
      if (t == title) { found = h; return false; }
      if (near == IntPtr.Zero && t.EndsWith(" " + title)) near = h;
      return true;
    }, IntPtr.Zero);
    return found != IntPtr.Zero ? found : near;
  }
}
'@
[W]::SetProcessDPIAware() | Out-Null
`$pids = [uint32[]]@(Get-Process -Name '$Exe' | ForEach-Object { `$_.Id })
`$h = [W]::Find(`$pids, '$Title')
if (`$h -eq [IntPtr]::Zero) { exit 2 }
[W]::ShowWindow(`$h, 9) | Out-Null
Start-Sleep -Milliseconds 500
`$r = New-Object W+RECT; [W]::GetWindowRect(`$h, [ref]`$r) | Out-Null
`$bmp = New-Object Drawing.Bitmap (`$r.R - `$r.L), (`$r.B - `$r.T)
`$g = [Drawing.Graphics]::FromImage(`$bmp)
`$hdc = `$g.GetHdc()
# PW_RENDERFULLCONTENT (2): the window draws itself, WebView2 content
# included, whatever is on top of it. Nothing else on the screen can end
# up in the picture.
`$ok = [W]::PrintWindow(`$h, `$hdc, 2)
`$g.ReleaseHdc(`$hdc)
if (-not `$ok) { exit 3 }
`$bmp.Save('$Shot', [Drawing.Imaging.ImageFormat]::Png)
"@ | Set-Content -Path $shotPs1 -Encoding ASCII
    Remove-Item $Shot -ErrorAction SilentlyContinue
    AsUser "shot" "powershell.exe" "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$shotPs1`""
    for ($i = 0; $i -lt 30 -and -not (Test-Path $Shot); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Path $Shot)) { throw "no photograph was written (no window found?)" }
    Write-Host "   $Shot ($((Get-Item $Shot).Length) bytes)"

    Step "Close the window"
    # Asked again every five seconds while the app is still there: closing
    # a first-run dialog (NB's and Radio's Welcome) brings the main window
    # up behind it, and that one needs closing too.
    $gone = $false
    for ($i = 0; $i -lt 40 -and -not $gone; $i++) {
        if ($i % 10 -eq 0) {
            AsUser "close$i" "powershell.exe" "-NoProfile -WindowStyle Hidden -Command `"Get-Process -Name '$Exe' | Where-Object { `$_.MainWindowHandle -ne 0 } | ForEach-Object { `$_.CloseMainWindow() }`""
        }
        Start-Sleep -Milliseconds 500
        $gone = @(Get-Process -Id $procs.Id -ErrorAction SilentlyContinue).Count -eq 0
    }
    if (-not $gone) {
        Get-Process -Id $procs.Id -ErrorAction SilentlyContinue | Stop-Process -Force
        throw "closing the window left processes running"
    }
    Write-Host "   every process exited (window and server)"

    Step "Uninstall"
    $p = Start-Process (Join-Path $dir "unins000.exe") -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "uninstall exited $($p.ExitCode)" }
    for ($i = 0; $i -lt 20 -and (Test-Path $dir); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Path $dir) { throw "$dir is still there after uninstalling" }
    Write-Host "   removed $dir"
} finally {
    Cleanup
}
Write-Host ""
Write-Host "try-install: OK"
