# Packet rig station A: turn a Windows 11 VM parked at OOBE into a lab
# machine - local admin labadmin logged on automatically, OpenSSH with
# shack's key, Tailscale (not signed in), Python - then leave OOBE and
# reboot. Runs as SYSTEM: from the OOBE prompt (setup.cmd), or through the
# QEMU guest agent (make-kit.sh says how). Logs to C:\ohs\setup.log.
#
# Why not an answer file and sysprep /oobe: tried first on VM 132 (18/10/2026).
# An en-GB UILanguage on the en-US evaluation image fails OOBE outright ("Why
# did my PC restart?"), and with that fixed the re-run OOBE failed the same way
# again with nothing in setuperr.log. Doing the work here and then marking
# OOBE finished in HKLM\SYSTEM\Setup worked first time.
Start-Transcript -Path C:\ohs\setup.log -Append
$ErrorActionPreference = 'Continue'
$pw = (Get-Content C:\ohs\pw.txt -Raw).Trim()
if (-not (Get-LocalUser labadmin -ErrorAction SilentlyContinue)) {
  New-LocalUser labadmin -Password (ConvertTo-SecureString $pw -AsPlainText -Force) -FullName 'Lab Admin' | Out-Null
  Add-LocalGroupMember Administrators labadmin }
Set-LocalUser labadmin -PasswordNeverExpires $true
# Log labadmin on at every boot: the harness drives the app's window, which
# needs a live desktop, and nobody is there to type a password.
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $wl AutoAdminLogon '1'; Set-ItemProperty $wl DefaultUserName 'labadmin'; Set-ItemProperty $wl DefaultPassword $pw
Remove-ItemProperty $wl AutoLogonCount -ErrorAction SilentlyContinue
Remove-Item C:\ohs\pw.txt -Force
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
# Never sleep or lock.
powercfg /change standby-timeout-ac 0; powercfg /change monitor-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
# OpenSSH server from the MSI: the Windows capability needs Windows Update and
# can sit waiting on a reboot ("pending operations").
Start-Process msiexec -Wait -ArgumentList '/i','C:\ohs\openssh.msi','/qn','ADDLOCAL=Server'
New-Item -ItemType Directory -Force C:\ProgramData\ssh | Out-Null
Copy-Item C:\ohs\shack.pub C:\ProgramData\ssh\administrators_authorized_keys -Force
# sshd ignores the admin keys file silently unless only Administrators and SYSTEM can touch it.
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'
New-Item -Force HKLM:\SOFTWARE\OpenSSH | Out-Null
New-ItemProperty HKLM:\SOFTWARE\OpenSSH DefaultShell -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force
# The MSI's config names sftp-server.exe without a path, and this build then
# drops every SFTP session ("Connection closed"), which is all scp does now.
$cfg = 'C:\ProgramData\ssh\sshd_config'
if (Test-Path $cfg) {
  (Get-Content $cfg) -replace '^Subsystem\s+sftp\s+sftp-server.exe', 'Subsystem sftp C:/PROGRA~1/OpenSSH/sftp-server.exe' |
    Set-Content $cfg -Encoding ascii }
Set-Service sshd -StartupType Automatic; Restart-Service sshd
# The harness's window driver (twostation_win.py) uses pywinauto, whose
# pywin32 needs the VC++ runtime's MFC DLL, which a fresh Windows lacks.
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile C:\ohs\vc_redist.x64.exe
Start-Process C:\ohs\vc_redist.x64.exe -Wait -ArgumentList '/install','/quiet','/norestart'
if (-not (Get-NetFirewallRule -Name OHS-sshd -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name OHS-sshd -DisplayName 'OpenSSH (OHS lab)' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null }
# Tailscale, installed but not signed in: Brian approves the login.
Start-Process msiexec -Wait -ArgumentList '/i','C:\ohs\tailscale.msi','/qn','TS_UNATTENDEDMODE=always','TS_NOLAUNCH=true'
# Python for the harness's window driver (pywinauto), for all users.
Start-Process C:\ohs\python.exe -Wait -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0'
& 'C:\Program Files\Python312\python.exe' -m pip install --quiet pywinauto
# Not the packet app: its installer is per user, and this runs as SYSTEM.
# Install it over SSH as labadmin afterwards (twostation.py install-a).
# Leave OOBE: the machine is set up as far as anyone needs.
$k = 'HKLM:\SYSTEM\Setup'
Set-ItemProperty $k OOBEInProgress 0; Set-ItemProperty $k SetupType 0; Set-ItemProperty $k SetupPhase 0; Set-ItemProperty $k CmdLine ''
if ($env:OHS_NAME) { Rename-Computer -NewName $env:OHS_NAME -Force -WarningAction SilentlyContinue }
'setup done' | Out-File C:\ohs\setup.done
Stop-Transcript
Restart-Computer -Force
