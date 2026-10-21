"""shack_pluto: what every OpenHamShack app that keys a Pluto needs, and
nothing that is one app's modem.

- `guard`: the transmitter guard that runs on the Pluto itself, so a
  process that dies mid-over cannot leave it transmitting;
- `identity`: which firmware family a Pluto runs, which chip it is set up
  as, what it can tune, and the key it is remembered by;
- `ad9364`: the range check and the one-click AD9364 enable, with the
  reboot under a TX-LO quiet watcher;
- `xo`: each Pluto's crystal correction, kept on the computer.

Product-neutral (PLAN-0.2 §3.6.6): Packet Radio uses it first, and QO-100
NB, which keys a Pluto with the same in-process-only quieting, can adopt it.
"""

from __future__ import annotations

__version__ = "0.1.0"
