"""A stand-in libhackrf, built from C at test time as the librtlsdr one is
(sdrhelp.stub_librtlsdr), so radios/hackrf.py's ctypes binding - the
callback type, the transfer struct, byref, the M0 state - runs exactly as
it would on a HackRF. A thread of its own plays libhackrf's transfer thread:
in RX it hands the callback 262 144-byte buffers of the IQ a test gave it,
at the sample rate; in TX it asks the callback for buffers and keeps every
byte. It models the firmware's transceiver mode (OFF, RX, TX), which is
what the safety tests read, and can be told to report an old USB API."""

from __future__ import annotations

import ctypes
import shutil
import subprocess
from pathlib import Path

import pytest

STUB_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <pthread.h>
#include <unistd.h>
#include <time.h>

typedef struct { void *device; uint8_t *buffer; int buffer_length; int valid_length; void *rx_ctx; void *tx_ctx; } hackrf_transfer;
typedef int (*cb_fn)(hackrf_transfer *);
typedef struct { uint16_t requested_mode, request_flag; uint32_t active_mode, m0_count, m4_count, num_shortfalls,
                 longest_shortfall, shortfall_limit, threshold, next_mode, error; } m0_state_t;

#define BUF 262144
static char logbuf[1 << 16]; static size_t loglen;
static int mode;                  /* 0 off, 1 rx, 2 tx: the firmware's transceiver mode */
static int streaming, open_result, api = 0x0113, stop_tx_result;
static double rate = 10e6;
static unsigned char *rxiq; static long rxlen, rxpos;
static unsigned char *txout; static long txcap, txlen;
static cb_fn cb; static pthread_t th; static int running;
static uint8_t buf[BUF];
static int dev_token = 7;
static uint32_t underrun;

static void logf_(const char *n, long v) {
    int k = snprintf(logbuf + loglen, sizeof logbuf - loglen, "%s %ld\n", n, v);
    if (k > 0 && loglen + k < sizeof logbuf) loglen += k;
}
const char *stub_log(void) { logbuf[loglen] = 0; return logbuf; }
void stub_reset(void) { loglen = 0; mode = 0; streaming = 0; open_result = 0; api = 0x0113; stop_tx_result = 0;
                        rxpos = 0; txlen = 0; underrun = 0; }
void stub_set_rx_iq(const unsigned char *d, long n) { free(rxiq); rxiq = malloc(n); memcpy(rxiq, d, n); rxlen = n; rxpos = 0; }
void stub_set_api(int v) { api = v; }
void stub_set_open_result(int r) { open_result = r; }
void stub_set_mode(int m) { mode = m; }
int stub_mode(void) { return mode; }
long stub_tx_len(void) { return txlen; }
const unsigned char *stub_tx(void) { return txout; }
uint32_t stub_underrun(void) { return underrun; }

static void *pump(void *arg) {
    int tx = (int)(long)arg;
    hackrf_transfer t; memset(&t, 0, sizeof t);
    t.device = &dev_token; t.buffer = buf; t.buffer_length = BUF;
    struct timespec ts = {0, (long)(BUF / 2 / rate * 1e9)};
    while (running) {
        if (tx) {
            t.valid_length = 0;
            if (cb(&t) != 0) break;
            if (txlen + t.valid_length > txcap) { txcap = (txlen + t.valid_length) * 2; txout = realloc(txout, txcap); }
            memcpy(txout + txlen, buf, t.valid_length); txlen += t.valid_length;
        } else {
            for (int i = 0; i < BUF; i++) { buf[i] = rxlen ? rxiq[rxpos] : 0; if (rxlen) rxpos = (rxpos + 1) % rxlen; }
            t.valid_length = BUF;
            if (cb(&t) != 0) break;
        }
        nanosleep(&ts, 0);
    }
    return 0;
}
static int start(cb_fn f, int tx) { cb = f; running = 1; streaming = 1; return pthread_create(&th, 0, pump, (void *)(long)tx) ? -1000 : 0; }
static void halt(void) { if (running) { running = 0; pthread_join(th, 0); } streaming = 0; }

int hackrf_init(void) { logf_("init", 0); return 0; }
int hackrf_exit(void) { logf_("exit", 0); return 0; }
int hackrf_open(void **d) { logf_("open", 0); if (open_result < 0) return open_result; *d = &dev_token; return 0; }
int hackrf_open_by_serial(const char *s, void **d) { logf_("open_by_serial", 0); *d = &dev_token; return open_result; }
int hackrf_close(void *d) { halt(); mode = 0; logf_("close", 0); return 0; }
int hackrf_start_rx(void *d, cb_fn f, void *ctx) { logf_("start_rx", 0); mode = 1; return start(f, 0); }
int hackrf_stop_rx(void *d) { halt(); mode = 0; logf_("stop_rx", 0); return 0; }
int hackrf_start_tx(void *d, cb_fn f, void *ctx) { logf_("start_tx", 0); mode = 2; return start(f, 1); }
int hackrf_stop_tx(void *d) { logf_("stop_tx", 0); if (stop_tx_result < 0) return stop_tx_result; halt(); mode = 0; return 0; }
void stub_set_stop_tx_result(int r) { stop_tx_result = r; }
int hackrf_is_streaming(void *d) { return streaming ? 1 : -1004; }
int hackrf_set_freq(void *d, uint64_t f) { logf_("freq", (long)f); return 0; }
int hackrf_set_sample_rate(void *d, double r) { rate = r; logf_("rate", (long)r); return 0; }
int hackrf_set_baseband_filter_bandwidth(void *d, uint32_t b) { logf_("bb", b); return 0; }
int hackrf_set_lna_gain(void *d, uint32_t g) { logf_("lna", g); return 0; }
int hackrf_set_vga_gain(void *d, uint32_t g) { logf_("vga", g); return 0; }
int hackrf_set_txvga_gain(void *d, uint32_t g) { logf_("txvga", g); return 0; }
int hackrf_set_amp_enable(void *d, uint8_t on) { logf_("amp", on); return 0; }
int hackrf_usb_api_version_read(void *d, uint16_t *v) { *v = api; return 0; }
int hackrf_version_string_read(void *d, char *s, uint8_t n) { snprintf(s, n, "stub-2026.09"); return 0; }
int hackrf_set_tx_underrun_limit(void *d, uint32_t v) { underrun = v; logf_("underrun", v); return api >= 0x0106 ? 0 : -1003; }
int hackrf_get_m0_state(void *d, m0_state_t *s) { memset(s, 0, sizeof *s); s->active_mode = mode == 2 ? 4 : mode == 1 ? 2 : 0; return 0; }
const char *hackrf_error_name(int e) { return "HACKRF_ERROR_STUB"; }
"""


def stub_libhackrf(where: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler to build the stand-in libhackrf")
    src = where / "stub_hackrf.c"
    src.write_text(STUB_C)
    lib = where / "libhackrf_stub.so"
    subprocess.run([cc, "-shared", "-fPIC", "-O1", "-pthread", "-o", str(lib), str(src)], check=True)
    return lib


class HackrfControl:
    def __init__(self, path: Path):
        self.lib = ctypes.CDLL(str(path))
        self.lib.stub_log.restype = ctypes.c_char_p
        self.lib.stub_tx.restype = ctypes.POINTER(ctypes.c_uint8)
        self.lib.stub_tx_len.restype = ctypes.c_long
        self.lib.stub_set_rx_iq.argtypes = (ctypes.c_char_p, ctypes.c_long)
        self.lib.stub_underrun.restype = ctypes.c_uint32
        self.lib.stub_reset()

    def log(self) -> list[tuple[str, int]]:
        return [(n, int(v)) for n, v in (line.split() for line in self.lib.stub_log().decode().splitlines())]

    def calls(self) -> list[str]:
        return [n for n, _v in self.log()]

    def mode(self) -> int:
        return self.lib.stub_mode()

    def tx_bytes(self) -> bytes:
        n = self.lib.stub_tx_len()
        return ctypes.string_at(self.lib.stub_tx(), n) if n else b""
