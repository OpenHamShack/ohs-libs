"""The SDRplay source (sdr/sdrplay.py) through its real ctypes binding,
against a stand-in API compiled here against SDRplay's OWN headers - so
every field this module writes is read back by C code through the header's
struct definitions, and a layout this module got wrong shows as a wrong
value. Skipped where SDRplay's headers are not installed (they come with
the API; shack has 3.15 in /usr/local/include) or there is no compiler.
And an SdrRxLink on it hears frames, as an RTL-SDR does."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from radiohelp import Sink, heard_frames, until

from shack_packet import Station, afsk
from shack_packet.ax25 import Frame
from shack_packet.radios import SdrRxLink
from shack_packet.radios.pluto import FmModulator
from shack_packet.sdr.sdrplay import SdrplaySource, devices, load
from shack_packet.sdr.source import SdrError

INCLUDE = Path(os.environ.get("SHACK_SDRPLAY_INCLUDE", "/usr/local/include"))

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


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    import ctypes
    if not (INCLUDE / "sdrplay_api.h").exists():
        pytest.skip("SDRplay's API headers are not installed here")
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler")
    d = tmp_path_factory.mktemp("sdrplay")
    (d / "stub.c").write_text(STUB)
    lib = d / "libsdrplay_api_stub.so"
    subprocess.run([cc, "-shared", "-fPIC", "-O1", "-pthread", f"-I{INCLUDE}", "-o", str(lib), str(d / "stub.c")],
                   check=True)
    ctl = ctypes.CDLL(str(lib))
    ctl.stub_rf.restype = ctl.stub_fs.restype = ctypes.c_double
    return lib, ctl


def test_the_fields_written_are_the_fields_the_headers_name(stub):
    lib, ctl = stub
    src = SdrplaySource(144_700_000, 2_400_000, lib_path=lib, lna_state=4)
    with src:
        assert ctl.stub_state() == 111                       # API open, device selected, streaming
        assert ctl.stub_rf() == 144_700_000 and ctl.stub_fs() == 2_400_000
        assert ctl.stub_bw() == 1536 and ctl.stub_if() == 0 and ctl.stub_dec() == 0
        assert ctl.stub_agc() == 2 and ctl.stub_lna() == 4    # AGC at 50 Hz, the LNA where asked
        src.set_gain(35)
        assert ctl.stub_agc() == 0 and ctl.stub_grdb() == 35
        src.set_centre(145_000_000)
        assert ctl.stub_rf() == 145_000_000
        assert src.api_version >= 3.0
        assert src.read().samples.dtype == np.complex64
    assert ctl.stub_state() == 0                             # uninit, released, closed


def test_frames_heard_through_an_rsp(stub):
    lib, ctl = stub
    pcm = afsk.pcm16(afsk.modulate([Frame.from_monitor(f"M0RSP>APZOHS:>rsp {i}").encode() for i in range(2)],
                                   12_000, amplitude=0.5, txdelay_ms=300))
    audio = np.frombuffer(pcm, "<i2").astype(np.float64) / 32768.0
    iq = FmModulator(2_400_000, 3_000.0, 100_000.0, amplitude=0.3)(np.concatenate([np.zeros(6000), audio,
                                                                                    np.zeros(6000)]))
    i = np.round(iq.real * 32767).astype(np.int16)
    q = np.round(iq.imag * 32767).astype(np.int16)
    ctl.stub_set_iq(i.ctypes.data, q.ctypes.data, len(i))
    link = SdrRxLink(SdrplaySource(rate=2_400_000, lib_path=lib), 144_800_000, kind="sdrplay")
    sink = Sink(Station.of("GM4BRI"))
    link.attach(sink)
    with link:
        assert until(lambda: len(set(heard_frames(sink.pcm, 12_000))) == 2, 15)
    ctl.stub_set_iq(None, None, 0)


def test_an_unplugged_rsp_is_disconnected(stub):
    lib, ctl = stub
    src = SdrplaySource(lib_path=lib)
    with src:
        ctl.stub_remove()
        with pytest.raises(SdrError) as e:
            for _ in range(500):
                src.read()
        assert e.value.reason == "disconnected"


def test_no_rsp_and_no_service_are_said_plainly(stub):
    lib, ctl = stub
    ctl.stub_set_ndev(0)
    try:
        with pytest.raises(SdrError) as e:
            SdrplaySource(lib_path=lib).open()
        assert e.value.reason == "no-device" and ctl.stub_state() == 0
    finally:
        ctl.stub_set_ndev(1)
    ctl.stub_set_open_err(1)
    try:
        with pytest.raises(SdrError) as e:
            SdrplaySource(lib_path=lib).open()
        assert e.value.reason == "open-failed"
        assert devices(lib) == []
    finally:
        ctl.stub_set_open_err(0)
    assert [d["serial"] for d in devices(lib)] == ["240309F00"]


def test_without_the_api_installed_it_says_install_it(tmp_path, monkeypatch):
    from shack_packet.sdr import sdrplay
    monkeypatch.setattr(sdrplay, "DEFAULT_PLACES", [])
    monkeypatch.setattr(sdrplay, "LIB_PLACES", {})
    monkeypatch.setattr(sdrplay.ctypes.util, "find_library", lambda name: None)
    monkeypatch.delenv("SHACK_SDRPLAY_LIB", raising=False)
    with pytest.raises(SdrError) as e:
        load(tmp_path / "nothing-here.so")
    assert e.value.reason == "no-library" and "never bundled" in e.value.detail
