# shack-rig

The OpenHamShack rig-control library (PLATFORM-PLAN §1.4): what the per-maker
rig apps share. Extracted from OpenHamShack Icom (`~/dock360-apps-icom-9700`,
branch `icom`, at 24a343a); `docs/RIG.md` is the adoption guide for that app
and any other.

| Module | What |
|---|---|
| `shack_rig.rig` | The `Rig` surface every backend implements, and the state keys. |
| `shack_rig.model` | `RigModel` (modes, coverage, power per band), mode names, the DATA flag, band buttons. |
| `shack_rig.lease` | `TxLease` (what the app hands in to key), `PttRefused`, a reference `Lease`. |
| `shack_rig.rigctl` | A hamlib NET rigctld server over any `Rig`, for WSJT-X and friends. |
| `shack_rig.serialio` | An exclusive, raw serial port in pure termios (pyserial on Windows); RTS/DTR low on open and close, raised only by a PTT driver's `set_line`. |
| `shack_rig.usbaudio` | Receive audio from a radio's USB codec via `arecord`, decimated exactly to 12 kHz. |
| `shack_rig.sound` | Sound devices by name on any desktop (PortAudio via pyaudio, `[audio]` extra), at the device's own rate; `FakeBackend` for tests. |
| `shack_rig.txaudio` | Transmit audio to a USB codec: 12 kHz in, the codec's rate out by an exact integer factor, via `aplay` (appliance) or PortAudio (desktop). |
| `shack_rig.yaesu` | The Yaesu backend, FTX-1 first (from its CAT manual 2508-C): `cat` (codec, the 30-character IF), `models` (FTX-1; FT-991A from hamlib, unverified), `rig.YaesuRig` (CAT on the Enhanced COM port, PTT by `TX1;` or a line on the Standard port, the squelch as busy, MOD SOURCE read/set/restored, audio at the codec's rate), `fake.FakeFtx1`. |
| `shack_rig.icom` | The Icom backend: `civ` (codec), `lan` (Icom's UDP protocol), `link.LanLink`, `usb.UsbLink`, `usbprobe` (finding the port, speed and address), `models` (IC-9700/7610/705/7600), `rig.IcomRig`, `build.make_rig(profile)`, `datasetup` and `satdd` (the packet, satellite-pass and DD set-ups, journalled and put back), and `fake.FakeIcom` for tests. |

Standard library only. USB audio needs `arecord` (alsa-utils) on the host,
and `aplay` for USB transmit audio there; a desktop installs the `desktop`
extra (pyaudio, and pyserial on Windows) instead.

## Transmit

The library keys a radio only through `rig.set_ptt(True, lease)`, where
`lease` is the app's TX lease and broker claim as a `TxLease`. Without one it
raises `PttRefused`; while keyed the rig re-asks the lease four times a second
and unkeys the moment it refuses. Unkeying never needs a lease. Transmit audio
is refused unless the rig is keyed under a lease. `lease.py` explains each
choice.

## Tests

```
tools/test.sh          # makes ~/build/venvs/shack-rig on first run
```

No radio is touched: LAN tests talk to `FakeIcom` on 127.0.0.1, serial tests
to a radio made of a pty (an IC-7610 or `FakeFtx1`), USB-audio tests to a
stand-in `arecord` or `aplay`, sound-device tests to `sound.FakeBackend`.

## Comments that point elsewhere

The comments moved with the code and still cite the Icom app's own documents
(`docs/TRANSPORTS.md`, `docs/OFF-AIR.md`, `docs/USB-AUDIO.md`,
`docs/STANDALONE-PI.md`) and its modules (`main.py`, `digi/*`). Those are in
`~/dock360-apps-icom-9700/icom/`, where the measurements they quote were made;
they are kept as citations rather than copied here, so there is one copy of
each measurement.

Licence: GPL-3.0-or-later. The Icom LAN packet layouts and handshake come from
wfview (GPL-3.0).
