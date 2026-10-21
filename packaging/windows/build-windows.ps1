<#
.SYNOPSIS
Build an OpenHamShack app's Windows installer from its staged folder.

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File packaging\windows\build-windows.ps1 -Stage <stage> [-Work C:\shack-build] [-NoTests]

-Stage is the folder packaging/tools/shackpkg.py made on Linux (app.json,
src\, launcher.py, icons\, LICENSE), copied here: this machine has neither
the brand kit nor a Python of its own to read shack-app.toml with.

Generalised from openhamshack-nb's tools\build-windows.ps1 and the same in kind:
everything it downloads or builds goes under -Work (a private Python from
python.org's NuGet package, unpacked, not installed; a virtualenv; a
download cache; PyInstaller's folders; out\ with the installer). Nothing is
installed machine-wide and PATH is not touched; delete the folder and it
is gone. Inno Setup, if not already installed, is unpacked into the same
folder in its portable mode. Every download is pinned by SHA-256.

Programs an app declares in shack-app.toml's [[windows.bundle]] (ffmpeg,
mpv, ...) go into the frozen folder next: bundle.ps1 fetches each pinned
archive, takes out the named files, their licences and the app's notice,
and runs each program's own check.

The frozen program is checked before the installer is made: its server
mode (the same executable started with the shell's environment, as
shack_desktop starts it) must refuse a request without the launch token
and answer one with it. A bundle missing a module fails here rather than
on someone's PC.

Unsigned: the beta's installers are (BETA-PLAN). -NoSign is accepted and
changes nothing, so the habit from openhamshack-nb's script still works.
Signing, when the beta ends, is openhamshack-nb's packaging\windows\sign.ps1
(Artifact Signing, as 360 Technology) wired in through ISCC's SignTool.

Windows PowerShell 5.1 compatible (no ?? and no -NoNewline): that is what
a fresh Windows has.
#>
param(
    [Parameter(Mandatory = $true)][string]$Stage,
    [string]$Work = "C:\shack-build",
    [switch]$NoTests,
    [switch]$NoSign
)

$ErrorActionPreference = "Stop"
# Invoke-WebRequest in 5.1 is tens of times slower while drawing its progress bar.
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Kit = Split-Path -Parent $PSScriptRoot
$Stage = (Resolve-Path $Stage).Path
$App = Get-Content (Join-Path $Stage "app.json") -Raw | ConvertFrom-Json
$Cache = Join-Path $Work "cache"
$Out = Join-Path $Work "out"
New-Item -ItemType Directory -Force -Path $Work, $Cache, $Out | Out-Null

$Pins = @{
    # Python 3.13, the full interpreter as a zip (tools\ inside): venv and
    # pip work, nothing is registered with Windows. openhamshack-nb's pin.
    python    = @{ url  = "https://api.nuget.org/v3-flatcontainer/python/3.13.16/python.3.13.16.nupkg"
                   file = "python.3.13.16.zip"
                   sha  = "95fad176338f1d6a799e45946488063f4aa61764e6cff2f132ddeffbf7a04c34" }
    innosetup = @{ url  = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
                   file = "innosetup-6.7.3.exe"
                   sha  = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732" }
}

function Step([string]$what) { Write-Host ""; Write-Host "== $what" }

function Fetch([string]$name) {
    $p = $Pins[$name]
    $dest = Join-Path $Cache $p.file
    if (Test-Path $dest) {
        if ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $p.sha.ToUpper()) { return $dest }
        Remove-Item $dest
    }
    Write-Host "   fetching $($p.url)"
    Invoke-WebRequest -Uri $p.url -OutFile $dest -UseBasicParsing -UserAgent "Wget/1.21"
    $got = (Get-FileHash $dest -Algorithm SHA256).Hash
    if ($got -ne $p.sha.ToUpper()) {
        Remove-Item $dest
        throw "$($p.file): SHA-256 $got, expected $($p.sha). Upstream changed the file; check it and update the pin."
    }
    return $dest
}

function Run([string]$exe) {
    # Native commands do not throw in 5.1; their exit code is the only signal.
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$exe $args exited $LASTEXITCODE" }
}

Write-Host "$($App.name) $($App.version) ($($App.id))"
if ($NoSign) { Write-Host "(-NoSign: the kit builds unsigned installers anyway)" }

# ---------------------------------------------------------------- Python
Step "Python 3.13.16"
$PyDir = Join-Path $Work "python"
$Py = Join-Path $PyDir "tools\python.exe"
if (-not (Test-Path $Py)) {
    Expand-Archive -Path (Fetch "python") -DestinationPath $PyDir -Force
}
$Venv = Join-Path $Work "venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VPy)) { Run $Py -m venv $Venv }
Run $VPy -m pip install --quiet --disable-pip-version-check --cache-dir (Join-Path $Cache "pip") `
    -r (Join-Path $PSScriptRoot "build-requirements.txt") -r (Join-Path $Stage "requirements.txt")

# ---------------------------------------------------------------- freeze
Step "PyInstaller"
$Dist = Join-Path $Work "dist"
$Env:SHACK_STAGE = $Stage
try {
    Run $VPy -m PyInstaller --noconfirm --clean --log-level WARN --distpath $Dist --workpath (Join-Path $Work "pyi") `
        (Join-Path $Kit "pyinstaller\shack-app.spec")
} finally { Remove-Item Env:SHACK_STAGE }
$Bundle = Join-Path $Dist $App.executable
$Exe = Join-Path $Bundle "$($App.executable).exe"
if (-not (Test-Path $Exe)) { throw "PyInstaller made no $Exe" }

# ---------------------------------------------------------------- bundled programs
Step "Bundled programs"
# Its own script (bundle.ps1 says why). A child PowerShell, so its exit
# code is the signal as for any native step.
Run powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "bundle.ps1") `
    -Stage $Stage -Dest $Bundle -Work $Work

# ---------------------------------------------------------------- smoke test
if (-not $NoTests) {
    Step "Smoke test: the frozen server needs its launch token"
    # The executable in server mode, with the environment shack_desktop's
    # shell gives it (guard.py ENV_*): this is the path a launch takes,
    # minus the window.
    $port = Get-Random -Minimum 20000 -Maximum 60000
    $token = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]]).TrimEnd('=').Replace('+','-').Replace('/','_')
    $Env:SHACK_DESKTOP_HOST = "127.0.0.1"; $Env:SHACK_DESKTOP_PORT = "$port"; $Env:SHACK_DESKTOP_TOKEN = $token
    $proc = Start-Process -FilePath $Exe -ArgumentList "--server" -PassThru -WindowStyle Hidden
    Remove-Item Env:SHACK_DESKTOP_HOST, Env:SHACK_DESKTOP_PORT, Env:SHACK_DESKTOP_TOKEN
    try {
        $ready = $false
        for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
            Start-Sleep -Milliseconds 500
            $c = New-Object Net.Sockets.TcpClient
            try { $c.Connect("127.0.0.1", $port); $ready = $true } catch { } finally { $c.Close() }
            if ($proc.HasExited) { throw "the frozen server exited ($($proc.ExitCode)) before it listened" }
        }
        if (-not $ready) { throw "the frozen server did not listen on 127.0.0.1:$port in 30 s" }
        $refused = $false
        try { Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing | Out-Null } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403) { $refused = $true } else { throw }
        }
        if (-not $refused) { throw "the frozen server answered without the launch token" }
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -Headers @{ "X-Shack-Token" = $token }
        if ($r.StatusCode -ne 200) { throw "the frozen server refused its own token ($($r.StatusCode))" }
        Write-Host "   403 without the token, 200 with it ($($r.RawContentLength) bytes)"
    } finally {
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    }
}

# ---------------------------------------------------------------- Inno Setup
Step "Inno Setup"
$Iscc = $null
foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                 "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", (Join-Path $Work "innosetup\ISCC.exe"))) {
    if ($c -and (Test-Path $c)) { $Iscc = $c; break }
}
if (-not $Iscc) {
    # /PORTABLE=1: files only - no uninstaller, no registry, no Start menu.
    $is = Fetch "innosetup"
    $dir = Join-Path $Work "innosetup"
    $p = Start-Process $is -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /PORTABLE=1 /DIR=`"$dir`"" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Inno Setup unpack exited $($p.ExitCode)" }
    $Iscc = Join-Path $dir "ISCC.exe"
}
Write-Host "   $Iscc"

# ---------------------------------------------------------------- installer
Step "Installer"
$Base = "$($App.file_name)-windows-x64"
$InstallDir = $App.name -replace '^OpenHamShack\s+', ''
$isccArgs = @("/Q",
    "/DAppId=$($App.windows.app_guid.Trim('{', '}'))",
    "/DAppName=$($App.name)", "/DAppVersion=$($App.version)", "/DAppExe=$($App.executable).exe",
    "/DAppUserModelID=$($App.windows.app_user_model_id)", "/DPublisher=$($App.publisher)",
    "/DURL=$($App.homepage)", "/DSummary=$($App.summary)", "/DInstallDir=$InstallDir",
    "/DDistDir=$Bundle", "/DIconFile=$(Join-Path $Stage 'icons\app.ico')", "/DLicenseFile=$(Join-Path $Stage 'LICENSE')",
    "/DOutDir=$Out", "/DOutputBase=$Base",
    (Join-Path $PSScriptRoot "shack-app.iss"))
Run $Iscc @isccArgs
$Setup = Join-Path $Out "$Base.exe"
$hash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
# The same "<hash>  <name>" line sha256sum writes on Linux, ASCII and no
# BOM, so `sha256sum -c` reads both alike.
[IO.File]::WriteAllText("$Setup.sha256", "$hash  $Base.exe`n", [Text.Encoding]::ASCII)
Write-Host ""
Write-Host "built $Setup ($([math]::Round((Get-Item $Setup).Length / 1MB, 1)) MB)"
Write-Host "sha256 $hash"
Write-Host "UNSIGNED (beta): SmartScreen will warn until the installers are signed"
exit 0
