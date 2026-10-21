@echo off
rem Packet rig lab VM: run from the OOBE command prompt (Shift+F10) with the
rem kit ISO attached. Copies the kit to C:\ohs and runs setup.ps1 as SYSTEM,
rem which sets the machine up, leaves OOBE and reboots.
mkdir C:\ohs
xcopy /y %~dp0* C:\ohs\
set OHS_NAME=OHS-PACKET-A
powershell -NoProfile -ExecutionPolicy Bypass -File C:\ohs\setup.ps1
