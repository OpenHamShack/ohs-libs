; Inno Setup template for any OpenHamShack app (packaging/README.md).
; build-windows.ps1 passes everything app-specific as /D defines, read
; from the staged app.json, so no app needs a script of its own:
;   AppId AppName AppVersion AppExe AppUserModelID Publisher URL Summary
;   DistDir IconFile LicenseFile OutDir OutputBase InstallDir
;
; Generalised from openhamshack-nb's packaging/windows/openhamshack-nb.iss and kept
; the same in kind: per-user install by default (PrivilegesRequired=lowest:
; no UAC prompt, into %LOCALAPPDATA%\Programs), with Program Files offered
; by the wizard or /ALLUSERS for a shared machine; an upgrade replaces the
; whole bundle so a DLL a newer PyInstaller dropped cannot linger; and
; uninstalling leaves the operator's data (%LOCALAPPDATA%\OpenHamShack, the
; shared station store and each app's folder) exactly as the .deb leaves
; ~/.local/share: reinstalling must not cost them their station.
;
; Unsigned for the beta (BETA-PLAN: "Installers are unsigned"); SmartScreen
; will warn, and the download page says what to click.

#ifndef AppVersion
  #error Pass the defines from build-windows.ps1
#endif

[Setup]
; Never change AppId (it comes from shack-app.toml [windows] app_guid): it
; is how a newer installer finds the old install to upgrade in place.
AppId={{{#AppId}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#URL}
AppComments={#Summary}
DefaultDirName={autopf}\OpenHamShack\{#InstallDir}
DefaultGroupName=OpenHamShack
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutDir}
OutputBaseFilename={#OutputBase}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
LicenseFile={#LicenseFile}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Close a running copy before replacing its files, rather than leaving a
; half-updated folder behind a locked DLL.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; AppUserModelID groups the window under this shortcut's icon on the
; taskbar (REPO-LAYOUT §5: OpenHamShack.<Kind>).
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "{#Summary}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "{#AppUserModelID}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
