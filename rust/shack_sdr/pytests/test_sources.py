"""The receive-only sources moved from shack_packet.sdr (RADIO-SDR-PLAN §6),
through their real ctypes bindings against stand-in libraries built from C
at test time, as Packet's own tests do: librtlsdr with the same exported
functions, and SDRplay's API compiled against SDRplay's own headers (where
they are installed), so a struct field this module writes wrongly reads
back wrong. No real radio and no real library is ever opened here.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from shack_sdr.sources import RtlSdrSource, SdrError, SdrplaySource, complex_to_cu8, cu8_to_complex, rtlsdr, sdrplay

INCLUDE = Path(os.environ.get("SHACK_SDRPLAY_INCLUDE", "/usr/local/include"))

STUB_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static char logbuf[65536];
static size_t loglen = 0;
static unsigned char *iq = 0;
static int iqlen = 0, iqpos = 0;
static int count = 1, open_result = 0, read_result = 0, short_by = 0;
static int dev_token = 42;
static const int gains[] = {0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328,
                            338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496};

static void logf_(const char *name, long v) {
    int n = snprintf(logbuf + loglen, sizeof logbuf - loglen, "%s %ld\n", name, v);
    if (n > 0 && loglen + n < sizeof logbuf) loglen += n;
}

const char *stub_log(void) { logbuf[loglen] = 0; return logbuf; }
void stub_reset(void) { loglen = 0; iqpos = 0; count = 1; open_result = 0; read_result = 0; short_by = 0; }
void stub_set_iq(const unsigned char *data, int len) {
    free(iq); iq = malloc(len); memcpy(iq, data, len); iqlen = len; iqpos = 0;
}
void stub_set_count(int n) { count = n; }
void stub_set_open_result(int r) { open_result = r; }
void stub_set_read_result(int r) { read_result = r; }

uint32_t rtlsdr_get_device_count(void) { return count; }
const char *rtlsdr_get_device_name(uint32_t i) { return "Generic RTL2832U OEM"; }
int rtlsdr_get_device_usb_strings(uint32_t i, char *m, char *p, char *s) {
    strcpy(m, "Realtek"); strcpy(p, "RTL2838UHIDIR"); sprintf(s, "0000000%u", i + 1); return 0;
}
int rtlsdr_open(void **dev, uint32_t index) {
    logf_("open", index);
    if (open_result < 0) return open_result;
    *dev = &dev_token; return 0;
}
int rtlsdr_close(void *dev) { logf_("close", dev == &dev_token); return 0; }
static uint32_t freq = 0, rate = 0;
int rtlsdr_set_center_freq(void *dev, uint32_t f) { logf_("freq", f); freq = f; return 0; }
uint32_t rtlsdr_get_center_freq(void *dev) { return freq; }
int rtlsdr_set_freq_correction(void *dev, int ppm) { logf_("ppm", ppm); return 0; }
int rtlsdr_set_sample_rate(void *dev, uint32_t r) { logf_("rate", r); rate = r; return 0; }
uint32_t rtlsdr_get_sample_rate(void *dev) { return rate; }
int rtlsdr_get_tuner_gains(void *dev, int *g) {
    int n = sizeof gains / sizeof gains[0];
    if (g) memcpy(g, gains, sizeof gains);
    return n;
}
int rtlsdr_set_tuner_gain_mode(void *dev, int manual) { logf_("gain_mode", manual); return 0; }
int rtlsdr_set_tuner_gain(void *dev, int g) { logf_("gain", g); return 0; }
int rtlsdr_set_agc_mode(void *dev, int on) { logf_("agc", on); return 0; }
int rtlsdr_reset_buffer(void *dev) { logf_("reset", 0); return 0; }
int rtlsdr_set_bias_tee(void *dev, int on) { logf_("bias_tee", on); return 0; }
int rtlsdr_read_sync(void *dev, void *buf, int len, int *n_read) {
    if (read_result < 0) { *n_read = 0; return read_result; }
    unsigned char *b = buf;
    for (int i = 0; i < len; i++) { b[i] = iqlen ? iq[iqpos] : 127; if (iqlen) iqpos = (iqpos + 1) % iqlen; }
    *n_read = len;
    return 0;
}
"""

STUB = r"""
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <pthread.h>
#include <time.h>
#include "sdrplay_api.h"

static sdrplay_api_DevParamsT dev;
static sdrplay_api_RxChannelParamsT chA;
static sdrplay_api_DeviceParamsT params = {&dev, &chA, 0};
static sdrplay_api_CallbackFnsT cbs;
static short *iq_i, *iq_q; static long iqn, iqpos;
static int running, opened, locked, selected, open_err, ndev = 1;
static pthread_t th;
static int token = 5;

void stub_set_iq(const short *i, const short *q, long n) {
    free(iq_i); free(iq_q); iq_i = malloc(n * 2); iq_q = malloc(n * 2);
    memcpy(iq_i, i, n * 2); memcpy(iq_q, q, n * 2); iqn = n; iqpos = 0;
}
void stub_set_open_err(int e) { open_err = e; }
void stub_set_ndev(int n) { ndev = n; }
double stub_rf(void) { return chA.tunerParams.rfFreq.rfHz; }
double stub_fs(void) { return dev.fsFreq.fsHz; }
int stub_bw(void) { return chA.tunerParams.bwType; }
int stub_if(void) { return chA.tunerParams.ifType; }
int stub_agc(void) { return chA.ctrlParams.agc.enable; }
int stub_grdb(void) { return chA.tunerParams.gain.gRdB; }
int stub_lna(void) { return chA.tunerParams.gain.LNAstate; }
int stub_dec(void) { return chA.ctrlParams.decimation.enable; }
int stub_state(void) { return opened * 100 + selected * 10 + running; }
void stub_remove(void) { if (cbs.EventCbFn) cbs.EventCbFn(sdrplay_api_DeviceRemoved, sdrplay_api_Tuner_A, 0, 0); }

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

/* Packets of 1344 samples, as an RSP's USB stream gives them, paced by the
   clock: as many as real time says are due, then a short sleep. */
static void *pump(void *a) {
    enum { N = 1344 };
    short bi[N], bq[N];
    struct timespec ts = {0, 5000000};
    sdrplay_api_StreamCbParamsT p; memset(&p, 0, sizeof p);
    double t0 = now_s(); long sent = 0;
    while (running) {
        while (running && sent < (now_s() - t0) * dev.fsFreq.fsHz) {
            for (int k = 0; k < N; k++) { bi[k] = iqn ? iq_i[iqpos] : 0; bq[k] = iqn ? iq_q[iqpos] : 0; if (iqn) iqpos = (iqpos + 1) % iqn; }
            cbs.StreamACbFn(bi, bq, &p, N, 0, 0);
            sent += N;
        }
        nanosleep(&ts, 0);
    }
    return 0;
}

sdrplay_api_ErrT sdrplay_api_Open(void) { if (open_err) return open_err; opened = 1; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_Close(void) { opened = 0; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_ApiVersion(float *v) { *v = SDRPLAY_API_VERSION; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_LockDeviceApi(void) { locked = 1; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_UnlockDeviceApi(void) { locked = 0; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_GetDevices(sdrplay_api_DeviceT *d, unsigned int *n, unsigned int max) {
    *n = ndev;
    for (int k = 0; k < ndev; k++) { memset(&d[k], 0, sizeof d[k]); snprintf(d[k].SerNo, 64, "240309F0%d", k); d[k].hwVer = SDRPLAY_RSPdx_ID; d[k].valid = 1; d[k].dev = &token; }
    return sdrplay_api_Success;
}
sdrplay_api_ErrT sdrplay_api_SelectDevice(sdrplay_api_DeviceT *d) { if (!locked || d->tuner != sdrplay_api_Tuner_A) return sdrplay_api_Fail; selected = 1;
    memset(&dev, 0, sizeof dev); memset(&chA, 0, sizeof chA); dev.fsFreq.fsHz = 2e6; chA.tunerParams.gain.gRdB = 50; chA.ctrlParams.agc.enable = sdrplay_api_AGC_50HZ; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_ReleaseDevice(sdrplay_api_DeviceT *d) { selected = 0; return sdrplay_api_Success; }
const char *sdrplay_api_GetErrorString(sdrplay_api_ErrT e) { return "stub error"; }
sdrplay_api_ErrT sdrplay_api_GetDeviceParams(HANDLE h, sdrplay_api_DeviceParamsT **p) { *p = &params; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_Init(HANDLE h, sdrplay_api_CallbackFnsT *c, void *ctx) { cbs = *c; running = 1; pthread_create(&th, 0, pump, 0); return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_Uninit(HANDLE h) { if (running) { running = 0; pthread_join(th, 0); } return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_Update(HANDLE h, sdrplay_api_TunerSelectT t, sdrplay_api_ReasonForUpdateT r, sdrplay_api_ReasonForUpdateExtension1T e) { return sdrplay_api_Success; }
"""


def cc():
    c = shutil.which("cc") or shutil.which("gcc")
    if not c:
        pytest.skip("no C compiler for the stand-in libraries")
    return c


@pytest.fixture
def rtl(tmp_path):
    (tmp_path / "stub_rtlsdr.c").write_text(STUB_C)
    lib = tmp_path / "librtlsdr_stub.so"
    subprocess.run([cc(), "-shared", "-fPIC", "-O1", "-o", str(lib), str(tmp_path / "stub_rtlsdr.c")], check=True)
    ctl = ctypes.CDLL(str(lib))
    ctl.stub_log.restype = ctypes.c_char_p
    ctl.stub_set_iq.argtypes = (ctypes.c_char_p, ctypes.c_int)
    ctl.stub_reset()
    return lib, ctl


def log(ctl):
    return [(n, int(v)) for n, v in (line.split() for line in ctl.stub_log().decode().splitlines())]


def test_rtl_sdr_opens_reads_and_closes_through_the_c_library(rtl):
    lib, ctl = rtl
    tone = np.exp(2j * np.pi * 0.01 * np.arange(4096)).astype(np.complex64) * 0.5
    ctl.stub_set_iq(complex_to_cu8(tone), 2 * len(tone))
    with RtlSdrSource(145_000_000, 2_400_000, lib_path=lib, read_bytes=8192) as src:
        b = src.read()
    assert b.samples.dtype == np.complex64 and len(b.samples) == 4096 and b.index == 0
    assert np.allclose(b.samples, cu8_to_complex(complex_to_cu8(tone)), atol=1e-6)
    names = [n for n, _ in log(ctl)]
    assert names[:2] == ["open", "rate"] and ("freq", 145_000_000) in log(ctl)
    assert ("gain_mode", 0) in log(ctl) and ("agc", 0) in log(ctl) and names[-1] == "close"
    assert "bias_tee" not in names and "ppm" not in names          # never touched
    assert src.gains[:3] == [0.0, 0.9, 1.4]


def test_rtl_sdr_gain_failures_rates_and_availability(rtl, monkeypatch):
    lib, ctl = rtl
    with RtlSdrSource(gain=30.0, lib_path=lib, read_bytes=1 << 12):
        pass
    assert ("gain_mode", 1) in log(ctl) and ("gain", 297) in log(ctl)
    ctl.stub_set_open_result(-6)
    with pytest.raises(SdrError) as e:
        RtlSdrSource(lib_path=lib).open()
    assert e.value.reason == "busy"
    ctl.stub_set_open_result(0)
    ctl.stub_set_count(0)
    with pytest.raises(SdrError) as e:
        RtlSdrSource(lib_path=lib).open()
    assert e.value.reason == "no-device"
    assert RtlSdrSource.rates() == [1_200_000.0, 2_400_000.0]
    assert rtlsdr.availability(lib_path=lib)["available"]
    monkeypatch.setattr(rtlsdr, "_candidates", lambda *a: ["/nonexistent/librtlsdr.so"])
    assert rtlsdr.availability() == {"available": False, "why": "no-library", "detail": rtlsdr.availability()["detail"]}


@pytest.fixture(scope="module")
def rsp(tmp_path_factory):
    if not (INCLUDE / "sdrplay_api.h").exists():
        pytest.skip("SDRplay's API headers are not installed here")
    d = tmp_path_factory.mktemp("sdrplay")
    (d / "stub.c").write_text(STUB)
    lib = d / "libsdrplay_api_stub.so"
    subprocess.run([cc(), "-shared", "-fPIC", "-O1", "-pthread", f"-I{INCLUDE}", "-o", str(lib), str(d / "stub.c")],
                   check=True)
    ctl = ctypes.CDLL(str(lib))
    ctl.stub_rf.restype = ctl.stub_fs.restype = ctypes.c_double
    return lib, ctl


def test_the_rsp_fields_written_are_the_fields_the_headers_name(rsp):
    lib, ctl = rsp
    src = SdrplaySource(144_700_000, 2_400_000, lib_path=lib, lna_state=4)
    with src:
        assert ctl.stub_state() == 111
        assert ctl.stub_rf() == 144_700_000 and ctl.stub_fs() == 2_400_000
        assert ctl.stub_bw() == 1536 and ctl.stub_if() == 0 and ctl.stub_dec() == 0
        assert ctl.stub_agc() == 2 and ctl.stub_lna() == 4
        src.set_gain(35)
        assert ctl.stub_agc() == 0 and ctl.stub_grdb() == 35
        assert src.read().samples.dtype == np.complex64
    assert ctl.stub_state() == 0
    assert SdrplaySource.rates() == [2_400_000.0]


def test_an_unplugged_rsp_is_disconnected(rsp):
    lib, ctl = rsp
    with SdrplaySource(lib_path=lib) as src:
        ctl.stub_remove()
        with pytest.raises(SdrError) as e:
            for _ in range(500):
                src.read()
        assert e.value.reason == "disconnected"


def no_system_api(monkeypatch):
    monkeypatch.setattr(sdrplay, "DEFAULT_PLACES", [])
    monkeypatch.setattr(sdrplay, "LIB_PLACES", {})
    monkeypatch.setattr(sdrplay.ctypes.util, "find_library", lambda name: None)
    monkeypatch.delenv("SHACK_SDRPLAY_LIB", raising=False)
    monkeypatch.delenv("SHACK_HOSTLIBS", raising=False)


def test_without_the_api_it_says_install_it(monkeypatch):
    no_system_api(monkeypatch)
    a = sdrplay.availability()
    assert a["available"] is False and a["why"] == "sdrplay_api_missing" and a["host"] is False
    monkeypatch.setenv("SHACK_HOSTLIBS", "/opt/hostlibs-nonexistent")
    a = sdrplay.availability()
    assert a["why"] == "sdrplay_api_missing" and a["host"] is True        # "ask your administrator"
    assert a["places"][0] == "/opt/hostlibs-nonexistent/sdrplay-api/libsdrplay_api.so.3"
    with pytest.raises(SdrError) as e:
        SdrplaySource().open()
    assert e.value.reason == "no-library"


def test_on_a_controller_the_host_library_is_used(rsp, tmp_path, monkeypatch):
    lib, ctl = rsp
    no_system_api(monkeypatch)
    staged = tmp_path / "hostlibs" / "sdrplay-api"
    staged.mkdir(parents=True)
    shutil.copy(lib, staged / "libsdrplay_api.so.3")
    monkeypatch.setenv("SHACK_HOSTLIBS", str(tmp_path / "hostlibs"))
    a = sdrplay.availability()
    assert a["available"] and a["host"]
