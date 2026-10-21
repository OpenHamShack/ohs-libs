"""The librtlsdr binding through a stand-in librtlsdr built from C at test
time: the same calls, argument types and buffers a dongle would see."""

from __future__ import annotations

import numpy as np
import pytest
from sdrhelp import CENTRE, RATE, WANT, StubControl, band, decode, stub_librtlsdr, texts

from shack_packet.sdr import RtlSdrSource, SdrError
from shack_packet.sdr import rtlsdr as rtlmod
from shack_packet.sdr.source import complex_to_cu8, cu8_to_complex


@pytest.fixture
def stub(tmp_path):
    path = stub_librtlsdr(tmp_path)
    return path, StubControl(path)


def test_rtl_sdr_opens_reads_and_closes_through_the_c_library(stub):
    path, ctl = stub
    iq = band(seconds=3.0)
    with iq:
        ctl.set_iq(b"".join(complex_to_cu8(b.samples) for b in iter(iq.read, None)))
    src = RtlSdrSource(CENTRE, RATE, lib_path=path)
    pipe = decode(src, limit_s=3.0)
    assert texts(pipe) == WANT
    log = ctl.log()
    names = [n for n, _ in log]
    assert names[:2] == ["open", "rate"] and ("freq", int(CENTRE)) in log
    assert ("gain_mode", 0) in log and ("agc", 0) in log and "reset" in names and names[-1] == "close"
    assert "ppm" not in names and "bias_tee" not in names
    assert src.gains[:3] == [0.0, 0.9, 1.4]


def test_rtl_sdr_manual_gain_takes_the_nearest_step_and_samples_are_full_scale(stub):
    path, ctl = stub
    ctl.set_iq(bytes([255, 0, 128, 127]))
    with RtlSdrSource(CENTRE, RATE, gain=30.0, lib_path=path, read_bytes=1 << 14) as src:
        z = src.read().samples
    assert ("gain_mode", 1) in ctl.log() and ("gain", 297) in ctl.log()
    assert np.allclose(z[:2], cu8_to_complex(bytes([255, 0, 128, 127])), atol=1e-6)
    assert abs(z[0].real - 1.0) < 1e-6 and abs(z[0].imag + 1.0) < 1e-6


def test_rtl_sdr_failures_have_reasons(stub, monkeypatch):
    path, ctl = stub
    ctl.lib.stub_set_open_result(-6)               # LIBUSB_ERROR_BUSY: the kernel's DVB driver, or another program
    with pytest.raises(SdrError) as e:
        RtlSdrSource(lib_path=path).open()
    assert e.value.reason == "busy"
    ctl.lib.stub_set_open_result(0)
    ctl.lib.stub_set_count(0)
    with pytest.raises(SdrError) as e:
        RtlSdrSource(lib_path=path).open()
    assert e.value.reason == "no-device"
    ctl.lib.stub_set_count(2)
    src = RtlSdrSource(lib_path=path, serial="00000002").open()
    assert ("open", 1) in ctl.log()
    ctl.lib.stub_set_read_result(-4)               # unplugged
    with pytest.raises(SdrError) as e:
        src.read()
    assert e.value.reason == "disconnected"
    src.close()
    monkeypatch.setattr(rtlmod, "_candidates", lambda *a: ["/nonexistent/librtlsdr.so"])
    with pytest.raises(SdrError) as e:
        RtlSdrSource().open()
    assert e.value.reason == "no-library"
