"""A Pluto's receive side, through libiio. Receive only, and it keeps the
transmitter quiet on purpose.

An ADALM-Pluto (and the Pluto+, LibreSDR and the nano) is a transceiver.
A Pluto that has just booted can have its TX LO powered up at 2450 MHz
with only 10 dB of attenuation (GUIDE §1 and §5: a Pluto reboot keys its
TX LO; Tezuka firmware does it, PlutoDVB2 does not), radiating from
whatever is on its TX port until something quiets it. So this class:

- **Quiets the transmitter first**, before any receive setting: TX LO
  powerdown = 1 and TX hardwaregain = -89.75 dB (its maximum attenuation),
  as QO-100 NB does whenever it is unkeyed (openhamshack-nb ohscore/devices/
  pluto.py) and Ku Sweep at open. Then it **reads both back**, and if
  either does not read quiet the Pluto is not used at all (SdrError
  "tx-not-quiet") - a receiver is not worth an unknown transmitter.
- **Writes nothing else on the TX side, ever.** Every attribute write goes
  through one gate (`_write`) that allows the receive settings and those
  two quiet values only, and refuses anything else as a bug before it
  reaches the radio (GUIDE §5, ohs-box's Pluto: "gate every attribute
  write"). The TX DMA device (cf-ad9361-dds-core-lpc) is never opened, no
  TX channel is enabled and no TX buffer is made, so no sample can ever
  be sent. The crystal correction (xo_correction) is shared with the
  transmitter, so it is not touched either: the channeliser corrects ppm.
- **Quiets it again at close**, and puts back the receive settings it found
  (LO, rate, bandwidth, gain), so a Pluto shared with QO-100 NB on the same
  desktop is left as NB had it. It never puts back a transmitter setting.

Rate: 2.4 MS/s by default, as the RTL-SDR, and never below 2.084 MS/s.
Below that the AD9361 needs its x4 FIR, and the FIR is shared between
receive and transmit - loading one writes the TX side's filter too, which
the gate forbids. (The baseband rate itself is shared by RX and TX in the
chip; setting it is a receive setting with the transmitter powered down,
as NB does.)

2 m on a Pluto: a stock Pluto runs as an AD9363 (325 MHz-3.8 GHz) and
cannot tune 144 MHz; the AD9364 setting (ADI's "customizing" fw_setenv,
which the app never does itself) reaches 70 MHz. The chip is read from the
context (`ad9361-phy,model`) and an AD9363 is refused with that sentence
("out-of-range"), before any receive setting is written.

The libiio Python bindings (`iio`, libiio 0.2x; openhamshack-nb's import_iio
explains the 1.0 trap) are what an installer bundles: libiio.dll on
Windows (plus ADI's Pluto USB drivers for USB, ticked in the installer),
libiio.dylib on macOS, the distribution's libiio0 and python3-libiio on
Linux. `iio_module` lets the tests give a stand-in.

NOT YET TESTED ON HARDWARE: no Pluto may be touched from shack (the boxes'
Plutos belong to QO-100 NB). The attribute names are NB's, which are the
ADI driver's (drivers/iio/adc/ad9361.c).
"""

from __future__ import annotations

import numpy as np

from ..errors import PacketError
from .source import IqSource, SdrError

TX_ATTEN_MAX = -89.75
NO_FIR_MIN = 2_083_334            # lowest AD9361 baseband rate without the x4 FIR
AD9363_RANGE = (325e6, 3.8e9)
#: What the operator is told when a stock (AD9363) Pluto is asked for 2 m:
#: radios.PlutoLink says the same.
AD9363_REFUSAL = (f"this Pluto runs as an AD9363 ({AD9363_RANGE[0] / 1e6:.0f} MHz-{AD9363_RANGE[1] / 1e9:.1f} GHz); "
                  "2 m needs the AD9364 setting (ADI's 'customizing' page: two fw_setenv commands)")
FULL_SCALE = 2048.0               # 12-bit ADC samples, sign-extended in int16

#: The gate: (channel, attribute) a receiver may write, and for the TX
#: side the one value each may be written with.
RX_WRITES = frozenset({
    ("rx_lo", "frequency"), ("rx", "sampling_frequency"), ("rx", "rf_bandwidth"), ("rx", "gain_control_mode"),
    ("rx", "hardwaregain"), ("rx", "rf_port_select"), ("rx", "quadrature_tracking_en"),
    ("rx", "rf_dc_offset_tracking_en"), ("rx", "bb_dc_offset_tracking_en"),
})
TX_QUIET = {("tx_lo", "powerdown"): "1", ("tx", "hardwaregain"): f"{TX_ATTEN_MAX}"}
#: What is put back at close, in this order (the LO last: it is the one a
#: watching app notices).
RESTORE = (("rx", "sampling_frequency"), ("rx", "rf_bandwidth"), ("rx", "gain_control_mode"),
           ("rx", "hardwaregain"), ("rx_lo", "frequency"))


def import_iio():
    try:
        import iio
    except ImportError as e:
        raise SdrError("no-library", f"the libiio Python bindings are not installed ({e})") from e
    except (AttributeError, OSError, TypeError) as e:
        # libiio 1.0's library under the 0.2x bindings, or no library at
        # all behind them (openhamshack-nb's import_iio has the cases).
        raise SdrError("no-library", f"libiio could not be loaded ({e})") from e
    return iio


def _number(text) -> float | None:
    try:
        return float(str(text).split()[0])
    except (ValueError, IndexError):
        return None


class PlutoSource(IqSource):
    kind = "pluto"

    def __init__(self, uri: str = "ip:192.168.2.1", centre: float = 145_312_500.0, rate: float = 2_400_000,
                 gain: float | None = None, *, iio_module=None, block_s: float = 0.05, restore: bool = True,
                 port: str = "A_BALANCED"):
        super().__init__(centre, rate, gain)
        self.uri = uri
        self._iio = iio_module
        self.block = int(rate * block_s)
        self.restore = restore
        self.port = port
        self.ctx = None
        self.rxbuf = None
        self.chans: dict = {}
        self.identity = ""
        self.found: dict = {}          # the receive settings at open, for close
        self.tx_quiet = False

    # -- the gate ---------------------------------------------------------------

    def _write(self, chan: str, name: str, value) -> None:
        text = str(value)
        if chan in ("tx", "tx_lo"):
            if TX_QUIET.get((chan, name)) != text:
                raise PacketError(f"refused: {chan} {name}={text} is a transmitter setting")
        elif (chan, name) not in RX_WRITES:
            raise PacketError(f"refused: {chan} {name} is not a receive setting")
        try:
            self.chans[chan].attrs[name].value = text
        except (KeyError, OSError, AttributeError) as e:
            raise SdrError("setup-failed", f"Pluto {self.uri}: {chan} {name}={text} ({e})") from e

    def _read_attr(self, chan: str, name: str):
        try:
            return self.chans[chan].attrs[name].value
        except (KeyError, OSError, AttributeError):
            return None

    def _quiet(self) -> bool:
        """Quiet the transmitter and say whether it reads back quiet."""
        ok = True
        for (chan, name), value in TX_QUIET.items():
            try:
                self._write(chan, name, value)
            except SdrError:
                ok = False
        pd = _number(self._read_attr("tx_lo", "powerdown"))
        gain = _number(self._read_attr("tx", "hardwaregain"))
        return ok and pd == 1 and gain is not None and gain <= TX_ATTEN_MAX + 0.01

    # -- open and close ---------------------------------------------------------

    def _open(self) -> None:
        if self.rate < NO_FIR_MIN:
            raise SdrError("rate-unsupported", f"a Pluto receiver here runs at {NO_FIR_MIN} S/s or more "
                           f"(below needs the FIR, shared with the transmitter)")
        self._mod = iio = self._iio or import_iio()
        try:
            self.ctx = iio.Context(self.uri)
        except OSError as e:
            raise SdrError("no-device", f"no Pluto at {self.uri} ({e})") from e
        try:
            self._setup()
        except Exception:
            self._release()
            raise

    def _setup(self) -> None:
        try:
            self.ctx.set_timeout(3000)
        except (AttributeError, OSError):
            pass
        attrs = getattr(self.ctx, "attrs", {}) or {}
        self.identity = str(attrs.get("ad9361-phy,model", "") or "").lower()
        phy = self.ctx.find_device("ad9361-phy")
        rxdev = self.ctx.find_device("cf-ad9361-lpc")
        if phy is None or rxdev is None:
            raise SdrError("not-receiver", f"{self.uri} is not an AD936x (no ad9361-phy / cf-ad9361-lpc)")
        self.chans = {"rx_lo": phy.find_channel("altvoltage0", True), "tx_lo": phy.find_channel("altvoltage1", True),
                      "rx": phy.find_channel("voltage0", False), "tx": phy.find_channel("voltage0", True)}
        if any(c is None for c in self.chans.values()):
            raise SdrError("not-receiver", f"{self.uri}: the AD936x's channels are not all there")
        # The transmitter first, before anything else is written.
        self.tx_quiet = self._quiet()
        if not self.tx_quiet:
            raise SdrError("tx-not-quiet", f"Pluto {self.uri}: TX LO powerdown / attenuation did not read back")
        lo, hi = AD9363_RANGE
        if self.identity == "ad9363" and not lo <= self.centre <= hi:
            raise SdrError("out-of-range", AD9363_REFUSAL)
        self.found = {key: self._read_attr(*key) for key in RESTORE}
        self._write("rx", "sampling_frequency", int(self.rate))
        self._write("rx", "rf_bandwidth", int(self.rate))
        try:
            self._write("rx", "rf_port_select", self.port)
        except SdrError as e:
            self.stats.notes.append(e.detail)
        for name in ("quadrature_tracking_en", "rf_dc_offset_tracking_en", "bb_dc_offset_tracking_en"):
            try:
                self._write("rx", name, 1)
            except SdrError:
                pass
        self._apply_gain()
        try:
            self._write("rx_lo", "frequency", int(round(self.centre)))
        except SdrError as e:
            raise SdrError("out-of-range", e.detail) from e
        for i in (0, 1):
            rxdev.find_channel(f"voltage{i}", False).enabled = True
        try:
            rxdev.set_kernel_buffers_count(4)
        except (AttributeError, OSError):
            pass
        self.rxbuf = self._buffer(rxdev)

    def _buffer(self, rxdev):
        try:
            return self._mod.Buffer(rxdev, self.block, False)
        except OSError as e:
            raise SdrError("busy", f"Pluto {self.uri}: the receive buffer ({e}); is another program using it?") from e

    def _apply_gain(self) -> None:
        if self.gain is None:
            self._write("rx", "gain_control_mode", "slow_attack")
        else:
            self._write("rx", "gain_control_mode", "manual")
            self._write("rx", "hardwaregain", float(self.gain))

    def set_gain(self, gain: float | None) -> None:
        self.gain = gain
        if self.ctx is not None:
            self._apply_gain()

    def set_centre(self, hz: float) -> None:
        """Retune while running. The channels were placed for the old centre:
        the app makes a new Pipeline for the new one (ChannelPlan.centre_for)."""
        self.centre = float(hz)
        if self.ctx is not None:
            self._write("rx_lo", "frequency", int(round(hz)))

    def _read(self) -> np.ndarray:
        try:
            self.rxbuf.refill()
            raw = self.rxbuf.read()
        except OSError as e:
            raise SdrError("read-failed", f"Pluto {self.uri}: {e}") from e
        return (np.frombuffer(raw, np.int16).astype(np.float32) / FULL_SCALE).view(np.complex64)

    def _close(self) -> None:
        self._release()

    def _release(self) -> None:
        self.rxbuf = None
        if self.ctx is not None and self.chans:
            if self.restore and self.found:
                for (chan, name) in RESTORE:
                    value = self.found.get((chan, name))
                    v = _number(value)
                    if value is None or (name != "gain_control_mode" and v is None):
                        continue
                    if name == "hardwaregain" and str(self.found.get(("rx", "gain_control_mode"))) != "manual":
                        continue           # the driver only takes a gain in manual mode
                    try:
                        self._write(chan, name, value if name == "gain_control_mode" else
                                    (v if name == "hardwaregain" else int(v)))
                    except SdrError:
                        pass
            self.tx_quiet = self._quiet()
        self.ctx = None
        self.chans = {}

    def status(self) -> dict:
        st = super().status()
        st.update({"uri": self.uri, "identity": self.identity, "txQuiet": self.tx_quiet})
        return st
