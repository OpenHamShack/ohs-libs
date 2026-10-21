# shack-pluto

What every OpenHamShack app that keys a Pluto (ADALM-Pluto, its clones,
the Tezuka nano) needs, kept out of any one app's modem:

- `guard.py`: the transmitter guard that runs **on the Pluto**, started over
  SSH as a session's remote command, so a process that dies mid-over cannot
  leave the transmitter up. It quiets at EOF, after 3 s without a heartbeat,
  on a hang-up, and when the TX LO has been up longer than the app's limit
  plus 2 s. No confirmed guard, no transmit.
- `identity.py`: firmware family (ADI and clones, Tezuka, PlutoDVB2), chip
  setting and range as read from the radio, and the key a Pluto's settings
  are filed under (serial, else the USB network's MAC).
- `ad9364.py`: the range check and the one-click AD9364 enable: backup,
  three `fw_setenv`s read back, a detached reboot under a TX-LO quiet
  watcher, verify. ADI firmware only, and only with the operator's word that
  a dummy load is on TX.
- `xo.py`: each Pluto's crystal correction, kept on the computer and
  written at every open.

Stdlib only; `pip install shack-pluto[ssh]` adds paramiko. libiio is the
caller's. The tests run the guard script itself under the host's busybox
`sh` (the Pluto's shell) against a stand-in sysfs:

    ~/build/venvs/<yours>/bin/python -m pytest python/shack_pluto/tests

Licence GPL-3.0-or-later.
