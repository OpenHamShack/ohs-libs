"""Stand-ins for the SDR libraries, so the receive backends are tested
through their real code with no receiver attached (none is on shack or
ohs-dev, and the boxes' Plutos belong to QO-100 NB).

- `stub_librtlsdr(dir)`: a librtlsdr built from C here, at test time, with
  the same exported functions as the real one, so sdr/rtlsdr.py's ctypes
  binding - argument types, byref, the read buffer - runs exactly as it
  would on a dongle. It logs every call and plays back IQ the test gives
  it. Skipped without a C compiler.
- `FakePluto`: the libiio Python bindings' shape (Context, devices,
  channels, attrs with .value, Buffer), a Pluto behind it that records
  every write and every device and buffer it was asked for, and every TX
  buffer pushed. Each Context reaches the one radio through a lock of its
  own, as a libiio network context does through its one iiod connection:
  a push held on one context (`push_hold`) stalls every attribute write
  made through that context and none made through another - the case
  radios.PlutoLink's independent watchdog exists for.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from shack_packet.ax25 import Frame
from shack_packet.sdr import Pipeline, Station, SyntheticSource, default_plan

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


def stub_librtlsdr(where: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler to build the stand-in librtlsdr")
    src = where / "stub_rtlsdr.c"
    src.write_text(STUB_C)
    lib = where / "librtlsdr_stub.so"
    subprocess.run([cc, "-shared", "-fPIC", "-O1", "-o", str(lib), str(src)], check=True)
    return lib


class StubControl:
    """The stub's own controls, on the same loaded library as the binding
    (ctypes.CDLL of one path shares the process's one copy)."""

    def __init__(self, path: Path):
        self.lib = ctypes.CDLL(str(path))
        self.lib.stub_log.restype = ctypes.c_char_p
        self.lib.stub_set_iq.argtypes = (ctypes.c_char_p, ctypes.c_int)
        self.lib.stub_reset()

    def log(self) -> list[tuple[str, int]]:
        out = []
        for line in self.lib.stub_log().decode().splitlines():
            name, value = line.split()
            out.append((name, int(value)))
        return out

    def set_iq(self, data: bytes) -> None:
        self.lib.stub_set_iq(data, len(data))


# -- libiio --------------------------------------------------------------------

class _Attr:
    def __init__(self, owner, name, value):
        self.owner, self.name, self._value = owner, name, value

    @property
    def value(self):
        if self.name in self.owner.dev.pluto.fail_reads:
            raise OSError(5, "Input/output error")
        return self._value

    @value.setter
    def value(self, v):
        p = self.owner.dev.pluto
        p.writes.append((self.owner.label, self.name, v))
        p.timeline.append((time.monotonic(), self.owner.label, self.name, str(v)))
        if (self.owner.label, self.name) in p.refuse:
            raise OSError(22, "Invalid argument")
        if self.name == "frequency" and p.lo_range and not p.lo_range[0] <= float(v) <= p.lo_range[1]:
            raise OSError(22, "Invalid argument")
        if (self.owner.label, self.name) not in p.ignore:
            self._value = self._readback(v)

    def _readback(self, v):
        # The driver reads hardwaregain back with its unit, as the real one.
        return f"{float(v):.6f} dB" if self.name == "hardwaregain" else str(v)


class _DevAttr:
    """A device attribute (ad9361-phy's xo_correction): writes logged with
    the device's name as the label."""

    def __init__(self, dev, name, value):
        self.dev, self.name, self._value = dev, name, value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self.dev.pluto.writes.append((self.dev.name, self.name, v))
        if (self.dev.name, self.name) in self.dev.pluto.refuse:
            raise OSError(22, "Invalid argument")
        self._value = str(v)


class _RegAttr:
    """ad9361-phy's debugfs direct_reg_access: "0x27" selects a register,
    "0x27 0x10" writes one, reading gives the selected one's value."""

    def __init__(self, dev):
        self.dev = dev
        self.at = 0

    @property
    def value(self):
        p = self.dev.pluto
        if p.pointer_stolen:
            p.pointer_stolen -= 1
            return hex(p.regs.get(0x27, 0))
        return hex(p.regs.get(self.at, 0))

    @value.setter
    def value(self, v):
        p = self.dev.pluto
        words = str(v).split()
        p.writes.append((self.dev.name, "direct_reg_access", str(v)))
        if p.no_debugfs:
            raise OSError(2, "No such file or directory")
        self.at = int(words[0], 0)
        if len(words) > 1:
            p.regs[self.at] = int(words[1], 0)
            p.timeline.append((time.monotonic(), "reg", hex(self.at), hex(int(words[1], 0))))
            p.reg_log.append((self.at, int(words[1], 0), p.tx_lo_up(), p.gain_db()))


class _Chan:
    def __init__(self, dev, cid, output, label, attrs):
        self.dev, self.id, self.output, self.label = dev, cid, output, label
        self.attrs = {k: _Attr(self, k, v) for k, v in attrs.items()}
        self._enabled = False

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, v):
        self.dev.pluto.enabled.append((self.dev.name, self.id, self.output, v))
        self._enabled = v


class _Dev:
    def __init__(self, pluto, name, chans):
        self.pluto, self.name = pluto, name
        self.channels = [_Chan(self, cid, out, label, attrs) for cid, out, label, attrs in chans]
        self.attrs = {"xo_correction": _DevAttr(self, "xo_correction", "40000000")} if name == "ad9361-phy" else {}
        self.debug_attrs = {"direct_reg_access": _RegAttr(self)} if name == "ad9361-phy" else {}

    def find_channel(self, cid, output=False):
        for c in self.channels:
            if c.id == cid and c.output == output:
                return c
        return None

    def set_kernel_buffers_count(self, n):
        self.pluto.kernel_buffers[self.name] = n


class _CtxAttr:
    """An attribute as one context reaches it: through that context's lock."""

    def __init__(self, ctx, attr):
        self._ctx, self._attr = ctx, attr

    @property
    def value(self):
        with self._ctx.lock:
            return self._attr.value

    @value.setter
    def value(self, v):
        with self._ctx.lock:
            self._attr.value = v


class _CtxChan:
    def __init__(self, ctx, chan):
        self._chan = chan
        self.id, self.output, self.label = chan.id, chan.output, chan.label
        self.attrs = {k: _CtxAttr(ctx, a) for k, a in chan.attrs.items()}

    @property
    def enabled(self):
        return self._chan.enabled

    @enabled.setter
    def enabled(self, v):
        self._chan.enabled = v


class _CtxDev:
    def __init__(self, ctx, dev):
        self.ctx, self._dev = ctx, dev
        self.name, self.attrs = dev.name, dev.attrs
        self.debug_attrs = getattr(dev, "debug_attrs", {})

    def find_channel(self, cid, output=False):
        c = self._dev.find_channel(cid, output)
        return None if c is None else _CtxChan(self.ctx, c)

    def set_kernel_buffers_count(self, n):
        self._dev.set_kernel_buffers_count(n)


class FakePluto:
    """The radio. As booted by Tezuka firmware: TX LO up, -10 dB."""

    def __init__(self, identity="ad9364", tx_lo_powerdown="0", tx_gain="-10.000000 dB"):
        self.identity = identity
        self.writes: list = []
        self.enabled: list = []
        self.devices_asked: list = []
        self.buffers: list = []
        self.contexts: list = []
        self.kernel_buffers: dict = {}
        self.refuse: set = set()          # (label, attr) writes that fail with EINVAL
        self.ignore: set = set()          # writes that "succeed" but do not change the value
        self.fail_reads: set = set()
        self.lo_range = (325e6, 3.8e9) if identity == "ad9363" else (70e6, 6e9)
        self.iq = np.zeros(0, np.int16)
        #: Receive in real time (refill waits a block's worth at the RX rate),
        #: for a link whose reader is a thread of its own; off, a read
        #: returns at once.
        self.rx_realtime = False
        #: Or receive from this: called with the int16 count a read wants
        #: (I,Q interleaved) and returning that many, so a test can make
        #: what is heard depend on what the radio is doing at that moment.
        self.rx_source = None
        self.refills = 0
        #: Receive failures: the next `refill_failures` refills raise EPIPE
        #: (iiod closing the stream, as workbench's nano did), and the next
        #: `rx_busy` receive buffers made after one are refused with EBUSY.
        self.refill_failures = 0
        self.rx_busy = 0
        #: Each push takes this long (iiod and the USB network), holding
        #: its context, as a real one does.
        self.push_delay_s = 0.0
        #: Transmit: every buffer pushed, as (raw bytes, TX LO up when pushed).
        self.pushed: list = []
        self.push_count = 0
        #: From push number N+1 on: raise `push_error`, or wait on
        #: `push_hold` (a threading.Event) holding the pushing context's lock.
        self.push_error: Exception | None = None
        self.push_error_after = 0
        self.push_hold: threading.Event | None = None
        self.hold_after = 0
        #: The AD936x's registers behind debugfs (_RegAttr): GPO mode as the
        #: ADI clone had it (0x26 = 0x80, measured 18/10/2026), GPOs low;
        #: every write with the transmitter's state at that moment.
        self.regs = {0x26: 0x80, 0x27: 0x00}
        self.fw_version = "v0.38"
        #: Another process on the Pluto moving the read pointer to 0x27 (a
        #: firmware's script): the next this-many reads answer 0x27's value.
        self.pointer_stolen = 0
        self.reg_log: list = []
        #: (time.monotonic(), label, attribute or register, value) for every
        #: write, for the order and spacing of a key's steps.
        self.timeline: list = []
        self.no_debugfs = False
        self.phy = _Dev(self, "ad9361-phy", [
            ("altvoltage0", True, "rx_lo", {"frequency": "2400000000", "powerdown": "0"}),
            ("altvoltage1", True, "tx_lo", {"frequency": "2450000000", "powerdown": tx_lo_powerdown}),
            ("voltage0", False, "rx", {"sampling_frequency": "30720000", "rf_bandwidth": "18000000",
                                       "gain_control_mode": "slow_attack", "hardwaregain": "71.000000 dB",
                                       "rf_port_select": "A_BALANCED", "quadrature_tracking_en": "1",
                                       "rf_dc_offset_tracking_en": "1", "bb_dc_offset_tracking_en": "1"}),
            ("voltage0", True, "tx", {"sampling_frequency": "30720000", "rf_bandwidth": "18000000",
                                      "hardwaregain": tx_gain, "rf_port_select": "A"}),
        ])
        self.rxdev = _Dev(self, "cf-ad9361-lpc", [("voltage0", False, "adc0", {}), ("voltage1", False, "adc1", {})])
        self.txdev = _Dev(self, "cf-ad9361-dds-core-lpc", [("voltage0", True, "dac0", {}),
                                                           ("voltage1", True, "dac1", {})])

    def tx_lo_up(self) -> bool:
        return self.attr("tx_lo", "powerdown") == "0"

    def gain_db(self) -> float:
        return float(self.attr("tx", "hardwaregain").split()[0])

    def attr(self, label, name):
        for c in self.phy.channels:
            if c.label == label:
                return c.attrs[name]._value
        raise KeyError(label)

    def tx_writes(self):
        return [w for w in self.writes if w[0] in ("tx", "tx_lo")]

    def tx_quiet(self) -> bool:
        """What the radio itself holds, past any context's lock."""
        return (self.attr("tx_lo", "powerdown") == "1"
                and float(self.attr("tx", "hardwaregain").split()[0]) <= -89.75)

    def tx_iq(self) -> np.ndarray:
        """Every sample pushed, complex, at 12-bit full scale 1.0 (the DAC
        takes the top 12 bits of each int16)."""
        raw = np.frombuffer(b"".join(d for d, _ in self.pushed), np.int16)
        x = (raw >> 4).astype(np.float64) / 2048.0
        return x[0::2] + 1j * x[1::2]

    def module(self):
        pluto = self

        class Ctx:
            def __init__(self, uri):
                self.uri = uri
                self.lock = threading.Lock()
                self.attrs = {"hw_model": "Analog Devices PlutoSDR Rev.C (Z7010-AD9363A)",
                              "ad9361-phy,model": pluto.identity, "fw_version": pluto.fw_version}

            def set_timeout(self, ms):
                pass

            def find_device(self, name):
                pluto.devices_asked.append(name)
                dev = {"ad9361-phy": pluto.phy, "cf-ad9361-lpc": pluto.rxdev,
                       "cf-ad9361-dds-core-lpc": pluto.txdev}.get(name)
                return None if dev is None else _CtxDev(self, dev)

        class Buffer:
            def __init__(self, dev, samples, cyclic):
                if dev.name == "cf-ad9361-lpc" and pluto.rx_busy > 0:
                    pluto.rx_busy -= 1
                    raise OSError(16, "Device or resource busy")
                pluto.buffers.append((dev.name, samples, cyclic))
                self.ctx = dev.ctx
                self.samples = samples
                self.pos = 0
                self.data = b""

            def refill(self):
                pluto.refills += 1
                if pluto.refill_failures > 0:
                    pluto.refill_failures -= 1
                    raise BrokenPipeError(32, "Broken pipe")
                if pluto.rx_realtime:
                    time.sleep(self.samples / float(pluto.attr("rx", "sampling_frequency")))

            def write(self, data):
                if len(data) != 4 * self.samples:
                    raise OSError(22, "Invalid argument")      # a buffer is the size it was made
                self.data = bytes(data)
                return len(data)

            def push(self, samples_count=None):
                with self.ctx.lock:
                    pluto.push_count += 1
                    if pluto.push_error is not None and pluto.push_count > pluto.push_error_after:
                        raise pluto.push_error
                    if pluto.push_hold is not None and pluto.push_count > pluto.hold_after:
                        pluto.push_hold.wait()
                    if pluto.push_delay_s:
                        time.sleep(pluto.push_delay_s)
                    pluto.pushed.append((self.data, pluto.attr("tx_lo", "powerdown") == "0"))

            def read(self):
                n = 2 * self.samples
                if pluto.rx_source is not None:
                    return np.asarray(pluto.rx_source(n), np.int16).tobytes()
                if not len(pluto.iq):
                    return bytes(2 * n)
                idx = (self.pos + np.arange(n)) % len(pluto.iq)
                self.pos = (self.pos + n) % len(pluto.iq)
                return pluto.iq[idx].tobytes()

        class Module:
            @staticmethod
            def Context(uri):
                if uri.startswith("ip:10.255."):
                    raise OSError(113, "No route to host")
                ctx = Ctx(uri)
                pluto.contexts.append(ctx)
                return ctx

        Module.Buffer = Buffer
        return Module


# -- a small band every source test plays ------------------------------------------

RATE = 2_400_000
PLAN = default_plan()
CENTRE = PLAN.centre_for(RATE)


def ui(src, text):
    return Frame.from_monitor(f"{src}>APZOHS,WIDE1-1:{text}").encode()


def band(centre=CENTRE, rate=RATE, seconds=3.0, **kw):
    stations = [Station(PLAN.channels[0].hz, [ui("M0AAA", f">one {j}") for j in range(2)], -30, 0.2, 1.2),
                Station(PLAN.channels[4].hz, [ui("RS0ISS", f">two {j}") for j in range(2)], -35, 0.5, 1.2)]
    return SyntheticSource(centre, rate, stations, noise_dbfs=-40, seconds=seconds, **kw)


def decode(source, limit_s=None):
    pipe = Pipeline(source.rate, source.centre, PLAN, spectrum=False)
    got = 0
    with source:
        while limit_s is None or got < limit_s * source.rate:
            b = source.read()
            if b is None:
                break
            got += len(b.samples)
            pipe.process(b)
    pipe.flush()
    return pipe


def texts(pipe):
    return sorted(d.text.split(":", 1)[1] for d in pipe.monitor.frames)


WANT = [">one 0", ">one 1", ">two 0", ">two 1"]


