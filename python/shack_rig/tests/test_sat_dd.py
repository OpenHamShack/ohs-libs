"""The satellite and DD commands Packet Radio 0.3.0 sends (civ.py's
"satellites, the other VFO, and DD" notes): their bytes, and what they do to
FakeIcom through the whole LAN path, including the DD transmitter the fake
models for tools/dd-chatter-test.py."""

import asyncio

import pytest

from helpers import OnFake, until
from shack_rig.icom import civ


def test_the_other_vfo_and_satellite_mode_build_and_decode():
    assert civ.set_vfo_freq(145_825_000) == b"\x25\x00" + civ.encode_freq(145_825_000)
    assert civ.set_vfo_freq(435_000_000, unselected=True)[:2] == b"\x25\x01"
    assert civ.decode_vfo_freq(b"\x25\x01" + civ.encode_freq(437_800_000)) == (True, 437_800_000)
    assert civ.set_satellite_mode(True) == b"\x16\x5a\x01"
    assert civ.decode_satellite_mode(b"\x16\x5a\x00") is False
    assert civ.set_dd_mode() == b"\x06\x22"
    assert civ.set_rps(True) == b"\x0f\x13" and civ.set_rps(False) == b"\x0f\x10"
    for bad in (b"\x25\x02" + bytes(5), b"\x25\x00", b"\x16\x5a\x07", b"\x16\x40\x01"):
        with pytest.raises(civ.CivError):
            (civ.decode_vfo_freq if bad[0] == 0x25 else civ.decode_satellite_mode)(bad)


def test_on_the_fake_radio_both_vfos_and_satellite_mode_answer_and_move():
    async def run():
        async with OnFake() as (rig, radio):
            got = await rig.ask(civ.read_vfo_freq(True), b"\x25\x01", 2.0)
            assert civ.decode_vfo_freq(got) == (True, 145_825_000)
            assert rig.send(civ.set_vfo_freq(145_828_000, unselected=True))
            assert rig.send(civ.set_satellite_mode(True))
            assert await until(lambda: radio.satellite and radio.freq_unselected == 145_828_000, 2)
            got = await rig.ask(civ.read_satellite_mode(), b"\x16\x5a", 2.0)
            assert civ.decode_satellite_mode(got) is True
            assert radio.vfo_writes == [(True, 145_828_000)]

    asyncio.run(run())


def test_dd_is_refused_off_23cm_and_its_transmitter_follows_tx_inhibit():
    async def run():
        async with OnFake() as (rig, radio):
            # 2 m: NG, and the radio stays in its mode.
            got = await rig.ask(civ.set_dd_mode(), b"\xfb", 2.0, refused=True)
            assert got == b"\xfa" and radio.mode != 0x22
            rig.set_freq(1_299_525_000)
            assert await until(lambda: radio.freq == 1_299_525_000, 2)
            assert await rig.ask(civ.set_dd_mode(), b"\xfb", 2.0) == b"\xfb"
            assert radio.mode == 0x22
            # TX INHIBIT on (as powered up): LAN traffic stays off the air.
            assert radio.lan_traffic(3) is False
            ptt = await rig.ask(civ.read_ptt(), b"\x1c\x00", 2.0)
            assert ptt[2] == 0
            radio.press_transmit()
            assert radio.lan_traffic(20)
            ptt = await rig.ask(civ.read_ptt(), b"\x1c\x00", 2.0)
            po = await rig.ask(civ.read_power(), b"\x15\x11", 2.0)
            assert ptt[2] == 1 and civ.decode_meter(po[2:4]) > 0
            out = await rig.ask(civ.read_menu(civ.DD_PACKET_OUTPUT_ITEM), b"\x1a\x05", 2.0)
            assert civ.decode_menu(out) == (88, 0x00)
            assert radio.menu_writes == []

    asyncio.run(run())


# -- the set-ups, through shack_rig.datasetup ---------------------------------

def _caller(loop):
    """What a packet link's `_call` is: run on the rig's loop, from a thread."""
    def call(fn, *args, timeout=2.0):
        async def run():
            got = fn(*args)
            if asyncio.iscoroutine(got):
                got = await got
            return got
        return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout)
    return call


def test_dd_is_set_up_on_23cm_journalled_and_put_back_exactly(tmp_path):
    from shack_rig.datasetup import DataSetup, Journal
    from shack_rig.icom.satdd import dd_settings

    async def run():
        async with OnFake() as (rig, radio):
            radio.freq, radio.mode, radio.data, radio.filter = 145_825_000, 0x05, 0x01, 0x01   # FM-D FIL1
            radio.rf_power_by_band[1_240_000_000] = 200
            radio.duplex = 0x13                                                                # RPS left on
            call = _caller(asyncio.get_running_loop())
            journal = Journal(tmp_path / "dd.json")
            setup = DataSetup(dd_settings(rig, call, frequency=1_299_525_000, power_level=0), journal)
            report = {line.key: line.status for line in await asyncio.to_thread(setup.apply)}
            assert report == {"frequency": "set", "mode": "set", "rps": "set", "rf_power": "set",
                              "dd_packet_output": "report"}
            assert (radio.freq, radio.mode, radio.duplex, radio.rf_power_level) == (1_299_525_000, 0x22, 0x10, 0)
            assert [k for k, _v in journal.load()] == ["frequency", "mode", "rps", "rf_power"]
            back = await asyncio.to_thread(setup.restore)
            assert [line.key for line in back] == ["rf_power", "rps", "mode", "frequency"]
            assert all(line.status == "restored" for line in back)
            assert radio.rf_power_by_band[1_240_000_000] == 200, "23 cm's own level, put back on 23 cm"
            assert (radio.freq, radio.mode, radio.data, radio.filter) == (145_825_000, 0x05, 0x01, 0x01)
            assert radio.duplex == 0x13 and not journal.path.exists()
            assert radio.menu_writes == []

    asyncio.run(run())


def test_a_pass_in_split_moves_both_vfos_and_puts_them_and_split_back(tmp_path):
    from shack_rig.datasetup import DataSetup, Journal
    from shack_rig.icom import satdd

    async def run():
        async with OnFake() as (rig, radio):
            radio.duplex = 0x10                    # simplex, as the packet set-up leaves it
            call = _caller(asyncio.get_running_loop())
            setup = DataSetup(satdd.satellite_settings(rig, call, satdd.SPLIT, rx_hz=145_828_500,
                                                       tx_hz=145_821_500), Journal(tmp_path / "sat.json"))
            report = {line.key: line.status for line in await asyncio.to_thread(setup.apply)}
            assert report == {"split": "set", "rx_vfo": "set", "tx_vfo": "set"}
            assert radio.split and radio.freq_unselected == 145_821_500
            assert satdd.retune(rig, satdd.SPLIT, rx_hz=145_826_000, tx_hz=145_824_000)      # on the loop
            assert await until(lambda: (radio.freq, radio.freq_unselected) == (145_826_000, 145_824_000), 2)
            await asyncio.to_thread(setup.restore)
            assert not radio.split and (radio.freq, radio.freq_unselected) == (144_174_000, 145_825_000)
            assert radio.satellite_writes == [], "split needs no satellite mode"

    asyncio.run(run())


def test_a_cross_band_pass_uses_satellite_mode_main_up_sub_down(tmp_path):
    from shack_rig.datasetup import DataSetup, Journal
    from shack_rig.icom import satdd

    async def run():
        async with OnFake() as (rig, radio):
            call = _caller(asyncio.get_running_loop())
            setup = DataSetup(satdd.satellite_settings(rig, call, satdd.SATELLITE, rx_hz=435_350_000,
                                                       tx_hz=145_850_000), Journal(tmp_path / "x.json"))
            await asyncio.to_thread(setup.apply)
            assert radio.satellite and (radio.freq, radio.freq_unselected) == (145_850_000, 435_350_000)
            await asyncio.to_thread(setup.restore)
            assert not radio.satellite and radio.freq == 144_174_000
        with pytest.raises(ValueError):
            satdd.satellite_settings(rig, call, "both", rx_hz=1, tx_hz=1)

    asyncio.run(run())
