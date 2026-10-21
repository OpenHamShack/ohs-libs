#!/usr/bin/env python3
"""A UDP relay that lets a computer on the tailnet use an Icom LAN radio on
shack's LAN, for the two-station packet rig (twostation.py).

Station A of that rig is the Windows app on an office VM; the IC-9700 is on
shack's LAN (198.51.100.150), which the tailnet does not route to. This
relay listens on shack's TAILNET address only, on the radio's own three
port numbers (50001 control, 50002 CI-V, 50003 audio), and forwards each
client's packets to the same port on the radio.

Why per-client sockets, and why they copy the client's port:

- **The radio answers the address and port a packet came from.** So every
  (client address, client port, radio port) gets an upstream socket of its
  own on shack's LAN side; whatever the radio sends to that socket goes
  back to that client, from the port the client sent to. This is the same
  symmetric mapping a NAT router port-forwarding RS-BA1 gives, which the
  protocol is known to survive.
- **The conninfo packet also names the client's local CI-V and audio port**
  (shack_rig.icom.lan: "here is the port I am listening on"), and the
  client binds its stream socket to that same port. If the radio ever sends
  to the advertised port rather than the source, a relay socket on an
  arbitrary port would never hear it. So the upstream socket is bound to the
  client's own port number when shack has it free, which makes the two
  answers the same; it falls back to any port (and says so) when not.
- **Nothing is rewritten.** The protocol's sender ids are derived from the
  client's address (lan.py my_id), which the radio treats as opaque tokens;
  the radio's reported stream ports are its own and are reached through the
  relay unchanged because the relay listens on the same numbers.

Safety: the relay keys nothing - it moves bytes, and the radio's one LAN
client at a time rule still holds (a second client is the radio's to refuse).
It only listens on the address given, which must not be a wildcard, and only
accepts clients from --allow (default: the tailnet, 100.64.0.0/10). ufw on
shack allows everything on tailscale0 inbound, and the radio's answers come
back on flows shack opened, which conntrack lets in; nothing on the firewall
changes for this.

Run as a systemd user service (`install-unit` writes it):

    python3 icom_relay.py --listen shack --radio 198.51.100.150
    python3 icom_relay.py install-unit [same options]   # then systemctl --user start ohs-icom-relay
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import selectors
import shutil
import socket
import sys
import time

log = logging.getLogger("icom_relay")

PORTS = (50001, 50002, 50003)
IDLE_S = 30.0
UNIT = "ohs-icom-relay.service"


class Relay:
    """One selector loop: a listening socket per radio port, an upstream
    socket per (client, radio port)."""

    def __init__(self, listen: str, radio: str, ports=PORTS, allow=("100.64.0.0/10",), idle_s: float = IDLE_S,
                 upstream_bind: str = "0.0.0.0", clock=time.monotonic):
        if ipaddress.ip_address(listen).is_unspecified:
            raise SystemExit("refusing a wildcard listen address: give shack's tailnet address")
        self.listen = listen
        self.radio = radio
        self.ports = tuple(ports)
        self.allow = [ipaddress.ip_network(a, strict=False) for a in allow]
        self.idle_s = idle_s
        self.upstream_bind = upstream_bind
        self.clock = clock
        self.sel = selectors.DefaultSelector()
        self.front: dict[int, socket.socket] = {}
        #: (radio port, client addr) -> [upstream socket, last activity, packets up, packets down]
        self.sessions: dict[tuple[int, tuple], list] = {}
        self.refused = 0
        self._last_report = clock()
        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((listen, port))
            s.setblocking(False)
            self.front[port] = s
            self.sel.register(s, selectors.EVENT_READ, ("front", port))
        log.info("relaying %s:%s -> %s", listen, ",".join(map(str, self.ports)), radio)

    def _allowed(self, host: str) -> bool:
        a = ipaddress.ip_address(host)
        return any(a in n for n in self.allow)

    def _upstream(self, port: int, client: tuple) -> list:
        key = (port, client)
        sess = self.sessions.get(key)
        if sess is not None:
            return sess
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            up.bind((self.upstream_bind, client[1]))
            how = "same port"
        except OSError:
            up.bind((self.upstream_bind, 0))
            how = "port %d taken here, using another" % client[1]
        up.connect((self.radio, port))
        up.setblocking(False)
        sess = [up, self.clock(), 0, 0]
        self.sessions[key] = sess
        self.sel.register(up, selectors.EVENT_READ, ("up", key))
        log.info("session %s:%d -> radio:%d via local port %d (%s)", client[0], client[1], port,
                 up.getsockname()[1], how)
        return sess

    def _drop(self, key, why: str) -> None:
        sess = self.sessions.pop(key, None)
        if sess is None:
            return
        self.sel.unregister(sess[0])
        sess[0].close()
        log.info("session %s:%d radio:%d ended (%s): %d up, %d down", key[1][0], key[1][1], key[0], why,
                 sess[2], sess[3])

    def step(self, timeout: float = 1.0) -> None:
        for sk, _ in self.sel.select(timeout):
            kind, ref = sk.data
            try:
                if kind == "front":
                    data, client = sk.fileobj.recvfrom(65536)
                    if not self._allowed(client[0]):
                        self.refused += 1
                        if self.refused <= 5:
                            log.warning("refused a packet from %s (not in --allow)", client[0])
                        continue
                    sess = self._upstream(ref, client)
                    sess[0].send(data)
                    sess[1] = self.clock()
                    sess[2] += 1
                else:
                    sess = self.sessions.get(ref)
                    if sess is None:
                        continue
                    data = sk.fileobj.recv(65536)
                    port, client = ref
                    self.front[port].sendto(data, client)
                    sess[1] = self.clock()
                    sess[3] += 1
            except (BlockingIOError, InterruptedError):
                pass
            except ConnectionRefusedError:
                # ICMP port unreachable from the radio (or the client): the
                # other end is not there; let the idle timer reap it.
                pass
        now = self.clock()
        for key, sess in list(self.sessions.items()):
            if now - sess[1] > self.idle_s:
                self._drop(key, "idle")
        if now - self._last_report > 60 and self.sessions:
            self._last_report = now
            log.info("%d session(s): %s", len(self.sessions),
                     ", ".join(f"{c[0]}:{c[1]}->{p} {s[2]}/{s[3]}" for (p, c), s in self.sessions.items()))

    def run(self) -> None:
        while True:
            self.step()

    def close(self) -> None:
        for key in list(self.sessions):
            self._drop(key, "closing")
        for s in self.front.values():
            self.sel.unregister(s)
            s.close()
        self.front.clear()
        self.sel.close()


def install_unit(argv: list[str]) -> None:
    """Copy this script somewhere stable and write a user unit that runs it
    with the given options (the worktree it came from may go away)."""
    home = os.path.expanduser("~")
    share = os.path.join(home, ".local", "share", "ohs-twostation")
    os.makedirs(share, exist_ok=True)
    dest = os.path.join(share, "icom_relay.py")
    shutil.copy2(os.path.abspath(__file__), dest)
    unitdir = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(unitdir, exist_ok=True)
    args = " ".join(argv)
    with open(os.path.join(unitdir, UNIT), "w") as f:
        f.write(f"""[Unit]
Description=OpenHamShack two-station rig: IC-9700 LAN relay on the tailnet (keys nothing)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 {dest} {args}
# The tailnet address may not be up yet at login: try again rather than give up.
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
    print(f"wrote {os.path.join(unitdir, UNIT)}; now: systemctl --user daemon-reload && "
          f"systemctl --user start {UNIT.removesuffix('.service')}  (stop: systemctl --user stop ...)")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    installing = bool(argv) and argv[0] == "install-unit"
    if installing:
        argv = argv[1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", required=True, help="shack's tailnet address (never a wildcard)")
    ap.add_argument("--radio", default="198.51.100.150")
    ap.add_argument("--ports", default=",".join(map(str, PORTS)))
    ap.add_argument("--allow", default="100.64.0.0/10", help="comma-separated client networks")
    ap.add_argument("--idle", type=float, default=IDLE_S)
    a = ap.parse_args(argv)
    if installing:
        install_unit(argv)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    relay = Relay(a.listen, a.radio, [int(p) for p in a.ports.split(",")], a.allow.split(","), a.idle)
    try:
        relay.run()
    except KeyboardInterrupt:
        pass
    finally:
        relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
