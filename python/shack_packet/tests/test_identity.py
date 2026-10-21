"""The station's identity and transmit switch."""

from __future__ import annotations

import pytest

from shack_packet.identity import IdentityError, Station, is_callsign, station_address


@pytest.mark.parametrize("call", ["G0ABC", "GM4BRI", "2E0TST", "M0A", "3DA0XX", "K1A", "VK2ABC", "mm0xyz"])
def test_issued_callsigns_identify(call):
    assert is_callsign(call)


@pytest.mark.parametrize("call", ["APRS", "WIDE1", "APDW17", "NOCALL", "N0CALL", "TEST", "GM4BRIX", "", "G0-AB"])
def test_aliases_destinations_and_placeholders_do_not(call):
    assert not is_callsign(call)


def test_station_address_keeps_the_ssid_and_refuses_a_bad_one():
    assert str(station_address("gm4bri-7")) == "GM4BRI-7"
    with pytest.raises(IdentityError):
        station_address("GM4BRI-16")
    with pytest.raises(IdentityError):
        station_address("WIDE2-2")


def test_nothing_may_be_sent_without_a_callsign_or_with_transmit_off():
    station = Station()
    assert station.refusal() == "no-callsign"
    station.set_callsign("GM4BRI")
    assert station.refusal() == "transmit-disabled"
    station.tx_enabled = True
    assert station.refusal() is None
    assert station.owns(station_address("GM4BRI-3")) and not station.owns(station_address("G0ABC"))
    assert str(station.address(1)) == "GM4BRI-1"
