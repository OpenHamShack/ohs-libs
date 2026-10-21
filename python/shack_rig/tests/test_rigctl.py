"""The hamlib NET rigctl server: its wire format, and transmit through leases.

The wire checks are ported from OpenHamShack Icom's tools/selftest.py
(test_dump_state, test_rigctl_wire, the hamlib half of test_data_mode) and
drive `_dispatch` with a stub rig, as they did there. The transmit checks run
over a real TCP socket against an IcomRig logged in to FakeIcom, because the
promise being tested - a client that vanishes while keyed is unkeyed - only
exists when there is a socket to vanish.
"""

import asyncio
import re
import time

import pytest

from helpers import OnFake, until
from shack_rig import Lease, PttRefused
from shack_rig.icom import models
from shack_rig.rigctl import RigctlServer, _dump_state, civ_mode, hamlib_mode, modes_mask


class Stub:
    """A rig whose setters answer True or False, as the real ones do.

    The original stub returned None, which is how a refusal used to read as
    success both here and on the wire.
    """

    def __init__(self, **state):
        self.state = {"connected": True, "freq": 144800000, "mode": "USB",
                      "data_mode": False, "filter": "FIL1", "ptt": False,
                      "vfo": "VFOA", "split": False, **state}
        self.model = models.model("ic-9700")
        self.allow_ptt = True
        self.refuse = False
        self.keyed_with = []

    def set_freq(self, hz):
        if self.refuse:
            return False
        self.state["freq"] = hz
        return True

    def set_mode(self, m, f=None, data=None, vfo=None):
        if self.refuse:
            return False
        self.state["mode"] = m
        if data is not None:
            self.state["data_mode"] = data
        return True

    def set_ptt(self, on, lease=None):
        if on:
            if lease is None:
                raise PttRefused("no lease")
            why = lease.refusal()
            if why:
                raise PttRefused(why)
            self.keyed_with.append(lease.holder)
        self.state["ptt"] = on
        return True

    def set_vfo(self, v):
        return True

    def set_split(self, on):
        if self.refuse:
            return False
        self.state["split"] = on
        return True


# -- dump_state --------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(models.MODELS))
def test_dump_state_is_what_netrigctl_parses_positionally(key):
    lines = _dump_state(models.model(key)).rstrip("\n").split("\n")
    assert lines[0] == "1" and lines[-1] == "done"
    # Every range line must have exactly seven fields or hamlib stops with a
    # protocol error rather than a warning.
    ranges = [ln for ln in lines if re.match(r"^\d+ \d+ 0x", ln)]
    assert ranges and all(len(ln.split()) == 7 for ln in ranges)
    assert lines.count("0 0 0 0 0 0 0") == 2
    assert lines.count("0 0") == 2
    assert f"rigctld_version=shack-rig-{key}" in lines


def test_dump_state_describes_the_rig_in_use():
    s = RigctlServer(Stub(), "127.0.0.1", 0)
    nine = s._dispatch("\\dump_state")[0]
    assert "1240000000 1300000000" in nine            # 23 cm on an IC-9700
    seven = Stub()
    seven.model = models.model("ic-7610")
    hf = RigctlServer(seven, "127.0.0.1", 0)._dispatch("\\dump_state")[0]
    assert "1240000000" not in hf and "14000000 14350000" in hf
    # An Icom model key, as the Icom app passes one, still works.
    assert "14000000 14350000" in RigctlServer(Stub(), "127.0.0.1", 0, "ic-7610")._dispatch("\\dump_state")[0]


# -- the wire format ---------------------------------------------------------

def test_gets_answer_bare_values_and_sets_answer_rprt():
    s = RigctlServer(Stub(), "127.0.0.1", 0)
    # Getting this backwards does not produce an error - it hangs the client.
    assert s._dispatch("f") == ("144800000\n", False)
    assert s._dispatch("F 14074000") == ("RPRT 0\n", False)
    assert s._dispatch("f") == ("14074000\n", False)
    assert s._dispatch("F14074000.000000") == ("RPRT 0\n", False)
    assert s._dispatch("m") == ("USB\n2400\n", False)
    assert s._dispatch("\\chk_vfo") == ("0\n", False)
    assert s._dispatch("q") == ("RPRT 0\n", True)
    body, close = s._dispatch("\\bogus")
    assert body.startswith("RPRT -") and not close


def test_a_refusal_is_not_reported_as_done():
    rig = Stub()
    rig.refuse = True
    r = RigctlServer(rig, "127.0.0.1", 0)
    assert r._dispatch("F 14074000") == ("RPRT -17\n", False)
    assert r._dispatch("M PKTUSB 2400") == ("RPRT -9\n", False)
    assert r._dispatch("S 1 VFOB") == ("RPRT -9\n", False)


def test_a_radio_that_is_not_there_says_so():
    gone = Stub(connected=False)
    g = RigctlServer(gone, "127.0.0.1", 0)
    assert g._dispatch("\\get_powerstat") == ("0\n", False)
    assert g._dispatch("\\dump_state")[0].endswith("done\n")
    assert g._dispatch("f") == ("RPRT -6\n", False)
    assert g._dispatch("m") == ("RPRT -6\n", False)
    assert g._dispatch("F 50313000") == ("RPRT -6\n", False)
    assert gone.state["freq"] == 144800000


def test_data_mode_is_a_packet_mode_to_hamlib():
    assert hamlib_mode("USB", True) == "PKTUSB"
    assert civ_mode("PKTUSB") == ("USB", True)
    assert civ_mode("USB") == ("USB", False)
    assert civ_mode("ECSSUSB") == (None, False)
    nine = models.model("ic-9700")
    mask = modes_mask(nine.modes, nine.data_modes())
    assert mask & 0x800 and mask & 0x400 and mask & 0x1000
    assert not modes_mask(nine.modes) & (0x400 | 0x800 | 0x1000 | 0x400000)

    rig = Stub(data_mode=True)
    s = RigctlServer(rig, "127.0.0.1", 0)
    assert s._dispatch("m") == ("PKTUSB\n2400\n", False)
    s._dispatch("M USB 2400")
    assert rig.state["data_mode"] is False
    s._dispatch("M PKTUSB 2400")
    assert rig.state["mode"] == "USB" and rig.state["data_mode"] is True
    info, _ = s._dispatch("\\get_vfo_info")
    assert info.splitlines()[1] == "PKTUSB"


# -- transmit, through the app's lease ---------------------------------------

def test_with_no_lease_provider_the_server_is_receive_only():
    rig = Stub()
    s = RigctlServer(rig, "127.0.0.1", 0)
    assert s._dispatch("T 1", "rigctl a") == ("RPRT -11\n", False)
    assert rig.state["ptt"] is False and rig.keyed_with == []
    # Unkey is answered for anybody, lease or not.
    assert s._dispatch("T 0", "rigctl a") == ("RPRT 0\n", False)


def test_keying_asks_the_app_for_a_lease_named_for_the_client():
    asked = []
    rig = Stub()

    def lease_for(holder):
        asked.append(holder)
        return Lease(holder)

    s = RigctlServer(rig, "127.0.0.1", 0, lease=lease_for)
    assert s._dispatch("T 1", "rigctl 10.0.0.5:5000") == ("RPRT 0\n", False)
    assert asked == ["rigctl 10.0.0.5:5000"] and rig.keyed_with == asked
    held = s._held["rigctl 10.0.0.5:5000"]
    assert s._dispatch("T 0", "rigctl 10.0.0.5:5000") == ("RPRT 0\n", False)
    assert held.refusal() == "the transmission ended", "unkey releases the lease"
    assert s._held == {}


def test_the_app_refusing_a_lease_is_function_not_available():
    s = RigctlServer(Stub(), "127.0.0.1", 0, lease=lambda holder: None)
    assert s._dispatch("T 1", "x") == ("RPRT -11\n", False)

    def refusing(holder):
        lease = Lease(holder)
        lease.revoke("outside the band plan")
        return lease

    s = RigctlServer(Stub(), "127.0.0.1", 0, lease=refusing)
    assert s._dispatch("T 1", "x") == ("RPRT -11\n", False)
    assert s._held == {}


async def _ask(port: int, line: str, reader=None, writer=None):
    if reader is None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((line + "\n").encode())
    await writer.drain()
    reply = await asyncio.wait_for(reader.readline(), 2)
    return reply.decode().strip(), reader, writer


def test_over_tcp_a_client_that_vanishes_while_keyed_is_unkeyed():
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            leases = []

            def lease_for(holder):
                leases.append(Lease(holder))
                return leases[-1]

            server = RigctlServer(rig, "127.0.0.1", 0, lease=lease_for)
            await server.start()
            try:
                got, r, w = await _ask(server.port, "f")
                assert got == "144174000"
                got, r, w = await _ask(server.port, "T 1", r, w)
                assert got == "RPRT 0"
                assert await until(lambda: radio.ptt, 2)
                assert leases[0].holder.startswith("rigctl 127.0.0.1:")
                # WSJT-X crashes mid-over: the socket closes with the key down.
                w.close()
                await w.wait_closed()
                assert await until(lambda: not radio.ptt, 2)
                assert radio.ptt_commands == [True, False]
                assert leases[0].refusal() is not None, "and its lease was released"
            finally:
                await server.stop()

    asyncio.run(run())


def test_stopping_with_a_keyed_client_still_connected_unkeys_and_returns():
    """WSJT-X stays connected, and sometimes keyed, while the module stops.
    Since Python 3.12 Server.wait_closed() waits for such a client, so stop()
    never returned and the unkey after it never ran - and the app's shutdown
    stalled behind it until the container was killed."""
    async def run():
        async with OnFake(tx_enabled=True) as (rig, radio):
            leases = []

            def lease_for(holder):
                leases.append(Lease(holder))
                return leases[-1]

            server = RigctlServer(rig, "127.0.0.1", 0, lease=lease_for)
            await server.start()
            got, r, w = await _ask(server.port, "T 1")
            assert got == "RPRT 0" and await until(lambda: radio.ptt, 2)
            started = time.monotonic()
            await asyncio.wait_for(server.stop(), 5)
            assert time.monotonic() - started < 2.5
            assert await until(lambda: not radio.ptt, 2)
            assert radio.ptt_commands == [True, False]
            assert await r.read() == b"", "the client was hung up on"
            w.close()

    asyncio.run(run())


def test_over_tcp_transmit_switched_off_is_refused_whatever_the_lease():
    async def run():
        async with OnFake(tx_enabled=False) as (rig, radio):
            server = RigctlServer(rig, "127.0.0.1", 0, lease=lambda h: Lease(h))
            await server.start()
            try:
                got, r, w = await _ask(server.port, "T 1")
                assert got == "RPRT -11"
                got, r, w = await _ask(server.port, "t", r, w)
                assert got == "0"
                w.close()
                await asyncio.sleep(0.2)
                assert radio.ptt_commands == []
            finally:
                await server.stop()

    asyncio.run(run())
