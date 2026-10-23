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
static sdrplay_api_RxChannelParamsT chA, chB;
static sdrplay_api_DeviceParamsT params = {&dev, &chA, &chB};
static sdrplay_api_CallbackFnsT cbs;
static short *iq_i, *iq_q; static long iqn, iqpos;
static int running, opened, locked, selected, open_err, ndev = 1;
static int hwver = SDRPLAY_RSPdx_ID, cur_tuner = sdrplay_api_Tuner_A, init_count, update_count;
static pthread_t th;
static int token = 5;
static unsigned int skip_next;   /* samples the next packet's number jumps by (an API-side drop) */
static char logbuf[1 << 17];

/* SDRplay's own defaults (the headers' "default:" comments), which
   SelectDevice leaves in the structure: what a module that never writes a
   field gets, and so what SoapySDRPlay3 left in every field it never set. */
static void api_defaults(void) {
    memset(&dev, 0, sizeof dev); memset(&chA, 0, sizeof chA); memset(&chB, 0, sizeof chB);
    dev.fsFreq.fsHz = 2e6; dev.ppm = 0.0; dev.mode = sdrplay_api_ISOCH;
    dev.rspDxParams.antennaSel = sdrplay_api_RspDx_ANTENNA_A;
    sdrplay_api_RxChannelParamsT *chs[2] = {&chA, &chB};
    for (int k = 0; k < 2; k++) {
        sdrplay_api_RxChannelParamsT *c = chs[k];
        c->tunerParams.bwType = sdrplay_api_BW_0_200; c->tunerParams.ifType = sdrplay_api_IF_Zero;
        c->tunerParams.loMode = sdrplay_api_LO_Auto;
        c->tunerParams.gain.gRdB = 50; c->tunerParams.gain.LNAstate = 0;
        c->tunerParams.gain.minGr = sdrplay_api_NORMAL_MIN_GR;
        c->tunerParams.rfFreq.rfHz = 200000000.0;
        c->tunerParams.dcOffsetTuner.dcCal = 3; c->tunerParams.dcOffsetTuner.speedUp = 0;
        c->tunerParams.dcOffsetTuner.trackTime = 1; c->tunerParams.dcOffsetTuner.refreshRateTime = 2048;
        c->ctrlParams.dcOffset.DCenable = 1; c->ctrlParams.dcOffset.IQenable = 1;
        c->ctrlParams.decimation.enable = 0; c->ctrlParams.decimation.decimationFactor = 1;
        c->ctrlParams.decimation.wideBandSignal = 0;
        c->ctrlParams.agc.enable = sdrplay_api_AGC_50HZ; c->ctrlParams.agc.setPoint_dBfs = -60;
        c->ctrlParams.adsbMode = sdrplay_api_ADSB_DECIMATION;
        c->rsp2TunerParams.antennaSel = sdrplay_api_Rsp2_ANTENNA_A; c->rsp2TunerParams.amPortSel = sdrplay_api_Rsp2_AMPORT_2;
        c->rspDuoTunerParams.tuner1AmPortSel = sdrplay_api_RspDuo_AMPORT_2;
        c->rspDxTunerParams.hdrBw = sdrplay_api_RspDx_HDRMODE_BW_1_700;
    }
}

/* Every field a module could set at open or stream time, as the API holds
   it at this moment: one line per Init and per Update, so a test compares
   the whole structure, not the fields it thought to ask about. */
static void snap(const char *what, unsigned int reason, unsigned int ext) {
    sdrplay_api_RxChannelParamsT *c = cur_tuner == sdrplay_api_Tuner_B ? &chB : &chA;
    size_t at = strlen(logbuf);
    if (at > sizeof logbuf - 2048) return;
    snprintf(logbuf + at, sizeof logbuf - at,
        "%s reason=%u ext=%u tuner=%d fsHz=%.0f ppm=%g mode=%d rfHz=%.3f bwType=%d ifType=%d loMode=%d "
        "gRdB=%d LNAstate=%d minGr=%d dcCal=%d speedUp=%d trackTime=%d refreshRateTime=%d "
        "DCenable=%d IQenable=%d decEnable=%d decimationFactor=%d wideBandSignal=%d "
        "agcEnable=%d setPoint_dBfs=%d attack_ms=%d decay_ms=%d decay_delay_ms=%d decay_threshold_dB=%d "
        "adsbMode=%d hdrEnable=%d dxBiasTEnable=%d antennaSel=%d dxRfNotchEnable=%d dxRfDabNotchEnable=%d hdrBw=%d "
        "rsp1aBiasTEnable=%d rsp1aRfNotchEnable=%d rsp1aRfDabNotchEnable=%d rsp2BiasTEnable=%d rsp2RfNotchEnable=%d "
        "duoBiasTEnable=%d duoAmNotchEnable=%d duoRfNotchEnable=%d duoRfDabNotchEnable=%d\n",
        what, reason, ext, cur_tuner, dev.fsFreq.fsHz, dev.ppm, dev.mode, c->tunerParams.rfFreq.rfHz,
        c->tunerParams.bwType, c->tunerParams.ifType, c->tunerParams.loMode,
        c->tunerParams.gain.gRdB, c->tunerParams.gain.LNAstate, c->tunerParams.gain.minGr,
        c->tunerParams.dcOffsetTuner.dcCal, c->tunerParams.dcOffsetTuner.speedUp,
        c->tunerParams.dcOffsetTuner.trackTime, c->tunerParams.dcOffsetTuner.refreshRateTime,
        c->ctrlParams.dcOffset.DCenable, c->ctrlParams.dcOffset.IQenable, c->ctrlParams.decimation.enable,
        c->ctrlParams.decimation.decimationFactor, c->ctrlParams.decimation.wideBandSignal,
        c->ctrlParams.agc.enable, c->ctrlParams.agc.setPoint_dBfs, c->ctrlParams.agc.attack_ms,
        c->ctrlParams.agc.decay_ms, c->ctrlParams.agc.decay_delay_ms, c->ctrlParams.agc.decay_threshold_dB,
        c->ctrlParams.adsbMode, dev.rspDxParams.hdrEnable, dev.rspDxParams.biasTEnable, dev.rspDxParams.antennaSel,
        dev.rspDxParams.rfNotchEnable, dev.rspDxParams.rfDabNotchEnable, c->rspDxTunerParams.hdrBw,
        c->rsp1aTunerParams.biasTEnable, dev.rsp1aParams.rfNotchEnable, dev.rsp1aParams.rfDabNotchEnable,
        c->rsp2TunerParams.biasTEnable, c->rsp2TunerParams.rfNotchEnable,
        c->rspDuoTunerParams.biasTEnable, c->rspDuoTunerParams.tuner1AmNotchEnable,
        c->rspDuoTunerParams.rfNotchEnable, c->rspDuoTunerParams.rfDabNotchEnable);
}
const char *stub_log(void) { return logbuf; }
void stub_clear(void) { logbuf[0] = 0; }
void stub_skip(unsigned int n) { skip_next = n; }
void stub_overload(int kind) {
    sdrplay_api_EventParamsT ep; memset(&ep, 0, sizeof ep);
    ep.powerOverloadParams.powerOverloadChangeType = kind;
    if (cbs.EventCbFn) cbs.EventCbFn(sdrplay_api_PowerOverloadChange, cur_tuner, &ep, 0);
}

void stub_set_iq(const short *i, const short *q, long n) {
    free(iq_i); free(iq_q); iq_i = malloc(n * 2); iq_q = malloc(n * 2);
    memcpy(iq_i, i, n * 2); memcpy(iq_q, q, n * 2); iqn = n; iqpos = 0;
}
void stub_set_open_err(int e) { open_err = e; }
void stub_set_ndev(int n) { ndev = n; }
void stub_set_hwver(int v) { hwver = v; }
/* The channel the API currently streams: A, unless an RSPduo swap moved it
   to B (sdrplay_api_SwapRspDuoActiveTuner below). */
static sdrplay_api_RxChannelParamsT *active_ch(void) { return cur_tuner == sdrplay_api_Tuner_B ? &chB : &chA; }
double stub_rf(void) { return active_ch()->tunerParams.rfFreq.rfHz; }
double stub_fs(void) { return dev.fsFreq.fsHz; }
int stub_bw(void) { return active_ch()->tunerParams.bwType; }
int stub_if(void) { return active_ch()->tunerParams.ifType; }
int stub_agc(void) { return active_ch()->ctrlParams.agc.enable; }
int stub_grdb(void) { return active_ch()->tunerParams.gain.gRdB; }
int stub_lna(void) { return active_ch()->tunerParams.gain.LNAstate; }
int stub_dec(void) { return active_ch()->ctrlParams.decimation.enable; }
int stub_state(void) { return opened * 100 + selected * 10 + running; }
int stub_init_count(void) { return init_count; }
int stub_update_count(void) { return update_count; }
int stub_rsp2_antsel(void) { return active_ch()->rsp2TunerParams.antennaSel; }
int stub_rsp2_amport(void) { return active_ch()->rsp2TunerParams.amPortSel; }
int stub_rspdx_antsel(void) { return dev.rspDxParams.antennaSel; }
int stub_duo_amport(void) { return active_ch()->rspDuoTunerParams.tuner1AmPortSel; }
int stub_duo_tuner(void) { return cur_tuner; }
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
            /* Whichever tuner is active now gets the samples - real enough
               to prove an RSPduo tuner swap does not go deaf (both of
               SdrplaySource's callbacks run the same code, sdrplay.py
               docstring). */
            p.firstSampleNum += skip_next; skip_next = 0;
            p.numSamples = N;
            if (cur_tuner == sdrplay_api_Tuner_B) cbs.StreamBCbFn(bi, bq, &p, N, 0, 0);
            else cbs.StreamACbFn(bi, bq, &p, N, 0, 0);
            p.firstSampleNum += N;
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
    for (int k = 0; k < ndev; k++) { memset(&d[k], 0, sizeof d[k]); snprintf(d[k].SerNo, 64, "240309F0%d", k); d[k].hwVer = hwver; d[k].valid = 1; d[k].dev = &token; }
    return sdrplay_api_Success;
}
sdrplay_api_ErrT sdrplay_api_SelectDevice(sdrplay_api_DeviceT *d) {
    if (!locked || (d->tuner != sdrplay_api_Tuner_A && d->tuner != sdrplay_api_Tuner_B)) return sdrplay_api_Fail;
    selected = 1; cur_tuner = d->tuner;
    api_defaults();
    return sdrplay_api_Success;
}
sdrplay_api_ErrT sdrplay_api_ReleaseDevice(sdrplay_api_DeviceT *d) { selected = 0; return sdrplay_api_Success; }
const char *sdrplay_api_GetErrorString(sdrplay_api_ErrT e) { return "stub error"; }
sdrplay_api_ErrT sdrplay_api_GetDeviceParams(HANDLE h, sdrplay_api_DeviceParamsT **p) { *p = &params; return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_Init(HANDLE h, sdrplay_api_CallbackFnsT *c, void *ctx) {
    cbs = *c; running = 1; init_count++; snap("init", 0, 0); pthread_create(&th, 0, pump, 0);
    return sdrplay_api_Success;
}
sdrplay_api_ErrT sdrplay_api_Uninit(HANDLE h) { if (running) { running = 0; pthread_join(th, 0); } return sdrplay_api_Success; }
sdrplay_api_ErrT sdrplay_api_Update(HANDLE h, sdrplay_api_TunerSelectT t, sdrplay_api_ReasonForUpdateT r, sdrplay_api_ReasonForUpdateExtension1T e) {
    update_count++; snap("update", (unsigned int)r, (unsigned int)e); return sdrplay_api_Success;
}
sdrplay_api_ErrT sdrplay_api_SwapRspDuoActiveTuner(HANDLE h, sdrplay_api_TunerSelectT *tuner,
                                                    sdrplay_api_RspDuo_AmPortSelectT port) {
    cur_tuner = (cur_tuner == sdrplay_api_Tuner_A) ? sdrplay_api_Tuner_B : sdrplay_api_Tuner_A;
    *tuner = cur_tuner;
    return sdrplay_api_Success;
}
void stub_reset(void) {
    hwver = SDRPLAY_RSPdx_ID; cur_tuner = sdrplay_api_Tuner_A; init_count = update_count = 0; logbuf[0] = 0; skip_next = 0;
}
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
    ctl.stub_log.restype = ctypes.c_char_p
    return lib, ctl


@pytest.fixture
def sdr(rsp):
    """`rsp`'s compiled library, reset to a fresh RSPdx before each test:
    the stub is built once (module-scoped, because compiling is slow) and
    every test shares it, so state from one must not leak into the next."""
    lib, ctl = rsp
    ctl.stub_reset()
    return lib, ctl


def test_the_rsp_fields_written_are_the_fields_the_headers_name(sdr):
    lib, ctl = sdr
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


def snaps(ctl) -> list[dict]:
    """The stand-in API's record: one dict per Init and per Update, every
    field as the API held it then."""
    out = []
    for line in ctl.stub_log().decode().splitlines():
        what, *kv = line.split()
        out.append({"what": what, **{k: float(v) for k, v in (x.split("=") for x in kv)}})
    return out


#: What SoapySDRPlay3 0.5.2 leaves in SDRplay's structure when an RSPdx
#: starts streaming for Radio's ohs-box station (serial and rfgain_sel=4
#: its only arguments; Antenna A, 2.4 MS/s, manual gain 40, 145.65 MHz):
#: the README's "SDRplay: parity with SoapySDRPlay3" table, field by field,
#: with SoapySDRPlay3's lines. The keys are every field the stand-in
#: records, so a field recorded later must be decided here, not missed.
SOAPYSDRPLAY3_RSPDX_INIT = {
    "tuner": 1,                         # GetDevices' own, not set for a non-duo (Settings.cpp 2100-2105)
    "fsHz": 2_400_000,                  # setSampleRate: above 2 MS/s, fs = rate (Settings.cpp 1019-1022, 807-809)
    "ppm": 0,                           # only setFrequencyCorrection writes it (Settings.cpp 701-704); never called
    "mode": 0,                          # ISOCH: BULK only when built STREAMING_USB_MODE_BULK (Streaming.cpp 368-371)
    "rfHz": 145_650_000,                # (uint32_t)frequency (Settings.cpp 674-676); we tune before Init, it after
    "bwType": 1536,                     # getBwEnumForRate(2.4e6) (Settings.cpp 830-832, 1091-1101)
    "ifType": 0,                        # IF_Zero above 2 MS/s (Settings.cpp 1019-1022, 813-815)
    "loMode": 1,                        # never written: LO_Auto, the API's default
    "gRdB": 59, "LNAstate": 1,          # IFGR then RFGR, base Device::setGain over setGain (Settings.cpp 537-565)
    "minGr": 20,                        # never written: NORMAL_MIN_GR
    "dcCal": 4, "speedUp": 0, "trackTime": 63,   # activateStream (Streaming.cpp 359-361)
    "refreshRateTime": 2048,            # never written: the API's default
    "DCenable": 1, "IQenable": 1,       # never written without iqcorr_ctrl (Settings.cpp 1617-1626): API default
    "decEnable": 0, "decimationFactor": 1, "wideBandSignal": 0,  # decM 1 == the default factor: untouched (818-827)
    "agcEnable": 0,                     # setGainMode(false): AGC_DISABLE (Settings.cpp 515-527)
    "setPoint_dBfs": -30,               # the constructor (Settings.cpp 65)
    "attack_ms": 0, "decay_ms": 0, "decay_delay_ms": 0, "decay_threshold_dB": 0,   # never written
    "adsbMode": 0,                      # never written: ADSB_DECIMATION
    "hdrEnable": 0,                     # only hdr_ctrl (Settings.cpp 1802-1826)
    "dxBiasTEnable": 0,                 # only biasT_ctrl (Settings.cpp 1661-1706)
    "antennaSel": 0,                    # setAntenna("Antenna A") (Settings.cpp 274-293)
    "dxRfNotchEnable": 0,               # only rfnotch_ctrl (Settings.cpp 1707-1763)
    "dxRfDabNotchEnable": 0,            # only dabnotch_ctrl (Settings.cpp 1764-1801)
    "hdrBw": 3,                         # never written: HDRMODE_BW_1_700
    "rsp1aBiasTEnable": 0, "rsp1aRfNotchEnable": 0, "rsp1aRfDabNotchEnable": 0,  # another model's: untouched
    "rsp2BiasTEnable": 0, "rsp2RfNotchEnable": 0,
    "duoBiasTEnable": 0, "duoAmNotchEnable": 0, "duoRfNotchEnable": 0, "duoRfDabNotchEnable": 0,
}

#: SoapySDR's overall gain under SoapySDRPlay3 on an RSPdx: rxGain ->
#: (gRdB, LNA state, the gain getGain reports). The same table as
#: openhamshack-nb tests/test_sdr_receivers.py SOAPYSDRPLAY3_RSPDX, which
#: Radio's soapy path was held to on ohs-box's settings.
SOAPYSDRPLAY3_RSPDX = {
    0.0: (20, 0, 0.0),
    10.0: (30, 0, 10.0),
    10.7: (30, 0, 10.0),      # (int)30.7 = 30; the 0.7 left is (int)0.7 = 0 on RFGR
    25.0: (45, 0, 25.0),
    39.0: (59, 0, 39.0),
    40.0: (59, 1, 40.0),      # ohs-box: manual 40 was gRdB 59 at LNA state 1, whatever rfgain_sel said
    40.5: (59, 1, 40.0),
    50.0: (59, 11, 50.0),
    66.0: (59, 27, 66.0),
    73.0: (59, 27, 66.0),     # Radio's top; RFGR stops at 27
}
SOAPYSDRPLAY3_RSP1A = {40.0: (59, 1, 40.0), 50.0: (59, 9, 48.0), 73.0: (59, 9, 48.0)}


def ohs_box(lib, **kw):
    """SdrplaySource as Radio opens ohs-box's RSPdx (soapyArgs serial and
    rfgain_sel=4, Antenna A, manual 40, 2 m)."""
    args = {"gain_model": "soapysdrplay3", "lna_state": 4, "antenna": "Antenna A", **kw}
    gain = args.pop("gain", 40.0)
    return SdrplaySource(145_650_000, 2_400_000, gain, lib_path=lib, **args)


def test_an_rspdx_starts_with_every_field_soapysdrplay3_set(sdr):
    lib, ctl = sdr
    with ohs_box(lib) as src:
        init = [e for e in snaps(ctl) if e["what"] == "init"]
        assert len(init) == 1
        got = {k: v for k, v in init[0].items() if k not in ("what", "reason", "ext")}
        assert set(got) == set(SOAPYSDRPLAY3_RSPDX_INIT)
        assert {k: v for k, v in got.items() if v != SOAPYSDRPLAY3_RSPDX_INIT[k]} == {}
        assert [e["what"] for e in snaps(ctl)] == ["init"]        # nothing pushed after it at open
        assert src.status()["options"] == {"bias_t": False, "rf_notch": False, "dab_notch": False, "hdr": False}


def test_the_reduction_model_differs_from_it_only_in_gain(sdr):
    # Packet's model (the default) shares every other field: the parity is
    # the module's, not the gain model's.
    lib, ctl = sdr
    with SdrplaySource(145_650_000, 2_400_000, None, lib_path=lib, antenna="Antenna A"):
        init = snaps(ctl)[0]
    differs = {k for k, v in SOAPYSDRPLAY3_RSPDX_INIT.items() if init[k] != v}
    assert differs == {"gRdB", "LNAstate", "agcEnable"}
    assert (init["gRdB"], init["LNAstate"], init["agcEnable"]) == (50, 3, sdrplay.AGC_50HZ)


def test_soapysdrplay3s_gain_split_is_its_table():
    for hw, table in ((sdrplay.HW_RSPDX, SOAPYSDRPLAY3_RSPDX), (sdrplay.HW_RSP1A, SOAPYSDRPLAY3_RSP1A)):
        for g, (grdb, lna, back) in table.items():
            assert sdrplay.soapy_gain_split(g, hw) == (grdb, lna), (hw, g)
            assert sdrplay.soapy_gain_of(grdb, lna, hw) == back, (hw, g)
    assert sdrplay.soapy_rfgr_range(sdrplay.HW_RSP2) == (0, 8)
    assert sdrplay.soapy_rfgr_range(99) == (20, 59)                # SoapySDRPlay3's fallthrough, kept
    assert [sdrplay.bw_for_rate(r) for r in (250e3, 2.4e6, 6e6, 10e6)] == [200, 1536, 6000, 8000]


def test_the_soapysdrplay3_model_sets_and_reports_its_table_live(sdr):
    lib, ctl = sdr
    with ohs_box(lib) as src:
        assert src.gain_now() == 40.0
        for g, (grdb, lna, back) in SOAPYSDRPLAY3_RSPDX.items():
            src.set_gain(g)
            last = snaps(ctl)[-1]
            assert (last["gRdB"], last["LNAstate"], last["agcEnable"]) == (grdb, lna, sdrplay.AGC_DISABLE), g
            assert int(last["reason"]) & sdrplay.UPDATE_TUNER_GR
            assert src.gain_now() == back
            st = src.status()
            assert (st["gain"], st["gRdB"], st["lnaState"]) == (back, grdb, lna)
        assert ctl.stub_init_count() == 1
    assert src.gain_now() == 66.0                                   # closed: from the gain asked for (73)


def test_another_model_tops_the_lna_state_at_its_own_range(sdr):
    lib, ctl = sdr
    ctl.stub_set_hwver(sdrplay.HW_RSP1A)
    with ohs_box(lib, gain=50.0, antenna=None) as src:
        init = snaps(ctl)[0]
        assert (init["gRdB"], init["LNAstate"]) == (59, 9) and src.gain_now() == 48.0
        assert src.gain_sense() == {"gainIsReduction": True, "gainModel": "soapysdrplay3", "rfgrRange": [0, 9]}


def test_soapysdrplay3s_agc_keeps_the_lna_state_it_had(sdr):
    lib, ctl = sdr
    with ohs_box(lib, gain=None) as src:
        init = snaps(ctl)[0]
        # rfgain_sel's LNA state, AGC_CTRL_EN at -30 dBfs, gRdB the API's 50.
        assert (init["gRdB"], init["LNAstate"], init["agcEnable"], init["setPoint_dBfs"]) == (50, 4, 4, -30)
        assert src.gain_now() == 34.0                                # (50 - 20) + 4
        src.set_gain(45.0)
        last = snaps(ctl)[-1]
        assert (last["gRdB"], last["LNAstate"], last["agcEnable"]) == (59, 6, 0)
        src.set_gain(None)
        last = snaps(ctl)[-1]
        assert (last["gRdB"], last["LNAstate"], last["agcEnable"]) == (59, 6, 4)
        assert src.gain_sense() == {"gainIsReduction": True, "gainModel": "soapysdrplay3", "rfgrRange": [0, 27]}
    with ohs_box(lib, gain=None, agc_setpoint=-40):
        assert snaps(ctl)[-1]["setPoint_dBfs"] == -40


def test_the_reduction_model_is_unchanged(sdr):
    lib, ctl = sdr
    with SdrplaySource(lib_path=lib, gain=35.0, lna_state=4) as src:
        assert src.gain_now() == 35.0 and src.status()["gain"] == 35.0
        assert src.gain_sense() == {"gainIsReduction": True, "gainModel": "reduction", "rfgrRange": [0, 27]}
        assert (snaps(ctl)[0]["gRdB"], snaps(ctl)[0]["LNAstate"]) == (35, 4)
    with pytest.raises(ValueError):
        SdrplaySource(gain_model="db")


def test_the_switches_are_soapysdrplay3s_fields_and_updates(sdr):
    lib, ctl = sdr
    with ohs_box(lib, hdr=True, rf_notch=True) as src:              # RSPdx: all four, on the device
        init = snaps(ctl)[0]
        assert (init["hdrEnable"], init["dxRfNotchEnable"], init["dxRfDabNotchEnable"], init["dxBiasTEnable"]) == \
            (1, 1, 0, 0)
        ctl.stub_clear()
        src.set_options(bias_t=True, hdr=False)
        upd = snaps(ctl)
        assert [(e["reason"], e["ext"]) for e in upd] == [(0, sdrplay.EXT1_RSPDX_BIAST), (0, sdrplay.EXT1_RSPDX_HDR)]
        assert (upd[-1]["dxBiasTEnable"], upd[-1]["hdrEnable"]) == (1, 0)
        assert src.status()["options"] == {"bias_t": True, "rf_notch": True, "dab_notch": False, "hdr": False}
        with pytest.raises(ValueError):
            src.set_options(bias_t=None)
    ctl.stub_set_hwver(sdrplay.HW_RSP1A)
    with ohs_box(lib, antenna=None) as src:
        ctl.stub_clear()
        src.set_options(dab_notch=True, hdr=True)                    # an RSP1A has no HDR: ignored, as SoapySDRPlay3
        upd = snaps(ctl)
        assert [(e["reason"], e["ext"]) for e in upd] == [(sdrplay.UPDATE_RSP1A_DAB_NOTCH, 0)]
        assert upd[0]["rsp1aRfDabNotchEnable"] == 1
        assert "hdr" not in src.status()["options"]
    ctl.stub_set_hwver(sdrplay.HW_RSPDUO)
    with ohs_box(lib, antenna="Tuner 1 Hi-Z") as src:
        ctl.stub_clear()
        src.set_options(rf_notch=True, dab_notch=True)               # Hi-Z: the AM notch; the DAB notch's sign bit
        upd = snaps(ctl)
        assert [e["reason"] for e in upd] == [sdrplay.UPDATE_RSPDUO_AM_NOTCH, sdrplay.UPDATE_RSPDUO_DAB_NOTCH]
        assert (upd[-1]["duoAmNotchEnable"], upd[-1]["duoRfNotchEnable"], upd[-1]["duoRfDabNotchEnable"]) == (1, 0, 1)
        src.set_antenna("Tuner 1 50 ohm")
        ctl.stub_clear()
        src.set_options(rf_notch=True)                               # 50 ohm: the RF notch
        assert [e["reason"] for e in snaps(ctl)] == [sdrplay.UPDATE_RSPDUO_RF_NOTCH]


def test_an_overload_is_acknowledged_as_soapysdrplay3_does(sdr):
    lib, ctl = sdr
    with ohs_box(lib) as src:
        ctl.stub_clear()
        ctl.stub_overload(sdrplay.OVERLOAD_DETECTED)
        assert [e["reason"] for e in snaps(ctl)] == [sdrplay.UPDATE_CTRL_OVERLOAD_ACK]
        assert (src.status()["overloads"], src.status()["overloaded"]) == (1, True)
        ctl.stub_overload(sdrplay.OVERLOAD_CORRECTED)
        assert [e["reason"] for e in snaps(ctl)] == [sdrplay.UPDATE_CTRL_OVERLOAD_ACK] * 2
        assert (src.status()["overloads"], src.status()["overloaded"]) == (1, False)


def test_samples_are_scaled_as_soapysdrplay3s_cf32_and_api_drops_counted(sdr):
    lib, ctl = sdr
    ctl.stub_set_iq.argtypes = (ctypes.POINTER(ctypes.c_short), ctypes.POINTER(ctypes.c_short), ctypes.c_long)
    i = np.arange(-32768, 32768, 7, dtype=np.int16)                 # every corner of the range, full scale included
    q = np.ascontiguousarray(i[::-1])
    ctl.stub_set_iq(i.ctypes.data_as(ctypes.POINTER(ctypes.c_short)), q.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
                    len(i))
    with ohs_box(lib) as src:
        b = src.read().samples
        n = len(b)
        assert n >= sdrplay.BLOCK_S * 2_400_000
        k = np.arange(n) % len(i)
        assert np.array_equal(b.real, i[k] / np.float32(32768)) and np.array_equal(b.imag, q[k] / np.float32(32768))
        assert src.status()["apiGaps"] == 0
        ctl.stub_skip(1000)
        src.read()
        src.read()
        assert (src.status()["apiGaps"], src.status()["apiLost"]) == (1, 1000)
        assert src.stats.dropped == 0                                # counted, not yet trusted to move the index


def test_a_single_input_rsp_reports_rx_and_refuses_to_switch(sdr):
    lib, ctl = sdr
    ctl.stub_set_hwver(sdrplay.HW_RSP1A)
    src = SdrplaySource(lib_path=lib)
    assert src.antennas() == []                       # the model is unknown before open()
    with src:
        assert src.antennas() == ["RX"] and src.antenna == "RX"
        src.set_antenna("RX")                          # confirming the one input is not a refusal
        assert src.antenna == "RX"
        with pytest.raises(SdrError) as e:
            src.set_antenna("Antenna A")
        assert e.value.reason == "bad-antenna"
        assert ctl.stub_init_count() == 1              # never reopened just to check a name


def test_an_rspdx_switches_antennas_live_with_no_reopen(sdr):
    lib, ctl = sdr                                     # stub_reset() leaves hwver at RSPdx
    src = SdrplaySource(lib_path=lib, antenna="Antenna B")
    with src:
        assert src.antennas() == ["Antenna A", "Antenna B", "Antenna C"]
        assert ctl.stub_rspdx_antsel() == 1 and ctl.stub_update_count() == 0   # set at open(), no Update needed
        src.set_antenna("Antenna C")
        assert ctl.stub_rspdx_antsel() == 2 and src.antenna == "Antenna C"
        assert ctl.stub_update_count() == 1 and ctl.stub_init_count() == 1    # one Update, never a reopen
        with pytest.raises(SdrError) as e:
            src.set_antenna("Antenna D")
        assert e.value.reason == "bad-antenna"
        assert src.antenna == "Antenna C"              # a refusal never half-changes it
        assert ctl.stub_init_count() == 1 and ctl.stub_update_count() == 1    # the refusal touched nothing


def test_an_rsp2_leaving_hi_z_updates_the_port_then_the_antenna(sdr):
    lib, ctl = sdr
    ctl.stub_set_hwver(sdrplay.HW_RSP2)
    src = SdrplaySource(lib_path=lib)
    with src:
        assert src.antennas() == ["Antenna A", "Antenna B", "Hi-Z"]
        assert ctl.stub_rsp2_antsel() == 5 and ctl.stub_rsp2_amport() == 0     # Antenna A, 50 ohm (the default)
        src.set_antenna("Hi-Z")
        assert ctl.stub_rsp2_amport() == 1 and ctl.stub_update_count() == 1   # the port alone
        before = ctl.stub_update_count()
        src.set_antenna("Antenna B")                    # leaving Hi-Z: the port alone, which also
        assert ctl.stub_rsp2_amport() == 0 and ctl.stub_rsp2_antsel() == 6   # engages the antenna already set
        assert ctl.stub_update_count() == before + 1
        before = ctl.stub_update_count()
        src.set_antenna("Antenna A")                    # already off Hi-Z: the antenna alone
        assert ctl.stub_rsp2_antsel() == 5 and ctl.stub_update_count() == before + 1
        assert ctl.stub_init_count() == 1


def test_an_rspduo_swaps_the_active_tuner_and_keeps_receiving(sdr):
    lib, ctl = sdr
    ctl.stub_set_hwver(sdrplay.HW_RSPDUO)
    ctl.stub_set_iq.argtypes = (ctypes.POINTER(ctypes.c_short), ctypes.POINTER(ctypes.c_short), ctypes.c_long)
    tone = np.exp(2j * np.pi * 0.01 * np.arange(4096)).astype(np.complex64) * 0.3
    i = np.ascontiguousarray((tone.real * 32767).astype(np.int16))
    q = np.ascontiguousarray((tone.imag * 32767).astype(np.int16))
    ctl.stub_set_iq(i.ctypes.data_as(ctypes.POINTER(ctypes.c_short)), q.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
                     len(tone))
    src = SdrplaySource(lib_path=lib)
    with src:
        assert src.antennas() == ["Tuner 1 50 ohm", "Tuner 1 Hi-Z", "Tuner 2 50 ohm"]
        assert ctl.stub_duo_tuner() == sdrplay.TUNER_A and src.device.tuner == sdrplay.TUNER_A
        src.set_antenna("Tuner 1 Hi-Z")                 # no tuner change: one Update, no swap
        assert ctl.stub_duo_amport() == 1 and ctl.stub_duo_tuner() == sdrplay.TUNER_A
        src.set_antenna("Tuner 2 50 ohm")                # swaps the active tuner
        assert ctl.stub_duo_tuner() == sdrplay.TUNER_B and src.device.tuner == sdrplay.TUNER_B
        assert src.read().samples.dtype == np.complex64  # still receiving, on the other callback
        src.set_antenna("Tuner 1 50 ohm")                 # swaps back
        assert ctl.stub_duo_tuner() == sdrplay.TUNER_A and src.device.tuner == sdrplay.TUNER_A
        with pytest.raises(SdrError) as e:
            src.set_antenna("Tuner 3 50 ohm")
        assert e.value.reason == "bad-antenna"
        assert ctl.stub_init_count() == 1               # tuner swaps are never a reopen


def test_an_unplugged_rsp_is_disconnected(sdr):
    lib, ctl = sdr
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
