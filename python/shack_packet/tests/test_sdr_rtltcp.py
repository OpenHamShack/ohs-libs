"""The rtl_tcp client against a server that speaks the protocol: it tunes,
its band decodes, it never sends a crystal correction or bias-tee, and
every way a server can fail has a reason."""

from __future__ import annotations

import socket
import struct
import threading

import pytest
from sdrhelp import CENTRE, RATE, WANT, band, decode, texts

from shack_packet.sdr import RtlTcpServer, RtlTcpSource, SdrError


def test_rtl_tcp_tunes_the_server_and_its_band_decodes():
    with RtlTcpServer(lambda c, r: band(c, r, seconds=3.0), realtime=False) as server:
        src = RtlTcpSource("127.0.0.1", server.port, CENTRE, RATE, gain=29.7)
        pipe = decode(src, limit_s=3.0)
        assert texts(pipe) == WANT
        assert src.tuner == "R820T" and src.gain_steps == 29
    names = [n for n, _ in server.commands]
    assert dict(server.commands)["freq"] == int(CENTRE) and dict(server.commands)["rate"] == RATE
    assert ("gain_mode", 1) in server.commands and ("gain", 297) in server.commands
    assert "ppm" not in names and "bias_tee" not in names          # corrected in software; never powered


def test_rtl_tcp_failures_have_reasons():
    with pytest.raises(SdrError) as e:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        RtlTcpSource("127.0.0.1", port, timeout_s=1).open()
    assert e.value.reason == "no-device"
    with RtlTcpServer(lambda c, r: band(c, r), realtime=False, header=b"HTTP/1.1 400 x") as server:
        with pytest.raises(SdrError) as e:
            RtlTcpSource("127.0.0.1", server.port, timeout_s=1).open()
        assert e.value.reason == "not-receiver"
    with RtlTcpServer(lambda c, r: band(c, r, seconds=None), realtime=False, cut_after_s=0.5) as server:
        src = RtlTcpSource("127.0.0.1", server.port, CENTRE, RATE, timeout_s=2).open()
        with pytest.raises(SdrError) as e:
            while True:
                src.read()
        assert e.value.reason == "disconnected"
        src.close()


def test_a_server_that_goes_silent_is_a_read_failure_not_a_hang():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    held = []

    def serve():
        conn, _ = sock.accept()
        conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
        held.append(conn)                       # and then nothing

    threading.Thread(target=serve, daemon=True).start()
    src = RtlTcpSource("127.0.0.1", sock.getsockname()[1], timeout_s=0.5).open()
    with pytest.raises(SdrError) as e:
        src.read()
    assert e.value.reason == "read-failed"
    src.close()
    sock.close()
