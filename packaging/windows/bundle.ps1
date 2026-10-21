<#
.SYNOPSIS
Put an app's [[windows.bundle]] programs into its frozen folder.

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File packaging\windows\bundle.ps1 -Stage <stage> -Dest <frozen folder> -Work <dir> [-SevenZip <7z>]

build-windows.ps1 runs this after PyInstaller. For each bundle in the
staged app.json (shack-app.toml's [[windows.bundle]], checked by
shackpkg.py): fetch its archive into <Work>\cache, refuse it unless its
SHA-256 matches the pin, take the named files out flat into
<Dest>\<name>\ (beside the executable, where the frozen launcher puts the
folder first on PATH), its licence texts into <Dest>\licences\, then run
its check - the program as the app will run it, and the words its output
must hold - so a build that lost an encoder fails here, not on a
tester's PC. The app's notice goes in as <Dest>\THIRD-PARTY.txt.

The archives are opened with 7-Zip: FFmpeg's and mpv's Windows builds are
.7z with the BCJ2 filter, which neither Expand-Archive nor Windows' own
tar reads. 7-Zip 23.01's MSI is itself a pinned download, unpacked with
msiexec /a into <Work>\7zip (nothing installed) - openhamshack-datv's pin and
method, proved there. -SevenZip names another 7z (or Linux's 7zz), which
is how packaging/tests runs this script under PowerShell 7 on Linux.

A separate script rather than a section of build-windows.ps1 so that it
can be run, and tested, on its own: everything else in the Windows build
needs Windows.

Windows PowerShell 5.1 compatible, as build-windows.ps1 is.
#>
param(
    [Parameter(Mandatory = $true)][string]$Stage,
    [Parameter(Mandatory = $true)][string]$Dest,
    [Parameter(Mandatory = $true)][string]$Work,
    [string]$SevenZip = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$App = Get-Content (Join-Path $Stage "app.json") -Raw | ConvertFrom-Json
$Cache = Join-Path $Work "cache"
New-Item -ItemType Directory -Force -Path $Cache | Out-Null

# Wrapped in @(): PowerShell 5.1's ConvertFrom-Json hands back a lone
# element for a one-element array, and $null (not an empty array) for a
# missing key, whose Count would then be 1.
$Bundles = @()
if ($App.windows.bundle) { $Bundles = @($App.windows.bundle) }
if ($Bundles.Count -eq 0) { Write-Host "   no bundled programs"; exit 0 }

function FetchPinned([string]$file, [string[]]$urls, [string]$sha) {
    # The cached copy if it still matches; otherwise each address in turn.
    # Anything that does not match the pin is deleted, never used.
    $dest = Join-Path $Cache $file
    if (Test-Path $dest) {
        if ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $sha.ToUpper()) { return $dest }
        Remove-Item $dest
    }
    $why = @()
    foreach ($url in $urls) {
        Write-Host "   fetching $url"
        try {
            # A plain tool's user agent: SourceForge answers PowerShell's
            # default one with an HTML page instead of the file.
            Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -UserAgent "Wget/1.21"
        } catch {
            $why += "$url : $($_.Exception.Message)"
            continue
        }
        $got = (Get-FileHash $dest -Algorithm SHA256).Hash
        if ($got -eq $sha.ToUpper()) { return $dest }
        Remove-Item $dest
        $why += "$url : SHA-256 $got, expected $sha"
    }
    throw "$file could not be fetched as pinned (upstream changed or moved it; check it and update the pin):`n$($why -join "`n")"
}

function Run([string]$exe) {
    # Native commands do not throw; the exit code is the only signal.
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$exe $args exited $LASTEXITCODE" }
}

function Leaf([string]$member) { return ($member -split '[/\\]')[-1] }

if (-not $SevenZip) {
    $dir = Join-Path $Work "7zip"
    $SevenZip = Join-Path $dir "Files\7-Zip\7z.exe"
    if (-not (Test-Path $SevenZip)) {
        $msi = FetchPinned "7z2301-x64.msi" @("https://www.7-zip.org/a/7z2301-x64.msi") `
            "0ba639b6dacdf573d847c911bd147c6384381a54dac082b1e8c77bc73d58958b"
        $p = Start-Process msiexec.exe -ArgumentList "/a `"$msi`" /qn TARGETDIR=`"$dir`"" -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "msiexec /a 7-Zip exited $($p.ExitCode)" }
    }
}

$Lic = Join-Path $Dest "licences"
New-Item -ItemType Directory -Force -Path $Lic | Out-Null
foreach ($b in $Bundles) {
    $arc = FetchPinned $b.archive @($b.urls) $b.sha256
    # Rebuilt every time: nothing from an earlier build or pin can ship.
    $dir = Join-Path $Dest $b.name
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # 7z e: out flat, whatever folders the archive keeps them in. Members
    # are named with "/" in shack-app.toml; 7-Zip on Windows lists "\".
    $sep = [IO.Path]::DirectorySeparatorChar
    $members = @($b.files | ForEach-Object { $_.Replace('/', $sep) })
    Run $SevenZip e -y -bso0 -bsp0 "-o$dir" $arc @members
    foreach ($f in $b.files) {
        $p = Join-Path $dir (Leaf $f)
        if (-not (Test-Path $p)) { throw "$($b.archive) has no $f" }
        Write-Host "   $($b.name)$sep$(Leaf $f)  $((Get-FileHash $p -Algorithm SHA256).Hash.ToLower())"
    }
    # Licence texts: an archive member, or a pinned download of its own
    # (mpv's builds carry none). Each through a scratch folder, so two
    # archives' LICENSE files cannot overwrite each other.
    foreach ($l in $b.licences.PSObject.Properties) {
        $to = Join-Path $Lic $l.Name
        if ($l.Value.member) {
            $tmp = Join-Path $Work "licence-tmp"
            if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
            Run $SevenZip e -y -bso0 -bsp0 "-o$tmp" $arc $l.Value.member.Replace('/', $sep)
            $got = Join-Path $tmp (Leaf $l.Value.member)
            if (-not (Test-Path $got)) { throw "$($b.archive) has no $($l.Value.member)" }
            Move-Item -Force $got $to
            Remove-Item -Recurse -Force $tmp
        } else {
            Copy-Item (FetchPinned "$($b.name)-$($l.Name)" @($l.Value.urls) $l.Value.sha256) $to
        }
    }
    if ($b.check) {
        # The program as the app will run it, from its bundle folder.
        # stderr is output here, not failure (ffmpeg prints its banner and
        # mpv its version there): only the exit code and the words count.
        $exe = Join-Path $dir (Leaf $b.files[0])
        $checkArgs = @($b.check.args)
        $eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try { $said = (& $exe @checkArgs 2>&1 | ForEach-Object { "$_" }) -join "`n" } finally { $ErrorActionPreference = $eap }
        if ($LASTEXITCODE -ne 0) { throw "$exe $checkArgs exited $LASTEXITCODE" }
        $missing = @($b.check.expect | Where-Object { $said -notmatch [regex]::Escape($_) })
        if ($missing.Count -gt 0) { throw "the bundled $($b.name) lacks what the app needs: $($missing -join ', ')" }
        Write-Host "   $($b.name) has: $(@($b.check.expect) -join ', ')"
    }
}
$notice = Join-Path $Stage "THIRD-PARTY.txt"
if (-not (Test-Path $notice)) { throw "the stage has bundled programs but no THIRD-PARTY.txt ([windows] notices)" }
Copy-Item $notice (Join-Path $Dest "THIRD-PARTY.txt")
exit 0
