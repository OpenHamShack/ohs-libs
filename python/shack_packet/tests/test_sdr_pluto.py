"""The Pluto's receiver through a stand-in libiio, and its transmitter held
quiet whatever happens: quieted before anything else, read back, never
written otherwise, quieted again at close."""

from __future__ import annotations

import numpy as np
import pytest
from sdrhelp import CENTRE, RATE, WANT, FakePluto, band, decode, texts

from shack_packet.errors import PacketError
from shack_packet.sdr import PlutoSource, SdrError
from shack_packet.sdr.pluto import TX_ATTEN_MAX


def test_the_pluto_is_quiet_before_anything_else_and_nothing_else_touches_its_transmitter():
    radio = FakePluto()                             # as Tezuka firmware boots it: TX LO up, -10 dB
    src = PlutoSource("ip:192.168.2.1", CENTRE, RATE, iio_module=radio.module())
    src.open()
    assert radio.writes[:2] == [("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", str(TX_ATTEN_MAX))]
    assert radio.attr("tx_lo", "powerdown") == "1" and radio.attr("tx", "hardwaregain").startswith("-89.75")
    assert src.tx_quiet and src.status()["txQuiet"]
    assert radio.attr("rx_lo", "frequency") == str(int(CENTRE))
    assert radio.attr("rx", "sampling_frequency") == str(RATE)
    # The transmit side's DMA device is never asked for, no TX channel is
    # enabled, and the only buffer is the receiver's.
    assert "cf-ad9361-dds-core-lpc" not in radio.devices_asked
    assert all(dev == "cf-ad9361-lpc" and not output for dev, _, output, _ in radio.enabled)
    assert [b[0] for b in radio.buffers] == ["cf-ad9361-lpc"]
    src.set_gain(40.0)
    src.set_centre(144_900_000)
    src.close()
    tx = radio.tx_writes()
    assert set(tx) == {("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", str(TX_ATTEN_MAX))}
    assert tx[-2:] == [("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", str(TX_ATTEN_MAX))]   # again at close


def test_the_pluto_puts_back_the_receive_settings_it_found():
    radio = FakePluto()
    before = {k: radio.attr("rx", k) for k in ("sampling_frequency", "rf_bandwidth", "gain_control_mode")}
    lo = radio.attr("rx_lo", "frequency")
    with PlutoSource(centre=CENTRE, gain=20.0, iio_module=radio.module()):
        assert radio.attr("rx", "gain_control_mode") == "manual"
    assert {k: radio.attr("rx", k) for k in before} == before
    assert radio.attr("rx_lo", "frequency") == lo


def test_a_pluto_whose_transmitter_will_not_read_quiet_is_not_used():
    radio = FakePluto()
    radio.ignore.add(("tx_lo", "powerdown"))          # the write "works" and the LO stays up
    with pytest.raises(SdrError) as e:
        PlutoSource(centre=CENTRE, iio_module=radio.module()).open()
    assert e.value.reason == "tx-not-quiet"
    assert not [w for w in radio.writes if w[0] in ("rx", "rx_lo")]       # no receive setting was made
    assert radio.buffers == []


def test_a_stock_pluto_on_2m_is_refused_with_the_reason_after_quieting_it():
    radio = FakePluto(identity="ad9363")
    with pytest.raises(SdrError) as e:
        PlutoSource(centre=CENTRE, iio_module=radio.module()).open()
    assert e.value.reason == "out-of-range" and "AD9364" in e.value.detail
    assert radio.writes[0] == ("tx_lo", "powerdown", "1")
    assert not [w for w in radio.writes if w[0] in ("rx", "rx_lo")]


def test_below_the_fir_free_rate_the_pluto_is_not_even_opened():
    radio = FakePluto()
    with pytest.raises(SdrError) as e:
        PlutoSource(rate=1_200_000, iio_module=radio.module()).open()
    assert e.value.reason == "rate-unsupported" and radio.writes == [] and radio.devices_asked == []
    with pytest.raises(SdrError) as e:
        PlutoSource("ip:10.255.0.1", iio_module=radio.module()).open()
    assert e.value.reason == "no-device"


def test_the_pluto_gate_refuses_every_transmitter_write_but_quiet():
    radio = FakePluto()
    src = PlutoSource(centre=CENTRE, iio_module=radio.module()).open()
    n = len(radio.writes)
    for chan, name, value in (("tx", "hardwaregain", 0), ("tx", "hardwaregain", -10.0), ("tx_lo", "powerdown", 0),
                              ("tx_lo", "frequency", 2_400_000_000), ("tx", "sampling_frequency", RATE),
                              ("tx", "rf_bandwidth", RATE), ("rx", "filter_fir_config", "RX 3")):
        with pytest.raises(PacketError):
            src._write(chan, name, value)
    assert len(radio.writes) == n
    src.close()


def test_pluto_samples_arrive_at_full_scale_and_decode():
    radio = FakePluto()
    iq = band(seconds=3.0)
    with iq:
        z = np.concatenate([b.samples for b in iter(iq.read, None)])
    s = np.empty(2 * len(z), np.int16)
    s[0::2], s[1::2] = np.rint(z.real * 2048), np.rint(z.imag * 2048)
    radio.iq = s
    pipe = decode(PlutoSource(centre=CENTRE, iio_module=radio.module()), limit_s=3.0)
    assert texts(pipe) == WANT
