"""Identity, the AD9364 enable and XO calibration, against a stand-in Pluto
that reboots: its U-Boot environment decides the chip it comes back as,
and it comes back as a real one does, TX LO up at -10 dB."""

from __future__ import annotations

import threading
import time

import pytest

from shack_pluto import ad9364
from shack_pluto.ad9364 import REASONS as ENABLE_REASONS
from shack_pluto.ad9364 import EnableError, QuietWatcher, enable_ad9364, needs_enable, parse_env
from shack_pluto.identity import family_of, identify, parse_range, usable_serial
from shack_pluto.xo import REASONS as XO_REASONS
from shack_pluto.xo import XoError, XoStore, correction_for, ppm_from_rx, ppm_from_tx, ppm_of

RANGES = {"ad9363": "[325000000 1 3800000000]", "ad9364": "[70000000 1 6000000000]"}


class _Attr:
    def __init__(self, pluto, key):
        self.pluto, self.key = pluto, key

    @property
    def value(self):
        if not self.pluto.up:
            raise OSError(110, "timed out")
        return self.pluto.attrs[self.key]

    @value.setter
    def value(self, v):
        if not self.pluto.up:
            raise OSError(110, "timed out")
        self.pluto.writes.append((self.key, str(v), self.pluto.reboots))
        if self.key == ("tx", "hardwaregain"):
            v = f"{float(v):.6f} dB"
        self.pluto.attrs[self.key] = str(v)


class _Chan:
    def __init__(self, pluto, label, names):
        self.attrs = {n: _Attr(pluto, (label, n)) for n in names}


class _Phy:
    def __init__(self, pluto):
        self.chans = {("altvoltage1", True): _Chan(pluto, "tx_lo", ["powerdown", "frequency_available"]),
                      ("altvoltage0", True): _Chan(pluto, "rx_lo", ["frequency_available"]),
                      ("voltage0", True): _Chan(pluto, "tx", ["hardwaregain"])}

    def find_channel(self, cid, output=False):
        return self.chans.get((cid, output))


class RebootingPluto:
    """The radio, its environment and its SSH: fw_printenv, fw_setenv and a
    detached reboot (down `down_s`, then back as the environment says)."""

    def __init__(self, chip="ad9363", fw="v0.38", serial="104473dc5993000915001800cd5ac2e2c3",
                 down_s=0.3, boot_keyed=True, rekey_after_s=None):
        self.fw, self.serial, self.down_s = fw, serial, down_s
        self.boot_keyed, self.rekey_after_s = boot_keyed, rekey_after_s
        self.env = {"ethaddr": "00:0a:35:00:01:22", "mode": "1r1t"}
        self.up = True
        self.writes: list = []
        self.commands: list = []
        self.reboots = 0
        self.keyed_at: float | None = None
        self.quiet_at: float | None = None
        self._boot(chip)

    def _boot(self, chip):
        self.chip = chip
        self.attrs = {("tx_lo", "powerdown"): "0" if self.boot_keyed else "1",
                      ("tx", "hardwaregain"): "-10.000000 dB" if self.boot_keyed else "-89.750000 dB",
                      ("tx_lo", "frequency_available"): RANGES[chip], ("rx_lo", "frequency_available"): RANGES[chip]}

    def tx_quiet(self):
        return self.attrs[("tx_lo", "powerdown")] == "1" and self.attrs[("tx", "hardwaregain")].startswith("-89.75")

    # -- libiio -------------------------------------------------------------------
    def module(self):
        pluto = self

        class Context:
            def __init__(self, uri):
                if not pluto.up:
                    raise OSError(111, "Connection refused")
                self.attrs = {"hw_model": "Analog Devices PlutoSDR Rev.C (Z7010-AD9363A)", "fw_version": pluto.fw,
                              "hw_serial": pluto.serial, "ad9361-phy,model": pluto.chip}

            def find_device(self, name):
                return _Phy(pluto) if name == "ad9361-phy" else None

        return type("iio", (), {"Context": Context})

    # -- SSH ----------------------------------------------------------------------
    def run(self, command, timeout=10.0):
        self.commands.append(command)
        if command == "fw_printenv":
            return 0, "".join(f"{k}={v}\n" for k, v in self.env.items())
        if command.startswith("fw_setenv "):
            _, k, v = command.split(" ", 2)
            self.env[k] = v
            return 0, ""
        return 127, "sh: not found"

    def detach(self, command):
        self.commands.append(f"detach {command}")
        assert "reboot" in command
        threading.Thread(target=self._reboot, daemon=True).start()

    def _reboot(self):
        time.sleep(0.2)                            # the "sleep 1" before it, shortened
        self.up = False
        self.reboots += 1
        time.sleep(self.down_s)
        new = "ad9364" if (self.env.get("compatible") == "ad9364" and self.env.get("attr_val") == "ad9364") \
            else self.chip
        self._boot(new)
        self.keyed_at = time.monotonic()
        self.up = True
        if self.rekey_after_s is not None:
            time.sleep(self.rekey_after_s)
            self.attrs[("tx_lo", "powerdown")] = "0"


def ident_of(pluto, uri="ip:192.168.2.1"):
    return identify(pluto.module().Context(uri), uri)


def fast_watcher(iio, uri):
    return QuietWatcher(iio, uri, gone_timeout_s=3, back_timeout_s=3, hold_s=0.6)


# -- identity -------------------------------------------------------------------------

def test_each_family_is_told_from_what_the_pluto_says():
    assert family_of("v0.39-dirty") == "adi" and family_of("v0.38") == "adi"
    assert family_of("tezuka-v0.3.21") == "tezuka"
    assert family_of("tezuka-v0.3.21", mqtt_answers=True) == "tezuka"     # the nano runs a broker too
    assert family_of("v0.38", mqtt_answers=True) == "plutodvb2"
    assert family_of("") == "unknown" and family_of("maia-0.6") == "unknown"


def test_a_serial_of_all_fs_is_not_a_key_and_the_mac_is_used_instead():
    assert usable_serial("ffffffffffffffff") is None and usable_serial("0000") is None
    assert usable_serial("UQRUR7HPFYWAMKGY") == "uqrur7hpfywamkgy"
    pluto = RebootingPluto(serial="ffffffffffffffff")
    ident = identify(pluto.module().Context("ip:x"), "ip:x", mac="00:E0:22:12:17:43")
    assert ident.key == "mac:00:e0:22:12:17:43"
    assert identify(pluto.module().Context("ip:x"), "ip:x").key is None


def test_the_range_is_read_from_the_radio_not_assumed():
    assert parse_range("[46875001 1 6000000000]") == (46875001.0, 6e9)
    ident = ident_of(RebootingPluto("ad9363"))
    assert ident.covers(432.65e6) and not ident.covers(144.8e6)
    assert needs_enable(ident, 144.8e6) and not needs_enable(ident, 432.65e6)
    assert not needs_enable(ident, 50.0e6)                 # below even an AD9364: nothing to offer
    assert not needs_enable(ident_of(RebootingPluto("ad9364")), 144.8e6)


# -- the AD9364 enable ------------------------------------------------------------------

def test_the_enable_backs_up_writes_three_reboots_quiets_and_verifies(tmp_path):
    pluto = RebootingPluto("ad9363")
    ident = ident_of(pluto)
    result = enable_ad9364(ident, pluto, pluto.module(), tmp_path, operator_confirmed=True,
                           watcher_factory=fast_watcher)
    assert pluto.commands[:4] == ["fw_printenv", "fw_setenv attr_name compatible", "fw_setenv attr_val ad9364",
                                  "fw_setenv compatible ad9364"]
    assert pluto.reboots == 1 and pluto.commands[-1] == "detach sleep 1; reboot"
    assert parse_env(result.backup.read_text()) == {"ethaddr": "00:0a:35:00:01:22", "mode": "1r1t"}
    assert result.backup.parent == tmp_path / "pluto-env" and ident.key.replace(":", "-") in result.backup.name
    # Came back keyed, as a real one does, and was quieted the moment iiod answered.
    assert pluto.tx_quiet() and result.quiet_after_s < 0.2
    assert result.after.chip == "ad9364" and result.after.covers(144.8e6)


def test_the_watcher_never_acts_on_the_pluto_before_it_has_gone(tmp_path):
    pluto = RebootingPluto("ad9363", down_s=0.5)
    ident = ident_of(pluto)
    pluto.attrs[("tx_lo", "powerdown")] = "0"         # keyed before the reboot (it would not be; prove it)
    enable_ad9364(ident, pluto, pluto.module(), tmp_path, operator_confirmed=True, watcher_factory=fast_watcher)
    # Every write the watcher made was to the radio after its reboot.
    assert pluto.writes and {boot for _k, _v, boot in pluto.writes} == {1}


def test_a_transmitter_that_comes_up_again_during_boot_is_quieted_again(tmp_path):
    pluto = RebootingPluto("ad9363", rekey_after_s=0.2)
    result = enable_ad9364(ident_of(pluto), pluto, pluto.module(), tmp_path, operator_confirmed=True,
                           watcher_factory=fast_watcher)
    assert result.requiets >= 1 and pluto.tx_quiet()


@pytest.mark.parametrize("fw, mqtt, reason", [("tezuka-v0.3.21", False, "enable-firmware"),
                                               ("v0.38", True, "enable-firmware"),
                                               ("custom", False, "enable-firmware")])
def test_firmware_it_has_not_been_checked_on_is_refused_untouched(tmp_path, fw, mqtt, reason):
    pluto = RebootingPluto("ad9363", fw=fw)
    ctx = pluto.module().Context("ip:x")
    ident = identify(ctx, "ip:x", mqtt_answers=mqtt)
    with pytest.raises(EnableError) as e:
        enable_ad9364(ident, pluto, pluto.module(), tmp_path, operator_confirmed=True)
    assert e.value.reason == reason and pluto.commands == [] and pluto.reboots == 0


def test_without_the_operators_word_nothing_is_changed(tmp_path):
    pluto = RebootingPluto("ad9363")
    with pytest.raises(EnableError) as e:
        enable_ad9364(ident_of(pluto), pluto, pluto.module(), tmp_path, operator_confirmed=False)
    assert e.value.reason == "enable-unconfirmed" and pluto.commands == []


def test_an_enabled_pluto_is_not_enabled_again(tmp_path):
    pluto = RebootingPluto("ad9364")
    with pytest.raises(EnableError) as e:
        enable_ad9364(ident_of(pluto), pluto, pluto.module(), tmp_path, operator_confirmed=True)
    assert e.value.reason == "enable-done" and pluto.commands == []


def test_settings_that_do_not_read_back_stop_it_before_the_reboot(tmp_path):
    pluto = RebootingPluto("ad9363")
    real = pluto.run
    pluto.run = lambda c, timeout=10.0: (0, "") if c == "fw_setenv compatible ad9364" else real(c, timeout)
    with pytest.raises(EnableError) as e:
        enable_ad9364(ident_of(pluto), pluto, pluto.module(), tmp_path, operator_confirmed=True)
    assert e.value.reason == "enable-verify" and pluto.reboots == 0


def test_a_pluto_that_does_not_come_back_is_said_plainly(tmp_path):
    pluto = RebootingPluto("ad9363", down_s=60)
    with pytest.raises(EnableError) as e:
        enable_ad9364(ident_of(pluto), pluto, pluto.module(), tmp_path, operator_confirmed=True,
                      watcher_factory=lambda iio, uri: QuietWatcher(iio, uri, gone_timeout_s=1, back_timeout_s=0.5,
                                                                   hold_s=0.1))
    assert e.value.reason == "enable-not-back"


def test_every_reason_has_words():
    assert all(ENABLE_REASONS.values()) and all(k.startswith("enable-") for k in ENABLE_REASONS)
    assert ad9364.SETENV[-1] == ("compatible", "ad9364")


# -- XO calibration ----------------------------------------------------------------------

def test_the_nanos_measured_error_gives_the_value_that_fixed_it():
    assert correction_for(-19.1) == 39_999_236
    assert abs(ppm_of(39_999_236) + 19.1) < 0.01
    # Its carrier at 1298.5 MHz heard 24.7 kHz low; a beacon it hears high.
    assert abs(ppm_from_tx(1298.5e6 - 24_800, 1298.5e6) + 19.1) < 0.05
    assert abs(ppm_from_rx(1298.5e6 * (1 + 19.1e-6), 1298.5e6) + 19.1) < 0.05


def test_the_store_keeps_one_value_per_pluto_and_refuses_nonsense(tmp_path):
    store = XoStore(tmp_path)
    assert store.get("serial:a") is None
    assert store.put("serial:a", -19.1, "tx carrier on the IC-9700") == 39_999_236
    store.put("mac:00:e0:22:dc:e7:1b", 2.0, "beacon")
    again = XoStore(tmp_path)
    assert again.get("serial:a") == 39_999_236 and again.get("mac:00:e0:22:dc:e7:1b") == 40_000_080
    assert again.entry("serial:a")["how"] == "tx carrier on the IC-9700"
    with pytest.raises(XoError) as e:
        store.put("serial:b", 450.0, "typo")
    assert e.value.reason == "xo-out-of-range" and e.value.reason in XO_REASONS
    again.forget("serial:a")
    assert XoStore(tmp_path).get("serial:a") is None
    (tmp_path / "pluto-xo.json").write_text("{not json")
    assert XoStore(tmp_path).get("mac:00:e0:22:dc:e7:1b") is None       # a damaged file is no value, not a crash


def test_the_mac_is_read_from_the_usb_gadget_and_checked():
    class Runner:
        def __init__(self, out, status=0):
            self.out, self.status, self.cmds = out, status, []

        def run(self, cmd, timeout=10.0):
            self.cmds.append(cmd)
            return self.status, self.out
    from shack_pluto.identity import mac_over_ssh
    r = Runner("00:E0:22:12:17:43\x00\n")             # the nano's: a NUL after it, as cat showed
    assert mac_over_ssh(r) == "00:e0:22:12:17:43" and "host_addr" in r.cmds[0]
    assert mac_over_ssh(Runner("", 1)) is None and mac_over_ssh(Runner("garbage")) is None


def test_plutodvb2_is_told_over_ssh_where_its_broker_cannot_be_reached():
    """Through the controller box only iiod and SSH are forwarded: the
    probe's F5OEO script or its version on its own broker says PlutoDVB2."""
    from shack_pluto.identity import parse_probe
    dvb2 = parse_probe("/root/watchconsoletx.sh\n/etc/init.d/S95bgcript\n"
                       "dt/pluto/nocall/system/version PlutoDVB2-0.5.16.7(F5OEO)\n")
    assert dvb2 == {"f5oeo": True, "dvb2_version": "PlutoDVB2-0.5.16.7(F5OEO)", "tezuka": False}
    assert family_of("v0.38", probe=dvb2) == "plutodvb2"
    tezuka = parse_probe('NAME="Tezuka"\nVERSION="v0.3.21"\n')
    assert family_of("", probe=tezuka) == "tezuka"
    plain = parse_probe("")
    assert family_of("v0.39-dirty", probe=plain) == "adi"


def test_a_pluto_behind_the_controller_has_its_own_iiod_and_ssh_ports():
    from shack_pluto.endpoint import EndpointError, Endpoint, parse
    from shack_pluto.guard import GuardError, SshRunner
    assert parse("ip:192.168.2.1") == Endpoint("192.168.2.1", 30431, 22)
    assert parse("ip:ohs-box:30431", ssh_port=30422) == Endpoint("ohs-box", 30431, 30422)
    box = parse("pluto://openhamshack.tail547521.ts.net?iiod=30431&ssh=30422")
    assert box.iio_uri == "ip:openhamshack.tail547521.ts.net" and box.ssh_port == 30422
    assert parse(box.uri) == box and parse("ip:h:1234").iio_uri == "ip:h:1234"
    r = SshRunner.for_uri("pluto://ohs-box?ssh=30422")
    assert (r.host, r.port) == ("ohs-box", 30422) and r.timeout >= 20
    for bad in ("usb:1.5.5", "pluto://?ssh=1", "ip:h:x", "pluto://h?ssh=99999"):
        with pytest.raises(EndpointError):
            parse(bad)
    with pytest.raises(GuardError):
        SshRunner.for_uri("usb:1.2.3")
