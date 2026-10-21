"""tools/icom_relay.py: the two-station rig's tailnet relay to the IC-9700.

Run on loopback: the relay listens on 127.0.0.1, a stand-in radio answers on
127.0.0.2 the way the IC-9700 does (to the address and port a packet came
from), and the relay's own upstream sockets sit on 127.0.0.3 so that they can
take the client's port number without colliding with the client itself.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("icom_relay", Path(__file__).parent.parent / "tools" / "icom_relay.py")
icom_relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(icom_relay)


def _free_ports(n: int) -> list[int]:
    out = []
    socks = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        out.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return out


class FakeRadio:
    """Answers every packet to its source with b"re:" + packet, and notes
    where each came from."""

    def __init__(self, ports):
        self.socks = {}
        self.seen: list[tuple[int, tuple, bytes]] = []
        for p in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.2", p))
            s.settimeout(0.1)
            self.socks[p] = s
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            for p, s in self.socks.items():
                try:
                    data, src = s.recvfrom(2048)
                except (TimeoutError, socket.timeout):
                    continue
                self.seen.append((p, src, data))
                s.sendto(b"re:" + data, src)

    def close(self):
        self.stop.set()
        self.thread.join(1)
        for s in self.socks.values():
            s.close()


@pytest.fixture
def rig():
    ports = _free_ports(3)
    radio = FakeRadio(ports)
    now = [0.0]
    relay = icom_relay.Relay("127.0.0.1", "127.0.0.2", ports, ["127.0.0.0/8"], idle_s=30,
                             upstream_bind="127.0.0.3", clock=lambda: now[0])
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            relay.step(0.02)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield relay, radio, ports, now
    stop.set()
    t.join(1)
    relay.close()
    radio.close()


def _client(port=0):
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.bind(("127.0.0.1", port))
    c.settimeout(2)
    return c


def test_each_radio_port_answers_back_through_the_port_the_client_used(rig):
    relay, radio, ports, _ = rig
    clients = [_client() for _ in ports]
    try:
        for c, p in zip(clients, ports):
            c.sendto(b"hello %d" % p, ("127.0.0.1", p))
        for c, p in zip(clients, ports):
            data, src = c.recvfrom(2048)
            assert data == b"re:hello %d" % p
            # The answer comes from the relay port the client sent to, as the
            # radio's would: the client's stream sockets are "connected".
            assert src == ("127.0.0.1", p)
        # The radio saw each stream from the client's own port number, so a
        # radio that sends to the port advertised in conninfo reaches it too.
        by_port = {p: src for p, src, _ in radio.seen}
        for c, p in zip(clients, ports):
            assert by_port[p] == ("127.0.0.3", c.getsockname()[1])
    finally:
        for c in clients:
            c.close()


def test_two_packets_from_one_client_share_one_upstream_socket(rig):
    relay, radio, ports, _ = rig
    c = _client()
    try:
        for i in range(3):
            c.sendto(b"%d" % i, ("127.0.0.1", ports[0]))
            assert c.recvfrom(2048)[0] == b"re:%d" % i
        assert len({src for _, src, _ in radio.seen}) == 1
        assert len(relay.sessions) == 1
    finally:
        c.close()


def test_a_client_outside_allow_gets_nothing_through():
    ports = _free_ports(1)
    radio = FakeRadio(ports)
    relay = icom_relay.Relay("127.0.0.1", "127.0.0.2", ports, ["100.64.0.0/10"], upstream_bind="127.0.0.3")
    c = _client()
    try:
        c.sendto(b"x", ("127.0.0.1", ports[0]))
        for _ in range(10):
            relay.step(0.02)
        assert radio.seen == []
        assert relay.refused == 1
        assert relay.sessions == {}
    finally:
        c.close()
        relay.close()
        radio.close()


def test_a_wildcard_listen_address_is_refused():
    with pytest.raises(SystemExit):
        icom_relay.Relay("0.0.0.0", "127.0.0.2", _free_ports(1))


def test_an_idle_session_is_let_go(rig):
    relay, radio, ports, now = rig
    c = _client()
    try:
        c.sendto(b"x", ("127.0.0.1", ports[1]))
        c.recvfrom(2048)
        assert len(relay.sessions) == 1
        now[0] += 31
        c.sendto(b"y", ("127.0.0.1", ports[2]))       # any traffic runs the reaper
        c.recvfrom(2048)
        assert [k[0] for k in relay.sessions] == [ports[2]]
    finally:
        c.close()
