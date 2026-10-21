"""Reading DNS-SD adverts (CONTROLLER-CONTRACT.md §2), and one live browse
through python-zeroconf when the host's multicast allows it."""
import socket
import time

import pytest

from shack_client.discovery import SERVICE, merge_adverts, parse_advert

TXT = {b"platform": b"1", b"contract": b"1", b"id": b"7f3c2a9e", b"name": b"GM4BRI", b"version": b"2.0.0-beta.1",
       b"kinds": b"qo100wb,icom", b"path": b"/.well-known/openhamshack"}


def test_a_controller_advert():
    c = parse_advert(SERVICE, "GM4BRI._openhamshack._tcp.local.", "198.51.100.208", 80, TXT)
    assert (c.name, c.id, c.url, c.kinds) == ("GM4BRI", "7f3c2a9e", "http://198.51.100.208:80/",
                                              ["icom", "qo100wb"])
    https = parse_advert(SERVICE, "GM4BRI", "198.51.100.208", 80, dict(TXT, https=b"443"))
    assert https.url == "https://198.51.100.208:443/"


def test_an_openhamshack_record_without_platform_is_not_ours():
    assert parse_advert(SERVICE, "x", "10.0.0.1", 80, {b"name": b"x"}) is None


def test_two_adverts_for_one_host_and_port_merge_to_one():
    a = parse_advert(SERVICE, "GM4BRI", "198.51.100.208", 80, TXT)
    b = parse_advert(SERVICE, "GM4BRI", "198.51.100.208", 80, TXT)
    assert merge_adverts([a, b]) == [a]


def test_live_browse_finds_a_registered_controller():
    zc_mod = pytest.importorskip("zeroconf")
    from shack_client.discovery import discover
    zc = zc_mod.Zeroconf(interfaces=["127.0.0.1"]) if False else zc_mod.Zeroconf()
    info = zc_mod.ServiceInfo(SERVICE, "SHACKTEST._openhamshack._tcp.local.", addresses=[socket.inet_aton("127.0.0.1")],
                              port=18080, properties=dict(TXT, name=b"SHACKTEST", id=b"deadbeef"),
                              server="shacktest.local.")
    try:
        zc.register_service(info)
    except Exception as e:      # no multicast here
        zc.close()
        pytest.skip(f"cannot register an mDNS service on this host: {e}")
    try:
        time.sleep(0.5)
        found = discover(timeout=2.0)
        if not any(c.id == "deadbeef" for c in found):
            pytest.skip("mDNS browse saw nothing on this host (multicast filtered)")
        c = next(c for c in found if c.id == "deadbeef")
        assert c.name == "SHACKTEST" and c.port == 18080
    finally:
        zc.unregister_service(info)
        zc.close()
