#!/bin/sh
# Build the packet rig's Windows lab kit ISO and attach it to an office VM.
#
#   make-kit.sh VMID INSTALLER.exe [PASSWORD_FILE]
#
# The kit: setup.cmd, setup.ps1, the password (deleted by setup.ps1 once
# used), shack's public key, the OpenSSH, Tailscale and Python installers,
# and the Packet Radio installer. The ISO is made on office (genisoimage is there,
# not here) in /var/lib/vz/template/iso as ohs-kit-VMID.iso and attached as
# ide3. Detach and delete it when the VM is set up (it holds the password):
#   ssh office 'sudo qm set VMID --delete ide3; sudo rm /var/lib/vz/template/iso/ohs-kit-VMID.iso'
#
# Then, at the VM's OOBE: Shift+F10, and run setup.cmd from the kit's drive
# (sendkeys.py types it - the VM has only a display):
#   sendkeys.py VMID --keys shift-f10
#   sendkeys.py VMID --text 'for %d in (D E F G H) do if exist %d:\setup.cmd %d:\setup.cmd' --enter
# The office images also run the QEMU guest agent, so instead of typing:
#   ssh office 'sudo qm guest exec VMID -- cmd /c "for %d in (D E F G H) do if exist %d:\\setup.cmd %d:\\setup.cmd"'
# An IDE CD added to a running VM only appears after a stop and start.
set -eu
vmid=$1
exe=$2
pwfile=${3:-$HOME/.config/ohs-twostation/vm$vmid-labadmin.pw}
here=$(cd "$(dirname "$0")" && pwd)
stage=$HOME/build/ohs-winlab-$vmid
mkdir -p "$stage"
cache=$HOME/build/ohs-winlab-cache
mkdir -p "$cache"
[ -s "$cache/openssh.msi" ] || curl -fsSL -o "$cache/openssh.msi" \
    https://github.com/PowerShell/Win32-OpenSSH/releases/download/v9.8.3.0p2-Preview/OpenSSH-Win64-v9.8.3.0.msi
[ -s "$cache/tailscale.msi" ] || curl -fsSL -o "$cache/tailscale.msi" \
    https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi
[ -s "$cache/python.exe" ] || curl -fsSL -o "$cache/python.exe" \
    https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
cp "$cache"/openssh.msi "$cache"/tailscale.msi "$cache"/python.exe "$stage/"
cp "$here/setup.cmd" "$here/setup.ps1" "$stage/"
cp "$exe" "$stage/packet-setup.exe"
cp "$HOME/.ssh/id_ed25519.pub" "$stage/shack.pub"
umask 077
pw=$(cat "$pwfile")
printf '%s' "$pw" > "$stage/pw.txt"
# CRLF for the batch file: cmd mis-parses LF-only lines after a label or rem.
sed -i 's/$/\r/' "$stage/setup.cmd"
tar -C "$stage" -cf - . | ssh office "d=\$(mktemp -d) && tar -C \$d -xf - && sudo genisoimage -quiet -J -R -V OHSKIT -o /var/lib/vz/template/iso/ohs-kit-$vmid.iso \$d && rm -rf \$d"
rm -rf "$stage"
ssh office "sudo qm set $vmid --ide3 local:iso/ohs-kit-$vmid.iso,media=cdrom"
echo "attached ohs-kit-$vmid.iso to VM $vmid as ide3"
