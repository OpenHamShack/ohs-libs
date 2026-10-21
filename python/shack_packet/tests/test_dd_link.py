"""DdLink: AXUDP between two engines on 127.0.0.1, standing in for two PCs
on either side of a pair of IC-9700s in DD; the station's arming gating
every datagram; nothing taken from any address but the peer's; and the
radio's DD set-up on FakeIcom, put back on close. No radio is touched."""

from __future__ import annotations

import socket

import pytest

from radiohelp import ui, until
from test_icom_link import fake  # noqa: F401 - the fixture
from shack_packet import Engine, Runner, Station, hdlc
from shack_packet.radios import LinkRefused
from shack_packet.radios.dd import DdLink, decode, encode


def _ports(n=2):
    out = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        out.append(s.getsockname()[1])
        s.close()
    return out


def test_axudp_is_the_frame_and_its_fcs():
    frame = ui().encode()
    assert encode(frame) == hdlc.with_fcs(frame) and decode(encode(frame)) == frame
    assert decode(encode(frame)[:-1] + b"\x00") is None and decode(b"\x01\x02") is None


def test_two_stations_exchange_frames_and_a_disarmed_one_sends_none():
    pa, pb = _ports()
    a_station = Station.of("GM4BRI", tx_enabled=True)
    b_station = Station.of("GM0OTB", tx_enabled=False)
    a = DdLink(local=f"127.0.0.1:{pa}", peer=f"127.0.0.1:{pb}", frequency=1_299_525_000)
    b = DdLink(local=f"127.0.0.1:{pb}", peer=f"127.0.0.1:{pa}", frequency=1_299_525_000)
    ea, eb = Engine(a_station, a), Engine(b_station, b)
    heard = []
    eb.monitors.append(lambda f: heard.append(f.monitor()))
    with a, b:
        ra, rb = Runner(ea).start(), Runner(eb).start()
        try:
            ea.send_frame(ui())
            assert until(lambda: heard == [ui().monitor()], 5)
            assert a.stats["sent"] == 1 and b.stats["received"] == 1
            eb.send_frame(ui("GM0OTB>APRS:>disarmed"))
            # The channel refuses before keying; the link would refuse too
            # (below), so nothing goes either way.
            assert not until(lambda: b.stats["sent"] or a.stats["received"], 1.0), \
                "a disarmed station sends nothing"
            with pytest.raises(LinkRefused) as e:
                b.key()
            assert e.value.reason == b_station.refusal()
        finally:
            ra.stop()
            rb.stop()
    with pytest.raises(LinkRefused):
        a.send_frames([ui().encode()])


def test_only_the_peer_is_heard():
    pa, pb, stranger_port = _ports(3)
    link = DdLink(local=f"127.0.0.1:{pa}", peer=f"127.0.0.1:{pb}", frequency=1_299_525_000)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stranger.bind(("127.0.0.1", stranger_port))
        stranger.sendto(encode(ui().encode()), ("127.0.0.1", pa))
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", pb))
        peer.sendto(encode(ui().encode()), ("127.0.0.1", pa))
        peer.sendto(b"\x00" * 20, ("127.0.0.1", pa))
        assert until(lambda: link.stats["received"] == 1 and link.stats["bad_fcs"] == 1, 3)
        stranger.close()
        peer.close()
    assert link.stats["received"] == 1, "the stranger's datagram never reached the link"


def test_off_plan_channels_and_a_wildcard_address_are_refused():
    with pytest.raises(LinkRefused) as e:
        DdLink(local="127.0.0.1", peer="127.0.0.2", frequency=1_298_500_000).open()
    assert e.value.reason == "dd-channel"
    with pytest.raises(LinkRefused) as e:
        DdLink(local="0.0.0.0", peer="127.0.0.2").open()
    assert e.value.reason == "dd-address"


def test_the_radio_goes_to_dd_and_comes_back(fake, tmp_path):
    radio = fake.radio
    radio.freq, radio.mode, radio.data, radio.filter = 145_825_000, 0x05, 0x01, 0x01
    pa, pb = _ports()
    link = DdLink(local=f"127.0.0.1:{pa}", peer=f"127.0.0.1:{pb}", profile=fake.profile(), password="secret",
                  frequency=1_299_375_000, setup_dir=tmp_path)
    Engine(Station.of("GM4BRI", tx_enabled=True), link)
    with link:
        assert (radio.freq, radio.mode, radio.duplex) == (1_299_375_000, 0x22, 0x10)
        assert link.setup["mode"] and list(tmp_path.iterdir())
    assert (radio.freq, radio.mode, radio.data) == (145_825_000, 0x05, 0x01)
    assert not list(tmp_path.iterdir()) and radio.menu_writes == [] and radio.ptt_commands == []


def test_a_radio_that_refuses_dd_refuses_the_link_and_is_put_back(fake, tmp_path):
    radio = fake.radio
    radio.DD_BAND = (0, 1)                      # this pretend radio refuses DD everywhere
    pa, pb = _ports()
    link = DdLink(local=f"127.0.0.1:{pa}", peer=f"127.0.0.1:{pb}", profile=fake.profile(), password="secret",
                  setup_dir=tmp_path)
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "dd-refused" and "mode" in e.value.detail
    assert radio.freq == 144_174_000 and not link.is_open


def test_a_wrong_password_is_said_as_such(fake):
    pa, pb = _ports()
    link = DdLink(local=f"127.0.0.1:{pa}", peer=f"127.0.0.1:{pb}", profile=fake.profile(), password="wrong",
                  connect_s=10)
    with pytest.raises(LinkRefused) as e:
        link.open()
    assert e.value.reason == "radio-auth" and not link.is_open
